"""Both recovery adapters satisfy the same contract; credit-limit policy comes from YAML."""
import inspect
import json

import pytest
from fastapi.testclient import TestClient

import adapters
import db
import main
import verification

OPERATOR = {"X-Role": "operator", "X-Role-Key": "demo-operator-key"}
APPROVER = {"X-Role": "approver", "X-Role-Key": "demo-approver-key"}

# Same scenario for each adapter: change a target, a second actor edits it again,
# and an untouched row in the same table is used for tamper checks.
CASES = {
    "customer_status": {
        "tool": "update_customer_status", "route": "/api/tools/update-customer-status",
        "table": "customers", "field": "status", "arg": "new_status",
        "target": "ACC-102", "old": "Active", "new": "Archived", "newer": "Active",
        "other": "ACC-108", "tamper_sql": "UPDATE customers SET name = 'Tampered' WHERE account_id = ?",
    },
    "credit_limit": {
        "tool": "update_credit_limit", "route": "/api/tools/update-credit-limit",
        "table": "credit_lines", "field": "credit_limit", "arg": "new_limit",
        "target": "ACC-102", "old": 20000, "new": 10000, "newer": 15000,
        "other": "ACC-104", "tamper_sql": "UPDATE credit_lines SET currency = 'USD' WHERE account_id = ?",
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "adapters.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    for name in ("REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
                 "REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "REWIND_ROLE_KEYS", "REWIND_POLICIES_PATH"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(main.app, headers=OPERATOR) as c:
        yield c


def rows(table):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY account_id")]


def row(table, account_id):
    return next(r for r in rows(table) if r["account_id"] == account_id)


def change(client, case, value, account_id=None, **extra):
    return client.post(case["route"], json={
        "account_id": account_id or case["target"], case["arg"]: value,
        "reason": "adapter contract test", **extra})


def undo(client, action_id):
    return client.post(f"/api/actions/{action_id}/undo")


# --- the interface ---------------------------------------------------------------------

def test_both_adapters_implement_the_whole_interface():
    assert set(adapters.ADAPTERS) == {"update_customer_status", "update_credit_limit"}
    for adapter in adapters.ADAPTERS.values():
        assert isinstance(adapter, adapters.Adapter)
        assert not inspect.isabstract(type(adapter))
        for method in ("snapshot", "snapshot_unrelated", "apply", "restore", "verify",
                       "classify_reversibility", "evaluate", "new_value"):
            assert callable(getattr(adapter, method))


# --- the same contract for every adapter -------------------------------------------------

@pytest.fixture(params=sorted(CASES))
def case(request):
    return CASES[request.param]


def test_apply_changes_exactly_one_row_and_is_logged(client, case):
    before_rows = rows(case["table"])
    response = change(client, case, case["new"])
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["status"] == "executed"
    assert data["verification"]["verdict"] == "verified"

    after_rows = rows(case["table"])
    changed = [a for b, a in zip(before_rows, after_rows) if a != b]
    assert [c["account_id"] for c in changed] == [case["target"]]
    assert changed[0][case["field"]] == case["new"]
    assert changed[0]["version"] == 2

    action = client.get(f"/api/actions/{data['action_id']}").json()["data"]
    assert action["tool_name"] == case["tool"]
    assert (action["before"][case["field"]], action["after"][case["field"]]) == (case["old"], case["new"])
    assert (action["before_version"], action["after_version"]) == (1, 2)
    assert action["undo_available"] is True


def test_undo_restores_and_post_commit_verifies(client, case):
    action_id = change(client, case, case["new"]).json()["data"]["action_id"]
    response = undo(client, action_id)
    assert response.status_code == 200, response.json()
    verdict = response.json()["data"]["verification"]
    assert verdict["verdict"] == "verified"
    assert {c["id"] for c in verdict["checks"]} == {
        "restored_value", "version_transition", "ledger_consistency", "db_integrity",
        "unrelated_rows_unchanged"}
    assert case["table"] in verdict["scope"]
    target = row(case["table"], case["target"])
    assert (target[case["field"]], target["version"]) == (case["old"], 3)

    again = undo(client, action_id)
    assert again.status_code == 200
    assert again.json()["data"]["already_recovered"] is True
    assert row(case["table"], case["target"])["version"] == 3


def test_undo_after_newer_edit_is_a_conflict_and_keeps_the_edit(client, case):
    action_id = change(client, case, case["new"]).json()["data"]["action_id"]
    assert change(client, case, case["newer"]).status_code == 200
    newer = row(case["table"], case["target"])
    assert client.get(f"/api/actions/{action_id}").json()["data"]["undo_available"] is False

    response = undo(client, action_id)
    assert response.status_code == 409
    data = response.json()["data"]
    assert data["verification"]["verdict"] == "conflict_detected"
    assert data["differences"][case["field"]] == {"expected": case["new"], "current": case["newer"]}
    assert row(case["table"], case["target"]) == newer


def test_tampering_after_restore_is_never_verified(client, case):
    action_id = change(client, case, case["new"]).json()["data"]["action_id"]
    undo_id = undo(client, action_id).json()["data"]["undo_action_id"]

    with db.transaction() as conn:
        conn.execute(case["tamper_sql"], (case["other"],))
    result = verification.verify_undo(undo_id)
    assert result["verdict"] == "failed"
    assert {c["id"] for c in result["checks"] if not c["passed"]} == {"unrelated_rows_unchanged"}

    with db.transaction() as conn:
        conn.execute(f"UPDATE {case['table']} SET {case['field']} = ? WHERE account_id = ?",
                     (case["new"], case["target"]))
    result = verification.verify_undo(undo_id)
    assert result["verdict"] == "failed"
    assert "restored_value" in {c["id"] for c in result["checks"] if not c["passed"]}


def test_pre_commit_guard_failure_rolls_back(client, case, monkeypatch):
    def failed_guard(*args):
        return {"verdict": "failed", "stage": "pre_commit_guard", "scope": "forced", "checks": []}

    before = rows(case["table"])
    monkeypatch.setattr(verification, "guard_update", failed_guard)
    response = change(client, case, case["new"])
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "write_not_verified"
    assert rows(case["table"]) == before


# --- credit-limit specifics ------------------------------------------------------------------

CREDIT = CASES["credit_limit"]


@pytest.mark.parametrize("account_id, new_limit, status, rule_id", [
    ("ACC-103", 120000, 403, "protected_customer"),
    ("ACC-102", 20000, 409, "no_change"),
    ("ACC-104", 600000, 403, "limit_out_of_range"),
    ("ACC-104", 250000, 202, "approval_threshold"),
    ("ACC-105", 60000, 202, "large_increase"),
    ("ACC-104", 100000, 200, "default_allow"),
])
def test_credit_limit_policy_rules(client, account_id, new_limit, status, rule_id):
    before = row("credit_lines", account_id)
    response = change(client, CREDIT, new_limit, account_id=account_id)
    assert response.status_code == status, response.json()
    assert response.json()["data"]["rule_id"] == rule_id
    if status != 200:
        assert row("credit_lines", account_id) == before


def test_credit_limit_approval_then_undo(client):
    pending = change(client, CREDIT, 60000, account_id="ACC-105").json()["data"]["action_id"]
    assert row("credit_lines", "ACC-105")["credit_limit"] == 30000
    approved = client.post(f"/api/actions/{pending}/approve", headers=APPROVER)
    assert approved.status_code == 200, approved.json()
    assert approved.json()["data"]["status"] == "executed"
    assert row("credit_lines", "ACC-105")["credit_limit"] == 60000
    assert undo(client, pending).status_code == 200
    assert row("credit_lines", "ACC-105")["credit_limit"] == 30000


def test_missing_credit_line_is_rejected(client):
    response = change(client, CREDIT, 10000, account_id="ACC-107")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "credit_line_not_found"
    [action] = client.get("/api/actions").json()["data"]
    assert (action["status"], action["account_id"]) == ("rejected", None)


def test_negative_limit_is_malformed(client):
    before = rows("credit_lines")
    response = change(client, CREDIT, -5)
    assert response.status_code == 422
    assert rows("credit_lines") == before


def test_credit_tool_requires_operator(client):
    response = client.post(CREDIT["route"], headers=APPROVER,
                           json={"account_id": "ACC-102", "new_limit": 10000, "reason": "x"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "role_forbidden"


@pytest.mark.parametrize("content", [
    None,                                                   # file missing
    "credit_limit: [not, a, mapping",                       # invalid YAML
    "credit_limit:\n  min_limit: 0\n",                      # missing keys
    "credit_limit:\n  min_limit: 9\n  max_limit: 1\n  block_protected_customers: true\n"
    "  approval_above: 5\n  approval_increase_pct: 50\n",   # min > max
    "other: {}\n",                                          # no credit_limit section
])
def test_policy_file_problems_fail_closed(client, tmp_path, monkeypatch, content):
    path = tmp_path / "policies.yaml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("REWIND_POLICIES_PATH", str(path))
    before = rows("credit_lines")

    response = change(client, CREDIT, 10000)
    assert response.status_code == 403
    assert response.json()["data"]["rule_id"] == "policy_config_invalid"
    assert rows("credit_lines") == before


def test_policy_yaml_is_read_per_request(client, tmp_path, monkeypatch):
    path = tmp_path / "policies.yaml"
    path.write_text(
        "credit_limit:\n  min_limit: 0\n  max_limit: 15000\n  block_protected_customers: true\n"
        "  approval_above: 15000\n  approval_increase_pct: 50\n", encoding="utf-8")
    monkeypatch.setenv("REWIND_POLICIES_PATH", str(path))
    response = change(client, CREDIT, 18000)
    assert response.json()["data"]["rule_id"] == "limit_out_of_range"


def test_shipped_policy_file_is_valid():
    policy, error = __import__("policies").load_credit_limit_policy()
    assert error is None
    assert policy.max_limit >= policy.approval_above >= policy.min_limit


def test_customer_status_evidence_keys_unchanged(client):
    # The refactor must not rename customer-status evidence keys the frontend reads.
    action_id = change(client, CASES["customer_status"], "Archived").json()["data"]["action_id"]
    undo_row = [a for a in client.get("/api/actions").json()["data"] if a["tool_name"] == "undo"]
    assert undo_row == []
    undo(client, action_id)
    [undo_row] = [a for a in client.get("/api/actions").json()["data"] if a["tool_name"] == "undo"]
    assert set(undo_row["evidence"]) == {"other_customers", "pre_commit_guard"}
    assert json.dumps(undo_row["evidence"]["pre_commit_guard"]["checks"][1]["id"]) == '"status_written"'
