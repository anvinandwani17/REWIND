"""Regression tests for the security/honesty review. Each test reproduces a finding."""
import json
import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agent
import db
import gateway
import ledger
import main
import policies

BACKEND = Path(__file__).resolve().parents[1]
OPERATOR = {"X-Role": "operator", "X-Role-Key": "demo-operator-key"}
APPROVER = {"X-Role": "approver", "X-Role-Key": "demo-approver-key"}
STATUS = "/api/tools/update-customer-status"
CREDIT = "/api/tools/update-credit-limit"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "review.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    for name in ("REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
                 "REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "REWIND_ROLE_KEYS", "REWIND_POLICIES_PATH",
                 "REWIND_LAB_RESULTS_PATH"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(main.app, headers=OPERATOR) as c:
        yield c


def customers():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY account_id")]


# H1: rewriting the stored before-state AND its digest used to yield a false "verified" ----------

def test_forged_before_state_with_recomputed_digest_is_never_verified(client):
    action_id = client.post(STATUS, json={"account_id": "ACC-102", "new_status": "Archived",
                                          "reason": "x"}).json()["data"]["action_id"]
    with db.transaction() as conn:
        row = conn.execute("SELECT before_json, after_json FROM actions WHERE action_id = ?",
                           (action_id,)).fetchone()
        before, after = json.loads(row[0]), json.loads(row[1])
        before["status"] = "Archived"  # forged: "it was already archived"
        conn.execute("UPDATE actions SET before_json = ?, snapshot_sha256 = ? WHERE action_id = ?",
                     (json.dumps(before), ledger.snapshot_digest(before, after), action_id))
    state = customers()

    response = client.post(f"/api/actions/{action_id}/undo")
    data = response.json()["data"]
    assert response.status_code == 500
    assert data["verification"]["verdict"] != "verified"
    assert [c["id"] for c in data["pre_commit_guard"]["checks"] if not c["passed"]] == [
        "before_differs_from_after"]  # the digest matches, so that check alone cannot catch it
    assert customers() == state


def test_digest_check_says_what_it_cannot_prove(client):
    action_id = client.post(STATUS, json={"account_id": "ACC-102", "new_status": "Archived",
                                          "reason": "x"}).json()["data"]["action_id"]
    checks = client.post(f"/api/actions/{action_id}/undo").json()["data"]["verification"]["checks"]
    digest = next(c for c in checks if c["id"] == "snapshot_matches_stored_digest")
    assert "same ledger row" in digest["description"] and "not detectable" in digest["description"]
    assert not any(c["id"] == "ledger_snapshot_intact" for c in checks)


# H2: Undo wrote customer rows from recovery.py, outside the gateway -------------------------------

WRITE_SQL = re.compile(r"(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+(customers|credit_lines)\b", re.IGNORECASE)


def test_only_the_gateway_writes_adapter_tables():
    for path in BACKEND.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path.name != "gateway.py":
            assert not re.search(r"\.(apply|restore)\(", source), f"{path.name} calls an adapter write"
        if path.name not in ("db.py", "scenarios.py"):  # seed helpers; labelled lab fault injection
            assert not WRITE_SQL.search(source), f"{path.name} writes an adapter table directly"
        if path.name not in ("db.py", "demo.py"):
            assert "insert_customers(" not in source and "insert_credit_lines(" not in source


def test_apply_restore_refuses_to_run_outside_a_transaction(client):
    adapter = __import__("adapters").for_tool("update_customer_status")
    with db.connect() as conn:  # autocommit, no BEGIN IMMEDIATE
        with pytest.raises(RuntimeError, match="BEGIN IMMEDIATE"):
            gateway.apply_restore(conn, adapter, {}, {"account_id": "ACC-102"})


# M1: the public demo keys used to work with demo mode off -----------------------------------------

def test_demo_keys_are_refused_outside_demo_mode(client, monkeypatch):
    monkeypatch.delenv("REWIND_DEMO_MODE")
    state = customers()
    response = client.post(STATUS, json={"account_id": "ACC-102", "new_status": "Archived", "reason": "x"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "role_keys_not_configured"
    assert customers() == state

    monkeypatch.setenv("REWIND_ROLE_KEYS", "operator=real-op,approver=real-ap,auditor=real-au")
    assert client.post(STATUS, json={"account_id": "ACC-102", "new_status": "Archived",
                                     "reason": "x"}).status_code == 401  # demo key still refused
    configured = client.post(STATUS, headers={"X-Role": "operator", "X-Role-Key": "real-op"},
                             json={"account_id": "ACC-102", "new_status": "Archived", "reason": "x"})
    assert configured.status_code == 200


# M2: a failed lab run used to return stderr (tracebacks, paths) to the client ------------------------

def test_lab_failure_does_not_leak_stderr(client, monkeypatch):
    def broken_lab(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="",
                                           stderr='Traceback (most recent call last):\n  File "C:\\secret\\x.py"')
    monkeypatch.setattr(main.subprocess, "run", broken_lab)
    response = client.post("/api/lab/run")
    assert response.status_code == 500
    assert response.json()["error"] == {"code": "lab_failed", "message": "The scenario lab did not produce a report",
                                        "details": {"exit_code": 1}}
    assert "Traceback" not in response.text and "secret" not in response.text


# M3: policy-load errors and the metrics lab path used to expose server file paths ---------------------

def test_policy_load_error_has_no_server_path(client, tmp_path, monkeypatch):
    missing = tmp_path / "nowhere" / "policies.yaml"
    monkeypatch.setenv("REWIND_POLICIES_PATH", str(missing))
    _, error = policies.load_credit_limit_policy()
    assert error == "FileNotFoundError"
    response = client.post(CREDIT, json={"account_id": "ACC-102", "new_limit": 10000, "reason": "x"})
    assert response.json()["data"]["rule_id"] == "policy_config_invalid"
    assert str(tmp_path.name) not in response.text and "nowhere" not in response.text


def test_metrics_shows_only_the_lab_file_name(client, tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_LAB_RESULTS_PATH", str(tmp_path / "private-dir" / "lab_report.json"))
    lab = client.get("/api/metrics").json()["data"]["lab"]
    assert lab == {"available": False, "file": "lab_report.json"}
    (tmp_path / "private-dir").mkdir()
    (tmp_path / "private-dir" / "lab_report.json").write_text("{not json", encoding="utf-8")
    response = client.get("/api/metrics")
    assert response.json()["data"]["lab"]["error"] == "JSONDecodeError"
    assert "private-dir" not in response.text


# M4: a provider that did not say otherwise used to be labelled "live" ----------------------------------

def test_providers_are_replay_unless_they_really_call_a_model(client):
    class Unlabelled(agent.Provider):
        def __init__(self):
            self.turns = [agent.Turn(tool_calls=[agent.ToolCall("c1", "update_customer_status", {
                "account_id": "ACC-102", "new_status": "Archived", "reason": "x"})]), agent.Turn(text="done")]

        def next_turn(self, system, transcript, tool_definitions):
            return self.turns.pop(0)

    assert agent.AnthropicProvider.live is True
    _, body = agent.run_live("archive", Unlabelled())
    assert body["data"]["mode"] == "replay"
    with db.connect() as conn:
        modes = {r[0] for r in conn.execute("SELECT mode FROM actions WHERE run_id = ?",
                                            (body["data"]["run_id"],))}
    assert modes == {"replay"}


# M5: approving one pending action while passing different arguments ----------------------------------

def test_approval_executes_only_what_was_approved(client):
    pending = client.post(CREDIT, json={"account_id": "ACC-105", "new_limit": 60000,
                                        "reason": "x"}).json()["data"]
    assert pending["status"] == "pending_approval"

    status, body = gateway.execute("update_credit_limit",
                                   {"account_id": "ACC-105", "new_limit": 500000, "reason": "x"},
                                   actor="approver", approves=pending["action_id"])
    assert (status, body["error"]["code"]) == (409, "approval_mismatch")
    with db.connect() as conn:
        assert conn.execute("SELECT credit_limit FROM credit_lines WHERE account_id = 'ACC-105'").fetchone()[0] == 30000
    assert client.get(f"/api/actions/{pending['action_id']}").json()["data"]["status"] == "pending_approval"

    approved = client.post(f"/api/actions/{pending['action_id']}/approve", headers=APPROVER)
    assert approved.status_code == 200
    with db.connect() as conn:
        assert conn.execute("SELECT credit_limit FROM credit_lines WHERE account_id = 'ACC-105'").fetchone()[0] == 60000
