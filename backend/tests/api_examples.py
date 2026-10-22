"""Real request/response examples, produced by calling the app through TestClient.

Used by test_contract.py to lock response shapes and to generate
docs/backend-api.md, so the docs can never drift from the code.
IDs and timestamps are made deterministic; everything else is real output.
"""
import itertools
import json
import re
from datetime import timedelta

import agent
import demo
import gateway
import ledger
import recovery

UPDATE = "/api/tools/update-customer-status"
CREDIT = "/api/tools/update-credit-limit"
ROLE_HEADERS = {
    role: {"X-Role": role, "X-Role-Key": f"demo-{role}-key"}
    for role in ("operator", "approver", "auditor")
}


def make_deterministic(monkeypatch):
    ids = itertools.count(1)
    ticks = itertools.count(1)

    def fake_uuid():
        return f"00000000-0000-4000-8000-{next(ids):012d}"

    def fake_now():
        return (demo.DEMO_NOW + timedelta(seconds=next(ticks))).isoformat()

    for module in (gateway, recovery, agent):
        monkeypatch.setattr(module, "uuid4", fake_uuid)
    monkeypatch.setattr(ledger, "now", fake_now)
    monkeypatch.setattr(recovery, "now", fake_now)
    monkeypatch.setattr(gateway, "now", fake_now)


def run_examples(client, monkeypatch):
    """Walk the demo story once; return [{id, title, method, path, body, status, response}]."""
    examples = []

    def call(example_id, title, method, path, body=None, role="default"):
        # Reads need no role; writes default to operator. role=None sends no headers.
        if role == "default":
            role = None if method == "GET" else "operator"
        headers = ROLE_HEADERS[role] if role else {}
        response = client.request(method, path, json=body, headers=headers)
        examples.append({
            "id": example_id, "title": title, "method": method, "path": path,
            "headers": headers, "body": body, "status": response.status_code,
            "response": response.json(),
        })
        return response.json()

    def update(account_id, new_status, reason, **extra):
        return {"account_id": account_id, "new_status": new_status, "reason": reason, **extra}

    call("reset", "Reset the demo", "POST", "/api/demo/reset")
    call("customers", "List customers", "GET", "/api/customers")

    executed = call("update_executed", "Archive an inactive customer", "POST", UPDATE,
                    update("ACC-102", "Archived", "Inactive for 591 days", expected_version=1, run_id="run-1"))
    archive_id = executed["data"]["action_id"]
    call("action_undo_available", "Get one action (Undo available)", "GET", f"/api/actions/{archive_id}")
    call("undo_verified", "Undo it", "POST", f"/api/actions/{archive_id}/undo")
    call("undo_repeat", "Undo it again (idempotent)", "POST", f"/api/actions/{archive_id}/undo")

    blocked = call("update_blocked_protected", "Blocked: protected customer", "POST", UPDATE,
                   update("ACC-103", "Archived", "Inactive for 792 days", run_id="run-1"))
    call("update_blocked_recent", "Blocked: recently active", "POST", UPDATE,
         update("ACC-104", "Archived", "Cleanup", run_id="run-1"))
    call("update_no_change", "Blocked: already has that status", "POST", UPDATE,
         update("ACC-101", "Active", "Reactivate"))
    call("update_version_conflict", "Stale expected_version", "POST", UPDATE,
         update("ACC-105", "Archived", "Inactive for 420 days", expected_version=7))
    call("update_not_found", "Unknown account", "POST", UPDATE,
         update("ACC-999", "Archived", "Cleanup"))
    call("update_malformed", "Malformed arguments", "POST", UPDATE,
         {"account_id": "ACC-105", "new_status": "Archived"})

    second = call("update_executed_2", "Archive another customer", "POST", UPDATE,
                  update("ACC-105", "Archived", "Inactive for 420 days", run_id="run-2"))
    second_id = second["data"]["action_id"]
    call("update_newer_edit", "Someone else edits that customer", "POST", UPDATE,
         update("ACC-105", "Active", "Customer called back"))
    call("action_undo_conflict_predicted", "Get the earlier action (Undo now disabled)", "GET",
         f"/api/actions/{second_id}")
    call("undo_conflict", "Undo after a newer edit: conflict", "POST", f"/api/actions/{second_id}/undo")
    call("undo_not_undoable", "Undo a blocked action", "POST",
         f"/api/actions/{blocked['data']['action_id']}/undo")
    call("undo_not_found", "Undo an unknown action", "POST", "/api/actions/does-not-exist/undo")

    monkeypatch.setenv("REWIND_BULK_APPROVAL_THRESHOLD", "1")
    client.post(UPDATE, headers=ROLE_HEADERS["operator"],
                json=update("ACC-108", "Archived", "Inactive for 150 days", run_id="run-bulk"))
    pending = call("update_pending_approval", "Bulk run over threshold (threshold set to 1)", "POST", UPDATE,
                   update("ACC-102", "Archived", "Inactive for 591 days", run_id="run-bulk"))
    pending_id = pending["data"]["action_id"]
    call("approve_pending", "Approve a pending action", "POST", f"/api/actions/{pending_id}/approve",
         {"note": "Checked the account history"}, role="approver")
    call("approve_not_pending", "Approve it again", "POST", f"/api/actions/{pending_id}/approve",
         role="approver")
    second_pending = call("update_pending_approval_2", "Another bulk change waits for approval", "POST",
                          UPDATE, update("ACC-105", "Archived", "Inactive for 420 days", run_id="run-bulk"))
    call("reject_pending", "Reject a pending action", "POST",
         f"/api/actions/{second_pending['data']['action_id']}/reject", {"note": "Not this week"},
         role="approver")
    monkeypatch.delenv("REWIND_BULK_APPROVAL_THRESHOLD")

    call("update_role_missing", "No role headers", "POST", UPDATE,
         update("ACC-105", "Archived", "Cleanup"), role=None)
    call("update_role_forbidden", "Wrong role for the route", "POST", UPDATE,
         update("ACC-105", "Archived", "Cleanup"), role="auditor")
    call("audit_export", "Export the audit trail", "GET", "/api/audit/export", role="auditor")
    call("audit_export_forbidden", "Export as an operator", "GET", "/api/audit/export", role="operator")

    def credit(account_id, new_limit, reason):
        return {"account_id": account_id, "new_limit": new_limit, "reason": reason}

    call("credit_lines", "List credit lines", "GET", "/api/credit-lines")
    credit_change = call("credit_update_executed", "Raise a credit limit", "POST", CREDIT,
                         credit("ACC-104", 100000, "Six months of on-time payments"))
    call("credit_undo", "Undo the credit-limit change", "POST",
         f"/api/actions/{credit_change['data']['action_id']}/undo")
    call("credit_blocked", "Blocked: above max_limit", "POST", CREDIT,
         credit("ACC-104", 600000, "Requested by branch"))
    call("credit_pending", "Large increase waits for approval", "POST", CREDIT,
         credit("ACC-105", 60000, "Seasonal stock purchase"))
    call("credit_not_found", "No credit line for the account", "POST", CREDIT,
         credit("ACC-107", 10000, "Open a line"))

    call("actions", "List actions", "GET", "/api/actions")
    call("metrics", "Metrics (no saved lab run)", "GET", "/api/metrics")
    call("route_not_found", "Unknown route", "GET", "/api/nope")

    monkeypatch.delenv("REWIND_DEMO_MODE")
    call("reset_refused", "Reset with demo mode off", "POST", "/api/demo/reset")
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")

    # Agent runs start from a fresh fixture so the replay scripts' expected_version holds.
    client.post("/api/demo/reset", headers=ROLE_HEADERS["operator"])
    instruction = "Archive every customer who has been inactive for more than a year."
    replay_run = call("agent_run_replay", "Replay run: archive inactive customers", "POST",
                      "/api/agent/runs",
                      {"instruction": instruction, "mode": "replay", "scenario": "archive_inactive"})
    call("agent_run_replay_blocked", "Replay run stopped by a policy block", "POST", "/api/agent/runs",
         {"instruction": instruction, "mode": "replay", "scenario": "blocked_protected"})
    call("agent_run_live_unconfigured", "Live run with no provider configured", "POST",
         "/api/agent/runs", {"instruction": instruction, "mode": "live"})

    # Evidence after one of the run's actions has been undone.
    replay_id = replay_run["data"]["run_id"]
    client.post(f"/api/actions/{replay_run['data']['action_ids'][0]}/undo", headers=ROLE_HEADERS["operator"])
    call("run_evidence", "Evidence for the replay run (after undoing its first action)", "GET",
         f"/api/runs/{replay_id}/evidence")
    call("run_evidence_not_found", "Evidence for an unknown run", "GET", "/api/runs/nope/evidence")
    return examples


def shape(value):
    """Keys and JSON types only, recursively; lists are represented by their first item."""
    if isinstance(value, dict):
        return {key: shape(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return {bool: "bool", int: "int", float: "float", str: "str", type(None): "null"}[type(value)]


# --- docs rendering ------------------------------------------------------------------

def _json(value):
    return json.dumps(value, indent=2)


def _example(ex, note=None):
    lines = [f"#### {ex['title']}", "", "```http", f"{ex['method']} {ex['path']}"]
    lines += [f"{name}: {value}" for name, value in ex["headers"].items()]
    if ex["body"] is not None:
        lines += ["Content-Type: application/json", "", _json(ex["body"])]
    lines += ["```", "", f"Response `{ex['status']}`:", "", "```json", _json(ex["response"]), "```", ""]
    if note:
        lines[1:1] = ["", note]
    return "\n".join(lines)


def _undo_table():
    """Rows come from recovery.undo_eligibility itself, not from prose."""
    after = {"account_id": "X", "status": "Archived", "version": 2}
    base = {"tool_name": "update_customer_status", "status": "executed",
            "recovery_state": "available", "after": after, "after_version": 2}
    cases = [
        ("Executed status change, customer unchanged since", base, after),
        ("Executed, Undo failed before (`recovery_state: restore_failed`)",
         {**base, "recovery_state": "restore_failed"}, after),
        ("Already undone (`recovery_state: recovered`)", {**base, "recovery_state": "recovered"}, after),
        ("Customer edited since (version or any field differs)", base, {**after, "version": 3}),
        ("Action was `blocked` / `rejected` / `pending_approval` / `failed`",
         {**base, "status": "blocked"}, after),
        ("The row is itself an Undo record", {**base, "tool_name": "undo"}, after),
    ]
    rows = ["| Situation | `undo_available` | `undo_disabled_reason` |", "|---|---|---|"]
    for label, action, current in cases:
        available, reason, _ = recovery.undo_eligibility(action, current)
        rows.append(f"| {label} | `{str(available).lower()}` | {reason or '`null`'} |")
    return "\n".join(rows)


def _status_table(examples):
    rows = ["| Route | Status | `error.code` | When |", "|---|---|---|---|"]
    for ex in examples:
        code = (ex["response"].get("error") or {}).get("code")
        path = re.sub(r"/api/actions/[^/]+", "/api/actions/{action_id}", ex["path"])
        route = f"`{ex['method']} {path}`"
        rows.append(f"| {route} | {ex['status']} | {f'`{code}`' if code else '—'} | {ex['title']} |")
    return "\n".join(rows)


def render(examples):
    by_id = {ex["id"]: ex for ex in examples}
    actions = by_id["actions"]
    trimmed = {**actions, "response": {**actions["response"], "data": actions["response"]["data"][:1]}}
    count = len(actions["response"]["data"])
    export = by_id["audit_export"]
    export_data = export["response"]["data"]
    trimmed_export = {**export, "response": {**export["response"],
                      "data": {**export_data, "actions": export_data["actions"][:1]}}}

    sections = [
        "# REWIND backend API",
        "",
        "<!-- Generated by backend/tests/api_examples.py from real TestClient calls. "
        "Do not edit by hand: run `$env:REWIND_REGENERATE = \"1\"; python -m pytest -q` "
        "then remove the variable. -->",
        "",
        "Scope: there is **no universal rollback**. Undo supports exactly the changes a recovery "
        "adapter implements (`backend/adapters.py`): customer status (`update_customer_status`) and "
        "credit limits (`update_credit_limit`).",
        "",
        "```powershell",
        '$env:REWIND_DEMO_MODE = "1"; Set-Location backend; uvicorn main:app --reload',
        "```",
        "",
        "Base URL `http://127.0.0.1:8000`. CORS allows only `FRONTEND_ORIGIN` "
        "(default `http://localhost:5173`; read at startup).",
        "",
        "## Envelope",
        "",
        "Every response, success or error, is:",
        "",
        "```json",
        '{"data": <payload or null>, "error": null | {"code": "...", "message": "...", "details": ...}}',
        "```",
        "",
        "- `error` is `null` **exactly** when the HTTP status is 2xx.",
        "- Some non-2xx responses still carry `data` (the recorded outcome): policy blocks, Undo "
        "conflicts, and unverified/failed writes. Show `error.message`, and use `data` for detail.",
        "- Show \"verified\" only when `verification.verdict == \"verified\"`.",
        "",
        "## Roles",
        "",
        "Send `X-Role` and `X-Role-Key` on every write. This is a demo role gate, **not** an "
        "authentication system: keys are shared strings from `REWIND_ROLE_KEYS` "
        "(`operator=...,approver=...,auditor=...`), defaulting to the public demo keys "
        "`demo-operator-key`, `demo-approver-key`, `demo-auditor-key`.",
        "",
        "| Role | May call |",
        "|---|---|",
        "| (none) | `GET /api/customers`, `GET /api/credit-lines`, `GET /api/actions`, "
        "`GET /api/actions/{action_id}`, `GET /api/metrics`, `GET /api/runs/{run_id}/evidence` |",
        "| `operator` | `POST /api/tools/update-customer-status`, `POST /api/tools/update-credit-limit`, "
        "`POST /api/actions/{action_id}/undo`, "
        "`POST /api/demo/reset`, `POST /api/agent/runs`, `POST /api/lab/run` |",
        "| `approver` | `POST /api/actions/{action_id}/approve`, `POST /api/actions/{action_id}/reject` |",
        "| `auditor` | `GET /api/audit/export` |",
        "",
        "Each write route allows exactly one role. Missing/bad headers → 401 (`role_required`, "
        "`invalid_role_key`); a valid role on the wrong route → 403 `role_forbidden`. Nothing is written "
        "in either case.",
        "",
        "## Credit-limit policy (`backend/policies.yaml`)",
        "",
        "Read on every request. If the file is missing, invalid YAML, or has the wrong keys, **every** "
        "credit-limit change is blocked with `rule_id: policy_config_invalid` (fails closed). "
        "Rules, in order:",
        "",
        "| `rule_id` | Outcome | When |",
        "|---|---|---|",
        "| `policy_config_invalid` | 403 blocked | policies.yaml cannot be loaded |",
        "| `protected_customer` | 403 blocked | the owning customer is protected and `block_protected_customers` |",
        "| `no_change` | 409 blocked | new limit equals the current limit |",
        "| `limit_out_of_range` | 403 blocked | outside `min_limit`..`max_limit` |",
        "| `approval_threshold` | 202 pending | new limit above `approval_above` |",
        "| `large_increase` | 202 pending | increase above `approval_increase_pct` % of the current limit |",
        "| `default_allow` | 200 executed | otherwise |",
        "",
        "## Approvals",
        "",
        "- A change that needs approval (`rule_id: bulk_change`) returns 202 with "
        "`status: \"pending_approval\"` and does **not** touch the customer.",
        "- `approve` re-runs the recorded request through the gateway. It executes only if the customer "
        "is exactly as it was when approval was requested (else 409 `approval_stale`) and no blocking "
        "rule applies (an approval never overrides a block). The pending row itself becomes "
        "`executed` with `approval: {decision, role, at, note}`, and is then undoable as usual.",
        "- `reject` sets `status: \"denied\"`; the customer is unchanged.",
        "- A request is resolved once; later approve/reject calls → 409 `not_pending`.",
        "",
        "## PII",
        "",
        "- Customers have `name` and `phone` (synthetic). `GET /api/customers` is the FinCore UI and is "
        "unmasked.",
        "- Masked: everything the agent reads through its tools, free-text ledger args "
        "(`arguments.reason`) and approval notes, and the whole audit export "
        "(`name` → `M*** I***`, phone → only the last 2 digits).",
        "- `before`/`after` snapshots in `GET /api/actions` keep the full row, because Undo and the "
        "verifier compare exact rows.",
        "",
        "## Refresh advice",
        "",
        "- After **any** POST (update, Undo, reset), refetch `GET /api/customers` and `GET /api/actions`. "
        "Responses are not pushed; there is no websocket.",
        "- Never compute Undo eligibility in the UI: use `undo_available` / `undo_disabled_reason` from "
        "the latest `GET /api/actions`. They are evaluated against the current customer row on every read.",
        "- Polling is optional; if you poll, every 2–5 s is plenty for the demo.",
        "",
        "## When Undo is enabled",
        "",
        _undo_table(),
        "",
        "## Status codes (from the examples below)",
        "",
        _status_table(examples),
        "",
        "## Configuration (env vars)",
        "",
        "| Var | Default | Effect |",
        "|---|---|---|",
        "| `REWIND_DB_PATH` | `backend/data/rewind.db` | SQLite file |",
        "| `REWIND_DEMO_MODE` | off | `1` enables reset and the fixed demo clock (2026-10-22T12:00:00Z) |",
        "| `FRONTEND_ORIGIN` | `http://localhost:5173` | The only origin allowed by CORS |",
        "| `REWIND_RECENT_ACTIVITY_DAYS` | `90` | Archiving a customer active within N days is blocked |",
        "| `REWIND_BULK_APPROVAL_THRESHOLD` | `3` | Executed changes per `run_id` before approval is required |",
        "| `REWIND_FAULT_DISABLE_RECENT_ACTIVITY` | off | Injected fault (demo mode only): disables only "
        "`recent_activity`; affected actions carry `scenario: \"injected_fault\"` |",
        "",
        "## Routes, with real requests and responses",
        "",
        "IDs and timestamps are fixed for the docs; every other value is what the app returned.",
        "",
        "### `POST /api/demo/reset`",
        "",
        _example(by_id["reset"]),
        _example(by_id["reset_refused"]),
        "### `GET /api/customers`",
        "",
        _example(by_id["customers"]),
        "### `POST /api/tools/update-customer-status`",
        "",
        "Body: `account_id`, `new_status`, `reason` (required, 1–500 chars), optional "
        "`expected_version`, optional `run_id`. Unknown keys are rejected.",
        "",
        _example(by_id["update_executed"]),
        _example(by_id["update_blocked_protected"]),
        _example(by_id["update_blocked_recent"]),
        _example(by_id["update_no_change"]),
        _example(by_id["update_pending_approval"],
                 "Setup: one change already executed in `run-bulk`, with "
                 "`REWIND_BULK_APPROVAL_THRESHOLD=1`. No customer change is made; no approval UI exists yet."),
        _example(by_id["update_version_conflict"]),
        _example(by_id["update_not_found"]),
        _example(by_id["update_malformed"]),
        "### `POST /api/actions/{action_id}/approve` and `/reject` (approver)",
        "",
        "Optional body `{\"note\": \"...\"}` (≤500 chars, masked like ledger args). Returns the updated "
        "ActionRecord.",
        "",
        _example(by_id["approve_pending"]),
        _example(by_id["approve_not_pending"]),
        _example(by_id["reject_pending"], "Setup: `update_pending_approval_2` put ACC-105 in `pending_approval`."),
        "### `GET /api/audit/export` (auditor)",
        "",
        _example(trimmed_export, f"The real export had {export_data['action_count']} actions; only the "
                                 "first is shown."),
        _example(by_id["audit_export_forbidden"]),
        "### Role errors",
        "",
        _example(by_id["update_role_missing"]),
        _example(by_id["update_role_forbidden"]),
        "### `GET /api/credit-lines`",
        "",
        _example(by_id["credit_lines"]),
        "### `POST /api/tools/update-credit-limit` (operator)",
        "",
        "Body: `account_id`, `new_limit` (whole INR, 0..1,000,000,000), `reason` (required), optional "
        "`expected_version`, optional `run_id`. Approve, reject and Undo work exactly as for status "
        "changes; Undo evidence uses `current_credit_line` / `other_credit_lines`.",
        "",
        _example(by_id["credit_update_executed"]),
        _example(by_id["credit_undo"]),
        _example(by_id["credit_blocked"]),
        _example(by_id["credit_pending"]),
        _example(by_id["credit_not_found"]),
        "### `GET /api/actions`",
        "",
        _example(trimmed, f"The real response had {count} items; only the first (newest) is shown."),
        "### `GET /api/actions/{action_id}`",
        "",
        _example(by_id["action_undo_available"]),
        _example(by_id["action_undo_conflict_predicted"],
                 "Setup: ACC-105 was archived (`update_executed_2`), then edited again by another request."),
        "### `POST /api/actions/{action_id}/undo`",
        "",
        _example(by_id["undo_verified"]),
        _example(by_id["undo_repeat"]),
        _example(by_id["undo_conflict"]),
        _example(by_id["undo_not_undoable"]),
        _example(by_id["undo_not_found"]),
        "### `GET /api/metrics`",
        "",
        "Measured from the ledger and the last saved lab report (`python -m backend.scenarios` or "
        "`POST /api/lab/run` writes `backend/data/lab_report.json`). Values that cannot be measured "
        "are `null`. "
        "`assumptions` lists every definition used; read it before quoting a number. "
        "`false_verified.count` must be 0.",
        "",
        _example(by_id["metrics"], "Timings in this example come from the docs' fixed clock (one second "
                                   "per ledger write), not from real elapsed time."),
        "### `POST /api/agent/runs` (operator)",
        "",
        "Body: `instruction` (1-2000 chars), `mode` (`live` | `replay`), optional `scenario` "
        "(replay only: which `backend/replays/<scenario>.json` script; default `archive_inactive`).",
        "",
        "- **live**: an LLM tool loop. Configure `LLM_PROVIDER=anthropic`, `LLM_API_KEY`, optional "
        "`LLM_MODEL` (default `claude-opus-5-5`) and `LLM_TIMEOUT_SECONDS` (default 60). The model sees "
        "only `list_customers`, `get_customer`, `get_inactivity_report` (PII-masked) and "
        "`update_customer_status`; every change goes through the gateway.",
        "- **replay**: plays a recorded, versioned script through the same gateway, so policy, ledger "
        "and verifier all run for real. It is **never** a live model run: the response and every "
        "ledger row say `mode: \"replay\"`.",
        "- Max 8 tool calls. An unknown tool or malformed arguments is rejected and the error is fed "
        "back to the model once; a second one stops the run. Any change the gateway does not execute "
        "(blocked, pending approval, conflict, not found) stops the run.",
        "- `final_summary` and `action_ids` are built from this run's ledger rows, not from the model. "
        "`model_message` (live only) is the model's own last text, shown for context and unverified.",
        "- `status`: `completed` | `stopped_blocked` | `stopped_invalid_tool_call` | "
        "`step_limit_reached` | `provider_failed` (HTTP 502, `error.code: provider_failed`; the "
        "ledger holds exactly what executed before the failure).",
        "",
        _example(by_id["agent_run_replay"], "Setup: `POST /api/demo/reset` just before."),
        _example(by_id["agent_run_replay_blocked"]),
        _example(by_id["agent_run_live_unconfigured"]),
        "### `GET /api/runs/{run_id}/evidence`",
        "",
        "Everything recorded about one run, read back from SQLite: the instruction and steps (names "
        "and phones masked), and for each ledger row its tool args, policy decision, before/after, "
        "pre-commit guard, post-action check and `flag`, every recovery attempt with its verification "
        "checks, and the current (`final_state`) row of everything the run touched. "
        "`recorded_run: false` means the `run_id` only exists on ledger rows written outside an agent run.",
        "",
        _example(by_id["run_evidence"]),
        _example(by_id["run_evidence_not_found"]),
        "### `POST /api/lab/run` (operator)",
        "",
        "Runs the scenario lab (`backend/scenarios.py`) in a separate process on a throwaway database "
        "(the app's database is untouched) and returns the report it saves to "
        "`backend/data/lab_report.json`: `{ran_at, filter, isolation, totals: {total, passed, failed, "
        "injected_faults}, scenarios: [{name, label, adapter, description, expected, actual, ok, "
        "mismatches}]}`. Optional query `filter` (`[a-z0-9_]`, substring of scenario names). "
        "Takes several seconds. `totals.failed` must be 0. Same as `python -m backend.scenarios`.",
        "",
        "### Post-action eligibility check",
        "",
        "Every executed archive (`update_customer_status` to `Archived`) gets `post_action_check`: "
        "after commit, on a new connection, the archive rule (inactivity threshold, not protected) "
        "is re-checked against the customer's current row, ignoring injected faults. Verdict "
        "`eligible` or `flagged_unsafe`; a flagged action also has `flag: \"flagged_unsafe\"`. "
        "Show it as \"REWIND detected an unsafe change\" and offer Undo. Other actions have "
        "`post_action_check: null`.",
        "",
        "### Any unknown route",
        "",
        _example(by_id["route_not_found"]),
        "## Changes since the last version (agent runs)",
        "",
        "- New: `GET /api/runs/{run_id}/evidence` (was 501) and `POST /api/lab/run`.",
        "- Update responses and ActionRecord gain `post_action_check`; ActionRecord gains `flag` and "
        "`snapshot_sha256` (digest of the stored before/after, checked before every restore).",
        "- Undo verification gains the `ledger_snapshot_intact` check.",
        "- `/api/metrics`: `flagged` now also counts `flagged_unsafe` actions; the lab section reads "
        "`backend/data/lab_report.json`.",
        "- Local DBs from before this change need one `POST /api/demo/reset`.",
        "",
    ]
    return "\n".join(sections)
