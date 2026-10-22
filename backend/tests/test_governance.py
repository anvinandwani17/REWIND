"""Role boundaries, the approval gate, and PII masking."""
import json

import pytest
from fastapi.testclient import TestClient

import db
import main
import tools

UPDATE = "/api/tools/update-customer-status"
KEYS = {"operator": "demo-operator-key", "approver": "demo-approver-key", "auditor": "demo-auditor-key"}


def as_role(role):
    return {"X-Role": role, "X-Role-Key": KEYS[role]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "gov.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    for name in ("REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
                 "REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "REWIND_ROLE_KEYS"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(main.app) as c:  # no default role: every call states its role
        yield c


def snapshot():
    with db.connect() as conn:
        return (
            [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY account_id")],
            [dict(r) for r in conn.execute("SELECT * FROM actions ORDER BY action_id")],
        )


def archive(client, account_id, role="operator", run_id="run-1", reason="Inactive"):
    return client.post(UPDATE, headers=as_role(role), json={
        "account_id": account_id, "new_status": "Archived", "reason": reason, "run_id": run_id})


def customer(client, account_id):
    return next(c for c in client.get("/api/customers").json()["data"] if c["account_id"] == account_id)


def pending_action(client, monkeypatch):
    """One executed + one pending_approval change in run-bulk (threshold 1)."""
    monkeypatch.setenv("REWIND_BULK_APPROVAL_THRESHOLD", "1")
    assert archive(client, "ACC-108", run_id="run-bulk").status_code == 200
    response = archive(client, "ACC-102", run_id="run-bulk")
    assert response.status_code == 202
    return response.json()["data"]["action_id"]


# --- authentication-ish failures ---------------------------------------------------------

@pytest.mark.parametrize("headers, code", [
    ({}, "role_required"),
    ({"X-Role": "operator"}, "role_required"),
    ({"X-Role": "operator", "X-Role-Key": "wrong"}, "invalid_role_key"),
    ({"X-Role": "operator", "X-Role-Key": "demo-approver-key"}, "invalid_role_key"),
    ({"X-Role": "admin", "X-Role-Key": "anything"}, "invalid_role_key"),
])
def test_missing_or_bad_role_credentials_are_401_and_write_nothing(client, headers, code):
    before = snapshot()
    response = client.post(UPDATE, headers=headers, json={
        "account_id": "ACC-102", "new_status": "Archived", "reason": "x"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == code
    assert snapshot() == before


def test_role_keys_are_configurable(client, monkeypatch):
    monkeypatch.setenv("REWIND_ROLE_KEYS", "operator=op-secret,approver=ap-secret,auditor=au-secret")
    assert archive(client, "ACC-102").status_code == 401  # demo default no longer accepted
    response = client.post(UPDATE, headers={"X-Role": "operator", "X-Role-Key": "op-secret"}, json={
        "account_id": "ACC-102", "new_status": "Archived", "reason": "x"})
    assert response.status_code == 200


# --- role matrix: each route allows exactly one role ---------------------------------------

def routes(client, monkeypatch):
    pending_id = pending_action(client, monkeypatch)
    executed_id = client.get("/api/actions").json()["data"][-1]["id"]
    return {
        "operator": [("POST", UPDATE, {"account_id": "ACC-105", "new_status": "Archived", "reason": "x"}),
                     ("POST", f"/api/actions/{executed_id}/undo", None),
                     ("POST", "/api/demo/reset", None),
                     ("POST", "/api/agent/runs", None)],
        "approver": [("POST", f"/api/actions/{pending_id}/approve", None),
                     ("POST", f"/api/actions/{pending_id}/reject", None)],
        "auditor": [("GET", "/api/audit/export", None)],
    }


@pytest.mark.parametrize("caller", ["operator", "approver", "auditor"])
def test_role_boundaries(client, monkeypatch, caller):
    matrix = routes(client, monkeypatch)
    before = snapshot()
    for owner, calls in matrix.items():
        if owner == caller:
            continue
        for method, path, body in calls:
            response = client.request(method, path, headers=as_role(caller), json=body)
            assert response.status_code == 403, (caller, method, path)
            error = response.json()["error"]
            assert error["code"] == "role_forbidden"
            assert error["details"] == {"required": [owner], "role": caller}
    assert snapshot() == before  # forbidden calls change nothing


def test_reads_are_open_to_the_dashboard(client):
    for path in ("/api/customers", "/api/actions"):
        assert client.get(path).status_code == 200


def test_operator_can_run_tools_and_undo(client):
    response = archive(client, "ACC-102")
    assert response.status_code == 200
    action_id = response.json()["data"]["action_id"]
    assert client.get(f"/api/actions/{action_id}").json()["data"]["actor"] == "operator"

    undo = client.post(f"/api/actions/{action_id}/undo", headers=as_role("operator"))
    assert undo.status_code == 200
    [undo_row] = [a for a in client.get("/api/actions").json()["data"] if a["tool_name"] == "undo"]
    assert undo_row["actor"] == "operator"


# --- approval gate --------------------------------------------------------------------------

def test_require_approval_does_not_execute_until_approved(client, monkeypatch):
    action_id = pending_action(client, monkeypatch)
    pending_customer = customer(client, "ACC-102")
    assert (pending_customer["status"], pending_customer["version"]) == ("Active", 1)
    record = client.get(f"/api/actions/{action_id}").json()["data"]
    assert (record["status"], record["decision"], record["undo_available"]) == (
        "pending_approval", "require_approval", False)

    response = client.post(f"/api/actions/{action_id}/approve", headers=as_role("approver"),
                           json={"note": "Reviewed with Meera Iyer on +91 90000 00102"})
    assert response.status_code == 200
    approved = response.json()["data"]
    assert approved["id"] == action_id
    assert (approved["status"], approved["recovery_state"]) == ("executed", "available")
    assert approved["approval"]["decision"] == "approved"
    assert approved["approval"]["role"] == "approver"
    assert "Meera" not in approved["approval"]["note"] and "00102" not in approved["approval"]["note"]
    assert approved["verification"]["verdict"] == "verified"
    assert approved["undo_available"] is True
    assert (customer(client, "ACC-102")["status"], customer(client, "ACC-102")["version"]) == ("Archived", 2)

    # Still one ledger row for the request: approval resolves it, it does not add one.
    assert len([a for a in client.get("/api/actions").json()["data"] if a["account_id"] == "ACC-102"]) == 1

    # An approved action is undoable like any other.
    assert client.post(f"/api/actions/{action_id}/undo", headers=as_role("operator")).status_code == 200


def test_reject_leaves_customer_unchanged(client, monkeypatch):
    action_id = pending_action(client, monkeypatch)
    before = customer(client, "ACC-102")
    response = client.post(f"/api/actions/{action_id}/reject", headers=as_role("approver"),
                           json={"note": "Not this week"})
    assert response.status_code == 200
    record = response.json()["data"]
    assert (record["status"], record["approval"]["decision"]) == ("denied", "rejected")
    assert record["undo_available"] is False
    assert customer(client, "ACC-102") == before


def test_approval_cannot_be_resolved_twice(client, monkeypatch):
    action_id = pending_action(client, monkeypatch)
    assert client.post(f"/api/actions/{action_id}/approve", headers=as_role("approver")).status_code == 200
    state = snapshot()
    for verb in ("approve", "reject"):
        again = client.post(f"/api/actions/{action_id}/{verb}", headers=as_role("approver"))
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "not_pending"
    assert snapshot() == state


def test_only_pending_actions_can_be_approved(client):
    executed = archive(client, "ACC-102").json()["data"]["action_id"]
    blocked = archive(client, "ACC-103").json()["data"]["action_id"]
    for action_id in (executed, blocked):
        response = client.post(f"/api/actions/{action_id}/approve", headers=as_role("approver"))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "not_pending"
    missing = client.post("/api/actions/nope/approve", headers=as_role("approver"))
    assert missing.status_code == 404


def test_stale_approval_writes_nothing(client, monkeypatch):
    action_id = pending_action(client, monkeypatch)
    with db.transaction() as conn:
        conn.execute("UPDATE customers SET name = 'Edited', version = version + 1 WHERE account_id = 'ACC-102'")
    before = snapshot()

    response = client.post(f"/api/actions/{action_id}/approve", headers=as_role("approver"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_stale"
    assert snapshot() == before
    assert client.get(f"/api/actions/{action_id}").json()["data"]["status"] == "pending_approval"


def test_approval_never_overrides_a_block(client, monkeypatch):
    action_id = pending_action(client, monkeypatch)
    # Same customer row, but the recent-activity window now covers it.
    monkeypatch.setenv("REWIND_RECENT_ACTIVITY_DAYS", "100000")
    before = snapshot()
    response = client.post(f"/api/actions/{action_id}/approve", headers=as_role("approver"))
    assert response.status_code == 409
    assert response.json()["error"]["details"]["rule_id"] == "recent_activity"
    assert snapshot() == before


# --- PII masking -----------------------------------------------------------------------------

PII_REASON = "Meera Iyer asked to close; call her on +91 98765 43210 after 2025-03-10"


def test_ledger_args_mask_names_and_phones(client):
    action_id = archive(client, "ACC-102", reason=PII_REASON).json()["data"]["action_id"]
    with db.connect() as conn:
        raw_args = conn.execute("SELECT args_json FROM actions WHERE action_id = ?", (action_id,)).fetchone()[0]
    reason = json.loads(raw_args)["reason"]
    assert "Meera" not in reason and "Iyer" not in reason
    assert "98765" not in reason and reason.count("*") >= 8
    assert "2025-03-10" in reason          # dates are not phones
    assert json.loads(raw_args)["account_id"] == "ACC-102"


def test_agent_context_is_masked(client):
    names = {c["name"] for c in client.get("/api/customers").json()["data"]}
    phones = {c["phone"] for c in client.get("/api/customers").json()["data"]}

    agent_view = json.dumps([
        tools.list_customers(),
        tools.get_customer("ACC-102"),
        tools.get_inactivity_report(min_inactive_days=365),
        tools.update_customer_status("ACC-105", "Archived", "Inactive", run_id="agent-run")[1],
    ])
    for secret in names | phones:
        assert secret not in agent_view
    assert tools.get_customer("ACC-102")["name"] == "M*** I***"
    assert tools.get_customer("ACC-102")["phone"].endswith("02")
    assert tools.get_customer("ACC-102")["account_id"] == "ACC-102"


def test_dashboard_customers_stay_unmasked(client):
    assert customer(client, "ACC-102")["name"] == "Meera Iyer"


def test_audit_export_is_masked_and_complete(client):
    archive(client, "ACC-102", reason=PII_REASON)
    archive(client, "ACC-103")
    response = client.get("/api/audit/export", headers=as_role("auditor"))
    assert response.status_code == 200
    export = response.json()["data"]
    assert export["pii_masked"] is True
    assert export["exported_by_role"] == "auditor"
    assert export["integrity_check"] == "ok"
    assert export["action_count"] == 2 == len(export["actions"])

    text = json.dumps(export)
    for c in client.get("/api/customers").json()["data"]:
        assert c["name"] not in text and c["phone"] not in text
