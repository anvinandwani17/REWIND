"""Scenario lab: named, deterministic scenarios run through the real gateway, recovery
and verifier, each comparing what it expects with what actually happened.

    python -m backend.scenarios              # from the repo root
    python backend/scenarios.py [filter]     # same thing
    python -m backend.scenarios stale --json # only names containing "stale"; JSON to stdout

Every scenario starts with POST /api/demo/reset on a throwaway SQLite database, so the
app's own database is never touched. Scenarios that deliberately break something are
labelled injected_fault. Runs that use scripted model turns are recorded as mode
"replay" - no scenario calls a real model. The report is saved to
backend/data/lab_report.json (or REWIND_LAB_RESULTS_PATH) for GET /api/metrics.
"""
import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

if __package__:  # run as `python -m backend.scenarios`: the backend uses top-level imports
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient  # noqa: E402

import agent  # noqa: E402
import db  # noqa: E402
import demo  # noqa: E402
import gateway  # noqa: E402
import main  # noqa: E402
import metrics  # noqa: E402
import verification  # noqa: E402

OPERATOR = {"X-Role": "operator", "X-Role-Key": "demo-operator-key"}
APPROVER = {"X-Role": "approver", "X-Role-Key": "demo-approver-key"}
STATUS = "/api/tools/update-customer-status"
CREDIT = "/api/tools/update-credit-limit"
NORMAL, FAULT = "normal", "injected_fault"
LAB_ENV = ("REWIND_DB_PATH", "REWIND_DEMO_MODE", "REWIND_POLICIES_PATH", "REWIND_ROLE_KEYS",
           "REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
           "REWIND_FAULT_DISABLE_RECENT_ACTIVITY")


# --- harness ----------------------------------------------------------------------------

@contextmanager
def isolated(workdir):
    saved = {name: os.environ.get(name) for name in LAB_ENV}
    for name in LAB_ENV:
        os.environ.pop(name, None)
    os.environ["REWIND_DB_PATH"] = str(Path(workdir) / "lab.db")
    os.environ["REWIND_DEMO_MODE"] = "1"
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def env(**values):
    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def patched(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


class ScriptedProvider(agent.Provider):
    """Fixed model turns for lab scenarios. Not a model, so runs are labelled replay."""
    name, model, live = "scripted", None, False

    def __init__(self, turns):
        self.turns = list(turns)

    def next_turn(self, system, transcript, tool_definitions):
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def accounts(role):
    """Fixture accounts by their documented role, so no scenario names an account ID."""
    return [c["account_id"] for c in demo.CUSTOMERS if c["role"] == role]


def role_of(account_id):
    return next(c["role"] for c in demo.CUSTOMERS if c["account_id"] == account_id)


def credit_limit(account_id):
    return next(c["credit_limit"] for c in demo.CREDIT_LINES if c["account_id"] == account_id)


def row(table, account_id):
    with db.connect() as conn:
        found = conn.execute(f"SELECT * FROM {table} WHERE account_id = ?", (account_id,)).fetchone()
    return dict(found) if found else None


def snapshot():
    with db.connect() as conn:
        return ([dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY account_id")],
                [dict(r) for r in conn.execute("SELECT * FROM actions ORDER BY action_id")])


def ledger_statuses(**where):
    (column, value), = where.items()
    with db.connect() as conn:
        return [r[0] for r in conn.execute(
            f"SELECT status FROM actions WHERE {column} = ? ORDER BY created_at, action_id", (value,))]


def status_body(account_id, new_status, reason="Lab scenario", **extra):
    return {"account_id": account_id, "new_status": new_status, "reason": reason, **extra}


def credit_body(account_id, new_limit, **extra):
    return {"account_id": account_id, "new_limit": new_limit, "reason": "Lab scenario", **extra}


def failed_check_ids(result):
    return sorted(c["id"] for c in (result or {}).get("checks", []) if not c["passed"])


# --- customer-status scenarios -----------------------------------------------------------------

def clean_archive(account):
    def run(client):
        before = row("customers", account)
        response = client.post(STATUS, json=status_body(
            account, "Archived", "Inactive beyond the threshold", expected_version=before["version"]))
        data = response.json()["data"]
        action = client.get(f"/api/actions/{data['action_id']}").json()["data"]
        after = row("customers", account)
        return {
            "account": account,
            "http_status": response.status_code,
            "status": data["status"],
            "write_guard": data["verification"]["verdict"],
            "post_action_check": data["post_action_check"]["verdict"],
            "ledger_statuses": ledger_statuses(account_id=account),
            "ledger_before_after": [action["before"]["status"], action["after"]["status"]],
            "ledger_versions": [action["before_version"], action["after_version"]],
            "customer_now": [after["status"], after["version"]],
        }
    expected = {
        "account": account, "http_status": 200, "status": "executed", "write_guard": "verified",
        "post_action_check": "eligible", "ledger_statuses": ["executed"],
        "ledger_before_after": ["Active", "Archived"], "ledger_versions": [1, 2],
        "customer_now": ["Archived", 2],
    }
    return expected, run


def protected_block(account):
    def run(client):
        before = row("customers", account)
        response = client.post(STATUS, json=status_body(account, "Archived"))
        data = response.json()["data"]
        return {"http_status": response.status_code, "status": data["status"], "rule_id": data["rule_id"],
                "row_unchanged": row("customers", account) == before,
                "ledger_statuses": ledger_statuses(account_id=account)}
    return {"http_status": 403, "status": "blocked", "rule_id": "protected_customer",
            "row_unchanged": True, "ledger_statuses": ["blocked"]}, run


def stale_replay(client):
    script = agent.load_replay("stale_report")
    target = next(s.args["account_id"] for s in script.steps if s.tool == "update_customer_status")
    before = row("customers", target)
    response = client.post("/api/agent/runs", json={
        "instruction": script.instruction, "mode": "replay", "scenario": "stale_report"})
    return target, before, response.json()["data"]


def recent_activity_block():
    def run(client):
        target, before, run_data = stale_replay(client)
        last = run_data["steps"][-1]
        with db.connect() as conn:
            rules = [r[0] for r in conn.execute("SELECT rule_id FROM actions WHERE run_id = ?",
                                                (run_data["run_id"],))]
        return {"target_role": role_of(target), "mode": run_data["mode"], "run_status": run_data["status"],
                "blocked_step": [last["tool"], last["outcome"]], "rule_ids": rules,
                "row_unchanged": row("customers", target) == before}
    return {"target_role": "recently active", "mode": "replay", "run_status": "stopped_blocked",
            "blocked_step": ["update_customer_status", "blocked"], "rule_ids": ["recent_activity"],
            "row_unchanged": True}, run


def stale_report_fault():
    def run(client):
        with env(REWIND_FAULT_DISABLE_RECENT_ACTIVITY="1"):  # the injected fault: rule disabled
            target, _, run_data = stale_replay(client)
        action = client.get(f"/api/actions/{run_data['action_ids'][0]}").json()["data"]
        undo = client.post(f"/api/actions/{action['id']}/undo").json()["data"]
        after = row("customers", target)
        return {
            "target_role": role_of(target), "run_status": run_data["status"],
            "executed": action["status"], "ledger_scenario": action["scenario"],
            "post_action_check": action["post_action_check"]["verdict"],
            "failed_eligibility_checks": failed_check_ids(action["post_action_check"]),
            "flag": action["flag"], "undo_status": undo["status"],
            "verdict": undo["verification"]["verdict"], "customer_now": [after["status"], after["version"]],
        }
    return {"target_role": "recently active", "run_status": "completed", "executed": "executed",
            "ledger_scenario": "injected_fault", "post_action_check": "flagged_unsafe",
            "failed_eligibility_checks": ["inactive_beyond_threshold"], "flag": "flagged_unsafe",
            "undo_status": "recovered", "verdict": "verified", "customer_now": ["Active", 3]}, run


def archive_id(client, account):
    return client.post(STATUS, json=status_body(account, "Archived")).json()["data"]["action_id"]


def conflict_after_edit(account):
    def run(client):
        action_id = archive_id(client, account)
        _, ops = gateway.execute("update_customer_status",
                                 status_body(account, "Active", "Customer called back"), actor="ops_user")
        newer = row("customers", account)
        response = client.post(f"/api/actions/{action_id}/undo")
        data = response.json()["data"]
        return {"ops_edit": ops["data"]["status"], "undo_http": response.status_code,
                "verdict": data["verification"]["verdict"], "differences": sorted(data["differences"]),
                "newer_edit_intact": row("customers", account) == newer}
    return {"ops_edit": "executed", "undo_http": 409, "verdict": "conflict_detected",
            "differences": ["status", "version"], "newer_edit_intact": True}, run


def double_undo(account):
    def run(client):
        action_id = archive_id(client, account)
        first = client.post(f"/api/actions/{action_id}/undo")
        state = snapshot()
        second = client.post(f"/api/actions/{action_id}/undo")
        return {"first": [first.status_code, first.json()["data"]["verification"]["verdict"]],
                "second": [second.status_code, second.json()["data"]["already_recovered"]],
                "second_changed_nothing": snapshot() == state,
                "undo_rows": len([s for s in ledger_statuses(undo_of_action_id=action_id)])}
    return {"first": [200, "verified"], "second": [200, True], "second_changed_nothing": True,
            "undo_rows": 1}, run


def tampered_snapshot(account):
    def run(client):
        action_id = archive_id(client, account)
        with db.transaction() as conn:  # the injected fault: rewrite the stored before-state
            before = json.loads(conn.execute("SELECT before_json FROM actions WHERE action_id = ?",
                                             (action_id,)).fetchone()[0])
            before["status"] = "Archived"  # claims it was already archived, so a restore would be a no-op
            conn.execute("UPDATE actions SET before_json = ? WHERE action_id = ?",
                         (json.dumps(before), action_id))
        customer = row("customers", account)
        response = client.post(f"/api/actions/{action_id}/undo")
        data = response.json()["data"]
        return {"undo_http": response.status_code, "undo_status": data["status"],
                "verdict": data["verification"]["verdict"],
                "guard_failed_checks": failed_check_ids(data["pre_commit_guard"]),
                "customer_unchanged": row("customers", account) == customer}
    return {"undo_http": 500, "undo_status": "failed", "verdict": "failed",
            "guard_failed_checks": ["ledger_snapshot_intact"], "customer_unchanged": True}, run


def tamper_during_recovery(route, body, table, tamper_sql, bystander):
    def run(client):
        action_id = client.post(route, json=body).json()["data"]["action_id"]
        real_verify = verification.verify_undo

        def tamper_then_verify(undo_id):  # the injected fault: a write in the recovery window
            with db.transaction() as conn:
                conn.execute(tamper_sql, (bystander,))
            return real_verify(undo_id)

        with patched(verification, "verify_undo", tamper_then_verify):
            response = client.post(f"/api/actions/{action_id}/undo")
        data = response.json()["data"]
        return {"undo_http": response.status_code, "undo_status": data["status"],
                "verdict": data["verification"]["verdict"],
                "failed_checks": failed_check_ids(data["verification"])}
    return {"undo_http": 500, "undo_status": "recovered", "verdict": "failed",
            "failed_checks": ["unrelated_rows_unchanged"]}, run


def malformed_tool_call(account):
    def run(client):
        bad = {"account_id": account, "new_status": "Archived"}  # no reason
        provider = ScriptedProvider([
            agent.Turn(tool_calls=[agent.ToolCall("c1", "update_customer_status", bad)]),
            agent.Turn(tool_calls=[agent.ToolCall("c2", "update_customer_status", bad)]),
        ])
        before = snapshot()
        _, body = agent.run_live("Archive inactive customers", provider)
        run_data = body["data"]
        return {"mode": run_data["mode"], "run_status": run_data["status"],
                "outcomes": [s["outcome"] for s in run_data["steps"]], "action_ids": run_data["action_ids"],
                "customers_and_ledger_unchanged": snapshot() == before}
    return {"mode": "replay", "run_status": "stopped_invalid_tool_call", "outcomes": ["rejected", "rejected"],
            "action_ids": [], "customers_and_ledger_unchanged": True}, run


def provider_failure(account):
    def run(client):
        provider = ScriptedProvider([
            agent.Turn(tool_calls=[agent.ToolCall("c1", "get_customer", {"account_id": account})]),
            agent.Turn(tool_calls=[agent.ToolCall("c2", "update_customer_status", status_body(
                account, "Archived", "Inactive beyond the threshold", expected_version=1))]),
            agent.ProviderError("simulated provider timeout"),
        ])
        status, body = agent.run_live("Archive inactive customers", provider)
        run_data = body["data"]
        with db.connect() as conn:
            ledger_ids = [r[0] for r in conn.execute(
                "SELECT action_id FROM actions WHERE run_id = ? ORDER BY created_at", (run_data["run_id"],))]
            changed = [r[0] for r in conn.execute("SELECT account_id FROM customers WHERE version != 1")]
        return {"http_status": status, "error": body["error"]["code"], "run_status": run_data["status"],
                "ledger_statuses": ledger_statuses(run_id=run_data["run_id"]),
                "action_ids_match_ledger": ledger_ids == run_data["action_ids"],
                "changed_customers": changed}
    return {"http_status": 502, "error": "provider_failed", "run_status": "provider_failed",
            "ledger_statuses": ["executed"], "action_ids_match_ledger": True,
            "changed_customers": [account]}, run


# --- credit-limit scenarios ----------------------------------------------------------------

def credit_change_then_undo(account, new_limit):
    def run(client):
        response = client.post(CREDIT, json=credit_body(account, new_limit))
        data = response.json()["data"]
        undo = client.post(f"/api/actions/{data['action_id']}/undo").json()["data"]
        return {"status": data["status"], "undo_status": undo["status"],
                "verdict": undo["verification"]["verdict"],
                "limit_restored": row("credit_lines", account)["credit_limit"] == credit_limit(account)}
    return {"status": "executed", "undo_status": "recovered", "verdict": "verified", "limit_restored": True}, run


def credit_conflict_after_edit(account, first, second):
    def run(client):
        action_id = client.post(CREDIT, json=credit_body(account, first)).json()["data"]["action_id"]
        client.post(CREDIT, json=credit_body(account, second))
        newer = row("credit_lines", account)
        data = client.post(f"/api/actions/{action_id}/undo").json()["data"]
        return {"verdict": data["verification"]["verdict"],
                "newer_edit_intact": row("credit_lines", account) == newer}
    return {"verdict": "conflict_detected", "newer_edit_intact": True}, run


def credit_policy_outcome(account, new_limit, status, rule_id):
    def run(client):
        before = row("credit_lines", account)
        data = client.post(CREDIT, json=credit_body(account, new_limit)).json()["data"]
        return {"status": data["status"], "rule_id": data["rule_id"],
                "unchanged": row("credit_lines", account) == before}
    return {"status": status, "rule_id": rule_id, "unchanged": True}, run


def credit_large_increase_approval(account):
    def run(client):
        pending = client.post(CREDIT, json=credit_body(account, credit_limit(account) * 2)).json()["data"]
        approved = client.post(f"/api/actions/{pending['action_id']}/approve", headers=APPROVER).json()["data"]
        return {"requested": [pending["status"], pending["rule_id"]], "after_approval": approved["status"],
                "limit_now": row("credit_lines", account)["credit_limit"]}
    return {"requested": ["pending_approval", "large_increase"], "after_approval": "executed",
            "limit_now": credit_limit(account) * 2}, run


def credit_broken_policy(account, workdir):
    expected, inner = credit_policy_outcome(account, credit_limit(account) // 2, "blocked",
                                            "policy_config_invalid")

    def run(client):
        path = Path(workdir) / "broken-policies.yaml"
        path.write_text("credit_limit: [this is not, valid", encoding="utf-8")
        with env(REWIND_POLICIES_PATH=str(path)):  # the injected fault: unreadable policy file
            return inner(client)
    return expected, run


# --- catalogue -------------------------------------------------------------------------------

def scenarios(workdir):
    eligible, second_eligible = accounts("inactive, eligible to archive")
    protected = accounts("inactive but protected")[0]
    bystander = accounts("moderately inactive")[0]
    recent = accounts("recently active")[0]
    lower = credit_limit(eligible) // 2
    catalogue = [
        ("clean_archive", NORMAL, "customer_status",
         "Eligible inactive customer is archived; ledger, guard and post-action check agree",
         clean_archive(eligible)),
        ("protected_block", NORMAL, "customer_status",
         "Archiving a protected customer is blocked and the row is unchanged", protected_block(protected)),
        ("recent_activity_block", NORMAL, "customer_status",
         "Replay acting on a stale report: the recent_activity rule (on) blocks before any write - "
         "the production-correct behaviour", recent_activity_block()),
        ("stale_report_fault", FAULT, "customer_status",
         "INJECTED FAULT: recent_activity disabled. The stale-report archive executes, the post-action "
         "eligibility check flags it flagged_unsafe, and Undo restores it with a verified verdict",
         stale_report_fault()),
        ("conflict_after_edit", NORMAL, "customer_status",
         "ops_user edits the customer after the archive; Undo refuses with conflict_detected",
         conflict_after_edit(eligible)),
        ("double_undo", NORMAL, "customer_status", "A second Undo is a no-op", double_undo(eligible)),
        ("tampered_snapshot", FAULT, "customer_status",
         "INJECTED FAULT: the stored before-state is rewritten; the restore is refused and nothing "
         "is reported verified", tampered_snapshot(eligible)),
        ("unrelated_row_tamper", FAULT, "customer_status",
         "INJECTED FAULT: another customer changes in the recovery window; verdict failed",
         tamper_during_recovery(STATUS, status_body(eligible, "Archived"), "customers",
                                "UPDATE customers SET name = 'Tampered' WHERE account_id = ?", bystander)),
        ("malformed_tool_call", NORMAL, "customer_status",
         "Scripted model turns send malformed arguments twice; rejected, nothing written",
         malformed_tool_call(eligible)),
        ("provider_failure", NORMAL, "customer_status",
         "Scripted model turns fail mid-run; the ledger holds exactly what executed",
         provider_failure(eligible)),
        ("second_customer", NORMAL, "customer_status",
         "clean_archive on a different eligible account: nothing is hardcoded",
         clean_archive(second_eligible)),
        ("credit_lower_then_undo", NORMAL, "credit_limit", "Lower a limit, Undo, verified",
         credit_change_then_undo(eligible, lower)),
        ("credit_conflict_after_edit", NORMAL, "credit_limit", "Undo after a newer limit change conflicts",
         credit_conflict_after_edit(eligible, lower, lower + lower // 2)),
        ("credit_unrelated_row_tamper", FAULT, "credit_limit",
         "INJECTED FAULT: another credit line changes in the recovery window; verdict failed",
         tamper_during_recovery(CREDIT, credit_body(eligible, lower), "credit_lines",
                                "UPDATE credit_lines SET currency = 'USD' WHERE account_id = ?", recent)),
        ("credit_large_increase_approval", NORMAL, "credit_limit",
         "A large increase waits for approval, then executes", credit_large_increase_approval(second_eligible)),
        ("credit_above_max_block", NORMAL, "credit_limit", "A limit above max_limit is blocked",
         credit_policy_outcome(recent, 10_000_000, "blocked", "limit_out_of_range")),
        ("credit_protected_block", NORMAL, "credit_limit", "A protected customer's limit is blocked",
         credit_policy_outcome(protected, credit_limit(protected) + 1, "blocked", "protected_customer")),
        ("credit_broken_policy", FAULT, "credit_limit",
         "INJECTED FAULT: policies.yaml is unreadable; every credit change is blocked (fails closed)",
         credit_broken_policy(eligible, workdir)),
    ]
    return [{"name": name, "label": label, "adapter": adapter, "description": description,
             "expected": expected, "run": run}
            for name, label, adapter, description, (expected, run) in catalogue]


def run_all(name_filter=None):
    results = []
    with tempfile.TemporaryDirectory() as workdir, isolated(workdir):
        with TestClient(main.app, headers=OPERATOR) as client:
            for scenario in scenarios(workdir):
                if name_filter and name_filter not in scenario["name"]:
                    continue
                client.post("/api/demo/reset")  # every scenario starts from the fixture
                try:
                    actual = scenario["run"](client)
                except Exception as exc:  # a crashing scenario is a failed scenario, not a crashed lab
                    actual = {"error": f"{type(exc).__name__}: {exc}"}
                expected = scenario["expected"]
                mismatches = sorted(key for key in expected.keys() | actual.keys()
                                    if expected.get(key) != actual.get(key))
                results.append({key: scenario[key] for key in ("name", "label", "adapter", "description")}
                               | {"expected": expected, "actual": actual, "ok": not mismatches,
                                  "mismatches": mismatches})
    return results


def build_report(results, name_filter=None):
    return {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "filter": name_filter,
        "isolation": "throwaway SQLite database reset before every scenario; the app's database is untouched",
        "totals": {
            "total": len(results),
            "passed": sum(r["ok"] for r in results),
            "failed": sum(not r["ok"] for r in results),
            "injected_faults": sum(r["label"] == FAULT for r in results),
        },
        "scenarios": results,
    }


def save_report(report, path=None):
    path = Path(path or metrics.lab_results_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def _compact(values):
    return ", ".join(f"{key}={json.dumps(value)}" for key, value in values.items())


def print_table(report):
    results = report["scenarios"]
    width = max([len("scenario")] + [len(r["name"]) for r in results])
    print(f"{'#':>2}  {'scenario':<{width}}  {'label':<14}  result")
    print(f"{'-' * 2}  {'-' * width}  {'-' * 14}  ------")
    for number, r in enumerate(results, 1):
        print(f"{number:>2}  {r['name']:<{width}}  {r['label']:<14}  {'PASS' if r['ok'] else 'FAIL'}")
        print(f"{'':>{width + 6}}expected: {_compact(r['expected'])}")
        print(f"{'':>{width + 6}}actual:   {_compact(r['actual'])}")
        if r["mismatches"]:
            print(f"{'':>{width + 6}}MISMATCH: {', '.join(r['mismatches'])}")
    totals = report["totals"]
    print(f"\n{totals['passed']}/{totals['total']} scenarios behaved as expected "
          f"({totals['failed']} failed, {totals['injected_faults']} injected faults)")


def cli(argv=None):
    parser = argparse.ArgumentParser(description="Run the REWIND scenario lab.")
    parser.add_argument("filter", nargs="?", help="only scenarios whose name contains this")
    parser.add_argument("--json", action="store_true", help="print the JSON report instead of the table")
    args = parser.parse_args(argv)
    report = build_report(run_all(args.filter), args.filter)
    path = save_report(report)
    if args.json:
        print(json.dumps(report))
    else:
        print_table(report)
        print(f"Saved to {path}")
    return 0 if report["totals"]["total"] and not report["totals"]["failed"] else 1


if __name__ == "__main__":
    sys.exit(cli())
