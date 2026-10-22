"""Real request/response examples, produced by calling the app through TestClient.

Used by test_contract.py to lock response shapes and to generate
docs/backend-api.md, so the docs can never drift from the code.
IDs and timestamps are made deterministic; everything else is real output.
"""
import itertools
import json
import re
from datetime import timedelta

import demo
import gateway
import ledger
import recovery

UPDATE = "/api/tools/update-customer-status"


def make_deterministic(monkeypatch):
    ids = itertools.count(1)
    ticks = itertools.count(1)

    def fake_uuid():
        return f"00000000-0000-4000-8000-{next(ids):012d}"

    def fake_now():
        return (demo.DEMO_NOW + timedelta(seconds=next(ticks))).isoformat()

    for module in (gateway, recovery):
        monkeypatch.setattr(module, "uuid4", fake_uuid)
    monkeypatch.setattr(ledger, "now", fake_now)
    monkeypatch.setattr(recovery, "now", fake_now)


def run_examples(client, monkeypatch):
    """Walk the demo story once; return [{id, title, method, path, body, status, response}]."""
    examples = []

    def call(example_id, title, method, path, body=None):
        response = client.request(method, path, json=body)
        examples.append({
            "id": example_id, "title": title, "method": method, "path": path,
            "body": body, "status": response.status_code, "response": response.json(),
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
    client.post(UPDATE, json=update("ACC-108", "Archived", "Inactive for 150 days", run_id="run-bulk"))
    call("update_pending_approval", "Bulk run over threshold (threshold set to 1)", "POST", UPDATE,
         update("ACC-102", "Archived", "Inactive for 591 days", run_id="run-bulk"))
    monkeypatch.delenv("REWIND_BULK_APPROVAL_THRESHOLD")

    call("actions", "List actions", "GET", "/api/actions")
    call("agent_runs_reserved", "Reserved: start an agent run", "POST", "/api/agent/runs")
    call("run_evidence_reserved", "Reserved: run evidence", "GET", "/api/runs/run-1/evidence")
    call("route_not_found", "Unknown route", "GET", "/api/nope")

    monkeypatch.delenv("REWIND_DEMO_MODE")
    call("reset_refused", "Reset with demo mode off", "POST", "/api/demo/reset")
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
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

    sections = [
        "# REWIND backend API",
        "",
        "<!-- Generated by backend/tests/api_examples.py from real TestClient calls. "
        "Do not edit by hand: run `$env:REWIND_REGENERATE = \"1\"; python -m pytest -q` "
        "then remove the variable. -->",
        "",
        "Scope: Undo supports **customer status changes only**. This is not a universal rollback.",
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
        "### Reserved (not implemented yet)",
        "",
        _example(by_id["agent_runs_reserved"]),
        _example(by_id["run_evidence_reserved"]),
        "### Any unknown route",
        "",
        _example(by_id["route_not_found"]),
        "## Changes since the last version (Priority-0 item 4)",
        "",
        "- **Breaking:** every response is wrapped in `{data, error}`. `GET /api/customers` and "
        "`GET /api/actions` now return their arrays under `data`.",
        "- **Breaking:** errors are `{data: null, error: {...}}` (was `{error: {...}}`). Policy blocks "
        "(`policy_blocked`), Undo conflicts (`undo_conflict`), and unverified writes now also set `error`.",
        "- New on every action: `undo_available`, `undo_disabled_reason`.",
        "- New: `POST /api/agent/runs` and `GET /api/runs/{run_id}/evidence` reserved (501).",
        "- Unknown routes and 405s use the envelope (`route_not_found`, `method_not_allowed`).",
        "- CORS enabled for `FRONTEND_ORIGIN` only.",
        "",
    ]
    return "\n".join(sections)
