"""Scenario lab: run named, deterministic scenarios against a throwaway database.

    python backend/lab.py           # every scenario
    python backend/lab.py credit    # only scenarios whose name contains "credit"

Each scenario drives the real app through TestClient (same routes, gateway,
adapters and verifier as a live server), then compares what actually happened
with what it expects. Exit code 1 if any outcome differs. Scenarios that
deliberately break something (tampering with SQLite, a broken policy file) are
labelled injected_fault. Your real database is never touched.
"""
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

import db
import demo
import main
import verification

OPERATOR = {"X-Role": "operator", "X-Role-Key": "demo-operator-key"}
APPROVER = {"X-Role": "approver", "X-Role-Key": "demo-approver-key"}
STATUS = "/api/tools/update-customer-status"
CREDIT = "/api/tools/update-credit-limit"
LAB_ENV = ("REWIND_DB_PATH", "REWIND_DEMO_MODE", "REWIND_POLICIES_PATH", "REWIND_ROLE_KEYS",
           "REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
           "REWIND_FAULT_DISABLE_RECENT_ACTIVITY")


@contextmanager
def lab_environment(workdir):
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


def value_of(table, account_id, field):
    with db.connect() as conn:
        return conn.execute(f"SELECT {field}, version FROM {table} WHERE account_id = ?",
                            (account_id,)).fetchone()


# --- scenario steps: each returns the actual outcome as a short string ---------------------

def status_body(account_id, new_status, **extra):
    return {"account_id": account_id, "new_status": new_status, "reason": "lab", **extra}


def credit_body(account_id, new_limit, **extra):
    return {"account_id": account_id, "new_limit": new_limit, "reason": "lab", **extra}


def change_then_undo(route, body):
    def run(client):
        action_id = client.post(route, json=body).json()["data"]["action_id"]
        data = client.post(f"/api/actions/{action_id}/undo").json()["data"]
        return f"{data['status']}/{data['verification']['verdict']}"
    return run


def undo_after_newer_edit(route, first, second, table, field):
    def run(client):
        action_id = client.post(route, json=first).json()["data"]["action_id"]
        client.post(route, json=second)
        newer = value_of(table, first["account_id"], field)
        data = client.post(f"/api/actions/{action_id}/undo").json()["data"]
        intact = value_of(table, first["account_id"], field) == newer
        return f"{data['verification']['verdict']}/{'edit_intact' if intact else 'edit_overwritten'}"
    return run


def tamper_after_restore(route, body, tamper_sql, other_id):
    def run(client):
        action_id = client.post(route, json=body).json()["data"]["action_id"]
        undo_id = client.post(f"/api/actions/{action_id}/undo").json()["data"]["undo_action_id"]
        with db.transaction() as conn:
            conn.execute(tamper_sql, (other_id,))
        return verification.verify_undo(undo_id)["verdict"]
    return run


def policy_outcome(route, body):
    def run(client):
        data = client.post(route, json=body).json()["data"]
        return f"{data['status']}/{data['rule_id']}"
    return run


def approval_flow(route, body, env=None):
    def run(client):
        os.environ.update(env or {})
        try:
            pending = client.post(route, json=body).json()["data"]
            approved = client.post(f"/api/actions/{pending['action_id']}/approve", headers=APPROVER)
            return f"{pending['status']}->{approved.json()['data']['status']}"
        finally:
            for name in env or {}:
                os.environ.pop(name, None)
    return run


def broken_policy_file(route, body, workdir):
    def run(client):
        path = Path(workdir) / "broken-policies.yaml"
        path.write_text("credit_limit: [this is not, valid", encoding="utf-8")
        os.environ["REWIND_POLICIES_PATH"] = str(path)
        try:
            before = value_of("credit_lines", body["account_id"], "credit_limit")
            data = client.post(route, json=body).json()["data"]
            unchanged = value_of("credit_lines", body["account_id"], "credit_limit") == before
            return f"{data['status']}/{data['rule_id']}/{'unchanged' if unchanged else 'CHANGED'}"
        finally:
            os.environ.pop("REWIND_POLICIES_PATH", None)
    return run


def accounts(role):
    """Fixture accounts by their documented role, so scenarios name no account IDs."""
    return [c["account_id"] for c in demo.CUSTOMERS if c["role"] == role]


def credit_limit(account_id):
    return next(c["credit_limit"] for c in demo.CREDIT_LINES if c["account_id"] == account_id)


def scenarios(workdir):
    """(name, adapter, kind, expected, run)"""
    eligible, second_eligible = accounts("inactive, eligible to archive")
    protected = accounts("inactive but protected")[0]
    bystander = accounts("moderately inactive")[0]
    recent = accounts("recently active")[0]
    lower = credit_limit(eligible) // 2
    doubled = credit_limit(second_eligible) * 2  # +100%: above approval_increase_pct
    return [
        ("status: archive then undo", "customer_status", "normal", "recovered/verified",
         change_then_undo(STATUS, status_body(eligible, "Archived"))),
        ("status: undo after a newer edit", "customer_status", "normal", "conflict_detected/edit_intact",
         undo_after_newer_edit(STATUS, status_body(eligible, "Archived"),
                               status_body(eligible, "Active"), "customers", "status")),
        ("status: tamper with another customer after restore", "customer_status", "injected_fault",
         "failed",
         tamper_after_restore(STATUS, status_body(eligible, "Archived"),
                              "UPDATE customers SET name = 'Tampered' WHERE account_id = ?", bystander)),
        ("status: protected customer", "customer_status", "normal", "blocked/protected_customer",
         policy_outcome(STATUS, status_body(protected, "Archived"))),
        ("status: bulk run needs approval", "customer_status", "normal", "pending_approval->executed",
         approval_flow(STATUS, status_body(eligible, "Archived", run_id="lab-bulk"),
                       env={"REWIND_BULK_APPROVAL_THRESHOLD": "0"})),
        ("credit: lower a limit then undo", "credit_limit", "normal", "recovered/verified",
         change_then_undo(CREDIT, credit_body(eligible, lower))),
        ("credit: undo after a newer edit", "credit_limit", "normal", "conflict_detected/edit_intact",
         undo_after_newer_edit(CREDIT, credit_body(eligible, lower),
                               credit_body(eligible, lower + lower // 2), "credit_lines", "credit_limit")),
        ("credit: tamper with another credit line after restore", "credit_limit", "injected_fault",
         "failed",
         tamper_after_restore(CREDIT, credit_body(eligible, lower),
                              "UPDATE credit_lines SET currency = 'USD' WHERE account_id = ?", recent)),
        ("credit: large increase needs approval", "credit_limit", "normal", "pending_approval->executed",
         approval_flow(CREDIT, credit_body(second_eligible, doubled))),
        ("credit: above max limit", "credit_limit", "normal", "blocked/limit_out_of_range",
         policy_outcome(CREDIT, credit_body(recent, 10_000_000))),
        ("credit: protected customer", "credit_limit", "normal", "blocked/protected_customer",
         policy_outcome(CREDIT, credit_body(protected, credit_limit(protected) + 1))),
        ("credit: broken policies.yaml fails closed", "credit_limit", "injected_fault",
         "blocked/policy_config_invalid/unchanged",
         broken_policy_file(CREDIT, credit_body(eligible, lower), workdir)),
    ]


def run_all(name_filter=None):
    results = []
    with tempfile.TemporaryDirectory() as workdir, lab_environment(workdir):
        with TestClient(main.app, headers=OPERATOR) as client:
            for name, adapter, kind, expected, run in scenarios(workdir):
                if name_filter and name_filter not in name:
                    continue
                client.post("/api/demo/reset")  # every scenario starts from the fixture
                try:
                    actual = run(client)
                except Exception as exc:  # report it as a failed scenario, don't stop the lab
                    actual = f"error: {type(exc).__name__}: {exc}"
                results.append({"name": name, "adapter": adapter, "kind": kind,
                                "expected": expected, "actual": actual, "ok": actual == expected})
    return results


def print_table(results):
    headers = ("scenario", "adapter", "kind", "expected", "actual", "result")
    table = [(r["name"], r["adapter"], r["kind"], r["expected"], r["actual"],
              "PASS" if r["ok"] else "FAIL") for r in results]
    widths = [max(len(str(row[i])) for row in [headers, *table]) for i in range(len(headers))]
    for row in [headers, tuple("-" * w for w in widths), *table]:
        print("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)))
    passed = sum(r["ok"] for r in results)
    print(f"\n{passed}/{len(results)} scenarios behaved as expected")


if __name__ == "__main__":
    lab_results = run_all(sys.argv[1] if len(sys.argv) > 1 else None)
    print_table(lab_results)
    sys.exit(0 if lab_results and all(r["ok"] for r in lab_results) else 1)
