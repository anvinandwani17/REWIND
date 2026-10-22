"""API contract: envelope, locked response shapes, CORS, and generated docs.

To accept an intended shape/doc change:
    $env:REWIND_REGENERATE = "1"; python -m pytest -q; Remove-Item Env:REWIND_REGENERATE
then review the diff of the snapshot and docs files.
"""
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from api_examples import make_deterministic, render, run_examples, shape

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = Path(__file__).with_name("snapshots") / "response_shapes.json"
DOCS = ROOT / "docs" / "backend-api.md"
REGENERATE = os.environ.get("REWIND_REGENERATE") == "1"


@pytest.fixture
def examples(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "contract.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    # Fixed, never-created path so the generated docs don't depend on a local lab run.
    monkeypatch.setenv("REWIND_LAB_RESULTS_PATH", "no-lab-run-saved/lab_last_run.json")
    for name in ("REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
                 "REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "REWIND_ROLE_KEYS", "REWIND_POLICIES_PATH",
                 "LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    make_deterministic(monkeypatch)
    with TestClient(main.app) as client:
        yield run_examples(client, monkeypatch)


def check_generated(path, text):
    if REGENERATE:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == text, (
        f"{path.name} is out of date; regenerate (see module docstring) and review the diff"
    )


def test_every_response_uses_the_envelope(examples):
    for ex in examples:
        body = ex["response"]
        assert set(body) == {"data", "error"}, ex["id"]
        is_2xx = 200 <= ex["status"] < 300
        assert (body["error"] is None) == is_2xx, ex["id"]
        if body["error"] is not None:
            assert set(body["error"]) == {"code", "message", "details"}, ex["id"]


def test_response_shapes_are_locked(examples):
    shapes = {ex["id"]: {"status": ex["status"], "shape": shape(ex["response"])} for ex in examples}
    check_generated(SNAPSHOT, json.dumps(shapes, indent=2, sort_keys=True) + "\n")


def test_docs_are_generated_from_the_running_app(examples):
    check_generated(DOCS, render(examples))


def test_documented_status_codes(examples):
    codes = {ex["id"]: (ex["status"], (ex["response"]["error"] or {}).get("code")) for ex in examples}
    assert codes == {
        "reset": (200, None),
        "customers": (200, None),
        "update_executed": (200, None),
        "action_undo_available": (200, None),
        "undo_verified": (200, None),
        "undo_repeat": (200, None),
        "update_blocked_protected": (403, "policy_blocked"),
        "update_blocked_recent": (403, "policy_blocked"),
        "update_no_change": (409, "policy_blocked"),
        "update_version_conflict": (409, "version_conflict"),
        "update_not_found": (404, "customer_not_found"),
        "update_malformed": (422, "invalid_arguments"),
        "update_executed_2": (200, None),
        "update_newer_edit": (200, None),
        "action_undo_conflict_predicted": (200, None),
        "undo_conflict": (409, "undo_conflict"),
        "undo_not_undoable": (409, "not_undoable"),
        "undo_not_found": (404, "action_not_found"),
        "update_pending_approval": (202, None),
        "approve_pending": (200, None),
        "approve_not_pending": (409, "not_pending"),
        "update_pending_approval_2": (202, None),
        "reject_pending": (200, None),
        "update_role_missing": (401, "role_required"),
        "update_role_forbidden": (403, "role_forbidden"),
        "audit_export": (200, None),
        "audit_export_forbidden": (403, "role_forbidden"),
        "credit_lines": (200, None),
        "credit_update_executed": (200, None),
        "credit_undo": (200, None),
        "credit_blocked": (403, "policy_blocked"),
        "credit_pending": (202, None),
        "credit_not_found": (404, "credit_line_not_found"),
        "actions": (200, None),
        "metrics": (200, None),
        "agent_run_replay": (200, None),
        "agent_run_replay_blocked": (200, None),
        "agent_run_live_unconfigured": (503, "provider_not_configured"),
        "run_evidence_reserved": (501, "not_implemented"),
        "route_not_found": (404, "route_not_found"),
        "reset_refused": (403, "demo_mode_off"),
    }


def test_undo_fields_follow_current_state(examples):
    by_id = {ex["id"]: ex["response"]["data"] for ex in examples}
    assert by_id["action_undo_available"]["undo_available"] is True
    assert by_id["action_undo_available"]["undo_disabled_reason"] is None

    predicted = by_id["action_undo_conflict_predicted"]
    assert predicted["undo_available"] is False
    assert "newer edit" in predicted["undo_disabled_reason"]

    actions = by_id["actions"]
    assert all(set(a) >= {"undo_available", "undo_disabled_reason"} for a in actions)
    for action in actions:
        assert (action["undo_disabled_reason"] is None) == action["undo_available"]
        if action["tool_name"] == "undo" or action["status"] != "executed":
            assert action["undo_available"] is False


def test_cors_allows_only_the_frontend_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "cors.db"))
    preflight = {"Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "content-type, x-role, x-role-key"}
    with TestClient(main.app) as client:
        allowed = client.options("/api/customers",
                                 headers={"Origin": main.FRONTEND_ORIGIN, **preflight})
        assert allowed.status_code == 200
        assert allowed.headers.get("access-control-allow-origin") == main.FRONTEND_ORIGIN
        assert "x-role-key" in allowed.headers.get("access-control-allow-headers", "").lower()

        other = client.get("/api/customers", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in other.headers


def test_default_frontend_origin():
    if "FRONTEND_ORIGIN" not in os.environ:
        assert main.FRONTEND_ORIGIN == "http://localhost:5173"
