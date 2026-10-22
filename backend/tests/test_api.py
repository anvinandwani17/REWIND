import importlib
import json

import pytest
from fastapi.testclient import TestClient

import db
import demo
import gateway
import main
import tools

UPDATE = "/api/tools/update-customer-status"
OPERATOR = {"X-Role": "operator", "X-Role-Key": "demo-operator-key"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    for name in ("REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
                 "REWIND_FAULT_DISABLE_RECENT_ACTIVITY"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(main.app, headers=OPERATOR) as c:
        yield c


def snapshot():
    """Raw rows straight from SQLite, for byte-level before/after comparison."""
    with db.connect() as conn:
        return (
            [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY account_id")],
            [dict(r) for r in conn.execute("SELECT * FROM actions ORDER BY action_id")],
        )


def update(client, account_id, new_status="Archived", **extra):
    body = {"account_id": account_id, "new_status": new_status,
            "reason": "inactive for over a year", "run_id": "run-1", **extra}
    return client.post(UPDATE, json=body)


def archive(client, account_id, **extra):
    return update(client, account_id, "Archived", **extra)


def customer(client, account_id):
    return next(c for c in client.get("/api/customers").json()["data"] if c["account_id"] == account_id)


def actions(client):
    return client.get("/api/actions").json()["data"]


# --- startup / schema / reset -------------------------------------------------

def test_import_and_startup_never_wipe_data(client):
    assert archive(client, "ACC-102").status_code == 200
    importlib.reload(main)
    with TestClient(main.app) as fresh:
        assert customer(fresh, "ACC-102")["status"] == "Archived"
        assert len(fresh.get("/api/actions").json()["data"]) == 1


def test_schema_init_is_idempotent_and_preserves_rows(client):
    archive(client, "ACC-102")
    before = snapshot()
    db.init_schema()
    db.init_schema()
    demo.seed_if_empty()
    assert snapshot() == before
    assert len(before[0]) == len(demo.CUSTOMERS)
    assert len(before[1]) == 1

    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_customers_shape_and_fixture_roles(client):
    customers = client.get("/api/customers").json()["data"]
    assert set(customers[0]) == {
        "account_id", "name", "phone", "status", "last_activity_at", "protected", "version",
    }
    by_id = {c["account_id"]: c for c in customers}
    assert by_id["ACC-101"]["last_activity_at"] == "2026-10-15T12:00:00Z"
    assert by_id["ACC-103"]["protected"] is True
    assert by_id["ACC-107"]["status"] == "Archived"


def test_reset_reproduces_identical_state_twice(client):
    first = client.post("/api/demo/reset")
    assert first.status_code == 200
    state = snapshot()
    assert state[1] == []

    archive(client, "ACC-102")
    archive(client, "ACC-105")
    assert snapshot() != state

    second = client.post("/api/demo/reset")
    assert second.json() == first.json()
    assert snapshot() == state
    assert first.json()["data"]["summary"]["total"] == 8


def test_reset_refused_when_demo_mode_off(client, monkeypatch):
    archive(client, "ACC-102")
    before = snapshot()
    monkeypatch.delenv("REWIND_DEMO_MODE")

    response = client.post("/api/demo/reset")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "demo_mode_off"
    assert snapshot() == before


# --- read tools -----------------------------------------------------------------

def test_inactivity_report_explains_age(client):
    report = tools.get_inactivity_report(min_inactive_days=365)
    assert report["as_of"] == "2026-10-22T12:00:00+00:00"
    rows = {r["account_id"]: r for r in report["customers"]}
    assert (rows["ACC-102"]["inactive_days"], rows["ACC-102"]["meets_threshold"]) == (591, True)
    assert (rows["ACC-101"]["inactive_days"], rows["ACC-101"]["meets_threshold"]) == (7, False)
    assert rows["ACC-103"]["protected"] is True

    later = tools.get_inactivity_report(min_inactive_days=0, as_of="2026-10-25T12:00:00Z")
    assert {r["account_id"]: r for r in later["customers"]}["ACC-101"]["inactive_days"] == 10


# --- gateway: allowed path ---------------------------------------------------------

def test_authorized_update_changes_exactly_one_row(client):
    customers_before, _ = snapshot()
    response = archive(client, "ACC-102", expected_version=1)
    body = response.json()["data"]
    assert response.status_code == 200
    assert (body["status"], body["decision"], body["rule_id"]) == ("executed", "allow", "default_allow")

    customers_after, ledger_rows = snapshot()
    changed = [a for b, a in zip(customers_before, customers_after) if a != b]
    assert len(changed) == 1
    assert changed[0]["account_id"] == "ACC-102"
    assert changed[0]["status"] == "Archived"
    assert changed[0]["version"] == 2

    [row] = ledger_rows
    assert (row["status"], row["account_id"]) == ("executed", "ACC-102")
    assert (row["before_version"], row["after_version"]) == (1, 2)
    [action] = actions(client)
    before_api = {**customers_before[1], "protected": False}
    assert action["before"] == before_api == body["before"]
    assert action["after"] == {**changed[0], "protected": False} == body["after"]
    assert action["arguments"]["reason"] == "inactive for over a year"
    assert action["run_id"] == "run-1"
    assert action["recovery_state"] == "available"
    assert body["verification"]["verdict"] == "verified"
    assert body["verification"]["stage"] == "pre_commit_guard"
    assert all(c["passed"] for c in body["verification"]["checks"])


def test_get_action_by_id(client):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    response = client.get(f"/api/actions/{action_id}")
    assert response.status_code == 200
    assert response.json()["data"] == actions(client)[0]

    missing = client.get("/api/actions/nope")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "action_not_found"


# --- gateway: policy blocks ----------------------------------------------------------

def test_protected_customer_blocked_and_row_byte_identical(client):
    before = snapshot()[0]
    response = archive(client, "ACC-103")
    assert response.status_code == 403
    body = response.json()["data"]
    assert (body["status"], body["decision"], body["rule_id"]) == ("blocked", "block", "protected_customer")

    after, ledger_rows = snapshot()
    assert after == before
    [row] = ledger_rows
    assert (row["status"], row["decision"], row["rule_id"]) == ("blocked", "block", "protected_customer")
    assert row["before_json"] == row["after_json"]


def test_noop_update_is_blocked(client):
    response = update(client, "ACC-101", "Active")
    assert response.status_code == 409
    assert response.json()["data"]["rule_id"] == "no_change"
    assert customer(client, "ACC-101")["version"] == 1


def test_unknown_status_is_blocked_and_logged(client):
    before = snapshot()[0]
    response = update(client, "ACC-102", "Deleted")
    assert response.status_code == 403
    assert response.json()["data"]["rule_id"] == "unknown_status"
    assert snapshot()[0] == before
    assert actions(client)[0]["status"] == "blocked"


def test_recent_activity_blocks_archiving(client):
    before = snapshot()[0]
    response = archive(client, "ACC-101")  # active 7 days ago
    assert response.status_code == 403
    assert response.json()["data"]["rule_id"] == "recent_activity"
    assert "7 days" in response.json()["data"]["reason"]
    assert snapshot()[0] == before


def test_recent_activity_window_is_configurable(client, monkeypatch):
    monkeypatch.setenv("REWIND_RECENT_ACTIVITY_DAYS", "5")
    assert archive(client, "ACC-101").status_code == 200


def test_injected_fault_disables_only_recent_activity_and_is_labelled(client, monkeypatch):
    monkeypatch.setenv("REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "1")

    response = archive(client, "ACC-101")
    body = response.json()["data"]
    assert response.status_code == 200
    assert body["scenario"] == "injected_fault"
    assert body["reason"].startswith("INJECTED FAULT")
    assert actions(client)[0]["scenario"] == "injected_fault"

    # Other rules still apply.
    assert archive(client, "ACC-106").json()["data"]["rule_id"] == "protected_customer"


def test_injected_fault_flag_ignored_outside_demo_mode(client, monkeypatch):
    monkeypatch.setenv("REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "1")
    monkeypatch.delenv("REWIND_DEMO_MODE")
    monkeypatch.setenv("REWIND_RECENT_ACTIVITY_DAYS", "100000")  # real clock: block everyone

    response = archive(client, "ACC-101")
    assert response.status_code == 403
    assert response.json()["data"]["rule_id"] == "recent_activity"
    assert response.json()["data"]["scenario"] is None


def test_bulk_changes_above_threshold_require_approval(client, monkeypatch):
    monkeypatch.setenv("REWIND_BULK_APPROVAL_THRESHOLD", "1")
    assert archive(client, "ACC-102").status_code == 200

    before = snapshot()[0]
    response = archive(client, "ACC-105")
    assert response.status_code == 202
    body = response.json()["data"]
    assert (body["status"], body["decision"], body["rule_id"]) == (
        "pending_approval", "require_approval", "bulk_change")
    assert snapshot()[0] == before
    assert actions(client)[0]["status"] == "pending_approval"

    # A different run is counted separately.
    assert archive(client, "ACC-105", run_id="run-2").status_code == 200


# --- gateway: rejected / malformed / conflict / atomicity ------------------------------

def test_invalid_account_returns_404_error_and_never_executes(client):
    before = snapshot()[0]
    response = archive(client, "ACC-999")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "customer_not_found"
    assert error["details"]["account_id"] == "ACC-999"

    assert snapshot()[0] == before
    [action] = actions(client)
    assert (action["status"], action["decision"], action["account_id"]) == ("rejected", "reject", None)
    assert not [a for a in actions(client) if a["status"] == "executed"]


@pytest.mark.parametrize("body", [
    {"account_id": "ACC-102", "new_status": "Archived"},                      # no reason
    {"account_id": "ACC-102", "new_status": 5, "reason": "x"},                 # wrong type
    {"account_id": "ACC-102", "new_status": "Archived", "reason": ""},         # empty reason
    {"account_id": "ACC-102", "new_status": "Archived", "reason": "x", "sql": "DROP"},  # extra key
    {"account_id": "ACC-102", "new_status": "Archived", "reason": "x", "expected_version": "1"},
    {"account_id": "ACC-102", "new_status": "Archived", "reason": "x", "run_id": 7},
])
def test_malformed_args_return_422_and_write_nothing(client, body):
    before = snapshot()
    response = client.post(UPDATE, json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert snapshot() == before


def test_non_object_body_returns_422_error_object(client):
    before = snapshot()
    response = client.post(UPDATE, json=["not", "an", "object"])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert snapshot() == before


def test_stale_expected_version_conflicts_and_writes_nothing(client):
    before = snapshot()
    response = archive(client, "ACC-102", expected_version=5)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "version_conflict"
    assert error["details"] == {"expected_version": 5, "current_version": 1}
    assert snapshot() == before


def test_exception_mid_transaction_leaves_no_trace(client, monkeypatch):
    before = snapshot()

    def crash(*args, **kwargs):
        raise RuntimeError("simulated crash after UPDATE, before ledger insert")

    monkeypatch.setattr(gateway, "record_action", crash)
    with pytest.raises(RuntimeError):
        gateway.execute(
            "update_customer_status",
            {"account_id": "ACC-102", "new_status": "Archived", "reason": "x"},
        )
    assert snapshot() == before


def test_update_verification_failure_rolls_back_write_but_logs(client, monkeypatch):
    import verification
    monkeypatch.setattr(verification, "guard_update", failed_check)

    response = archive(client, "ACC-102")
    assert response.status_code == 500
    assert response.json()["data"]["status"] == "failed"
    assert customer(client, "ACC-102")["status"] == "Active"
    assert customer(client, "ACC-102")["version"] == 1
    [action] = actions(client)
    assert (action["status"], action["recovery_state"]) == ("failed", "unavailable")


# --- undo -------------------------------------------------------------------------------

def failed_check(*args):
    return {"verdict": "failed", "stage": "pre_commit_guard", "scope": "forced failure (test)", "checks": []}


def undo(client, action_id):
    return client.post(f"/api/actions/{action_id}/undo")


def undo_rows(client, action_id):
    return [a for a in actions(client) if a["undo_of_action_id"] == action_id]


def test_undo_restores_and_bumps_version(client):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    response = undo(client, action_id)
    body = response.json()["data"]
    assert response.status_code == 200
    assert (body["status"], body["already_recovered"]) == ("recovered", False)
    assert body["verification"]["verdict"] == "verified"
    assert body["verification"]["stage"] == "post_commit"
    assert {c["id"] for c in body["verification"]["checks"]} == {
        "restored_value", "version_transition", "ledger_consistency",
        "db_integrity", "unrelated_rows_unchanged",
    }
    assert all(c["passed"] for c in body["verification"]["checks"])
    assert (body["before"]["status"], body["before"]["version"]) == ("Archived", 2)
    assert (body["after"]["status"], body["after"]["version"]) == ("Active", 3)
    assert customer(client, "ACC-102")["status"] == "Active"
    assert customer(client, "ACC-102")["version"] == 3

    [row] = undo_rows(client, action_id)
    assert row["id"] == body["undo_action_id"]
    assert (row["tool_name"], row["status"], row["decision"]) == ("undo", "executed", "allow")
    assert (row["before_version"], row["after_version"]) == (2, 3)
    assert row["account_id"] == "ACC-102"

    original = client.get(f"/api/actions/{action_id}").json()["data"]
    assert original["recovery_state"] == "recovered"
    assert original["recovered_at"] is not None
    # The original's own forward verification is preserved.
    assert original["verification"]["stage"] == "pre_commit_guard"
    # The undo verdict is persisted and exposed on both rows.
    assert original["recovery_verification"] == body["verification"]
    assert client.get(f"/api/actions/{body['undo_action_id']}").json()["data"]["verification"] == body["verification"]


def test_undo_after_second_actor_edit_conflicts_and_keeps_newer_edit(client):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    status, ops = gateway.execute(
        "update_customer_status",
        {"account_id": "ACC-102", "new_status": "Active", "reason": "customer called back"},
        actor="ops_user",
    )
    assert (status, ops["data"]["status"]) == (200, "executed")
    newer = customer(client, "ACC-102")
    assert newer["version"] == 3

    response = undo(client, action_id)
    body = response.json()["data"]
    assert response.status_code == 409
    assert body["status"] == "conflict_detected"
    assert body["expected_version"] == 2
    assert body["current_customer"] == newer
    assert body["differences"] == {
        "status": {"expected": "Archived", "current": "Active"},
        "version": {"expected": 2, "current": 3},
    }

    assert customer(client, "ACC-102") == newer
    [attempt] = undo_rows(client, action_id)
    assert attempt["status"] == "conflict_detected"
    assert attempt["before"] == attempt["after"] == newer
    assert attempt["evidence"]["differences"] == body["differences"]
    assert body["verification"]["verdict"] == "conflict_detected"
    assert all(c["passed"] for c in body["verification"]["checks"])
    assert attempt["verification"] == body["verification"]
    ops_row = next(a for a in actions(client) if a["actor"] == "ops_user")
    assert ops_row["status"] == "executed"
    assert client.get(f"/api/actions/{action_id}").json()["data"]["recovery_state"] == "conflict_detected"


def test_undo_conflict_reports_changed_fields(client):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE customers SET name = 'Edited', version = version + 1"
            " WHERE account_id = 'ACC-102'"
        )

    response = undo(client, action_id)
    assert response.status_code == 409
    assert set(response.json()["data"]["differences"]) == {"name", "version"}
    current = customer(client, "ACC-102")
    assert (current["name"], current["status"], current["version"]) == ("Edited", "Archived", 3)


def test_double_undo_is_harmless(client):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    first = undo(client, action_id).json()["data"]
    state = snapshot()

    second = undo(client, action_id)
    assert second.status_code == 200
    assert second.json()["data"] == {**first, "already_recovered": True}
    assert snapshot() == state
    assert customer(client, "ACC-102")["version"] == 3
    assert len(undo_rows(client, action_id)) == 1


def test_undo_of_blocked_action_is_refused(client):
    action_id = archive(client, "ACC-103").json()["data"]["action_id"]  # protected -> blocked
    before = snapshot()
    response = undo(client, action_id)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "not_undoable"
    assert error["details"]["status"] == "blocked"
    assert snapshot() == before


def test_undo_of_an_undo_is_refused(client):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    undo_id = undo(client, action_id).json()["data"]["undo_action_id"]
    response = undo(client, undo_id)
    assert response.status_code == 409
    assert response.json()["error"]["details"]["tool"] == "undo"


def test_undo_works_for_two_customers_in_one_session(client):
    first = archive(client, "ACC-102").json()["data"]["action_id"]
    second = archive(client, "ACC-105").json()["data"]["action_id"]
    untouched = customer(client, "ACC-108")

    assert undo(client, second).status_code == 200
    assert undo(client, first).status_code == 200
    for account_id in ("ACC-102", "ACC-105"):
        assert (customer(client, account_id)["status"], customer(client, account_id)["version"]) == ("Active", 3)
    assert customer(client, "ACC-108") == untouched


def test_undo_unknown_action_returns_error_object(client):
    response = undo(client, "nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "action_not_found"


def test_failed_restore_verification_rolls_back_restore(client, monkeypatch):
    import verification
    real_guard_restore = verification.guard_restore
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    monkeypatch.setattr(verification, "guard_restore", failed_check)

    response = undo(client, action_id)
    assert response.status_code == 500
    assert response.json()["data"]["status"] == "failed"
    assert customer(client, "ACC-102")["status"] == "Archived"
    assert customer(client, "ACC-102")["version"] == 2
    original = client.get(f"/api/actions/{action_id}").json()["data"]
    assert original["recovery_state"] == "restore_failed"
    assert original["recovery_verification"]["verdict"] == "failed"
    assert original["recovered_at"] is None
    [attempt] = undo_rows(client, action_id)
    assert attempt["status"] == "failed"

    # A retry is allowed once verification passes again.
    monkeypatch.setattr(verification, "guard_restore", real_guard_restore)
    assert undo(client, action_id).status_code == 200


# --- post-commit verifier: negative tests (no false "verified") -----------------------

import verification as verifier


def recovered(client, account_id="ACC-102"):
    action_id = archive(client, account_id).json()["data"]["action_id"]
    body = undo(client, action_id).json()["data"]
    assert body["verification"]["verdict"] == "verified"
    return action_id, body["undo_action_id"]


def failed_ids(result):
    return {c["id"] for c in result["checks"] if not c["passed"]}


def test_tampering_with_another_customer_after_restore_fails(client):
    action_id, undo_id = recovered(client)
    with db.transaction() as conn:
        conn.execute("UPDATE customers SET name = 'Tampered' WHERE account_id = 'ACC-108'")

    result = verifier.verify_undo(undo_id)
    assert result["verdict"] == "failed"
    assert failed_ids(result) == {"unrelated_rows_unchanged"}
    # Persisted: the API now reports failed, and a repeat Undo is not a 200.
    assert client.get(f"/api/actions/{action_id}").json()["data"]["recovery_verification"]["verdict"] == "failed"
    assert undo(client, action_id).status_code == 500


def test_tampering_with_the_restored_row_fails(client):
    _, undo_id = recovered(client)
    with db.transaction() as conn:
        conn.execute("UPDATE customers SET status = 'Archived' WHERE account_id = 'ACC-102'")
    result = verifier.verify_undo(undo_id)
    assert result["verdict"] == "failed"
    assert failed_ids(result) == {"restored_value"}


def test_tampering_with_the_restored_version_fails(client):
    _, undo_id = recovered(client)
    with db.transaction() as conn:
        conn.execute("UPDATE customers SET version = version + 1 WHERE account_id = 'ACC-102'")
    result = verifier.verify_undo(undo_id)
    assert result["verdict"] == "failed"
    assert failed_ids(result) == {"version_transition"}


def set_evidence(undo_id, evidence_json):
    with db.transaction() as conn:
        conn.execute("UPDATE actions SET evidence_json = ? WHERE action_id = ?", (evidence_json, undo_id))


def test_corrupted_snapshot_hash_fails(client):
    _, undo_id = recovered(client)
    with db.connect() as conn:
        evidence = json.loads(conn.execute(
            "SELECT evidence_json FROM actions WHERE action_id = ?", (undo_id,)).fetchone()[0])
    evidence["other_customers"]["sha256"] = "0" * 64
    set_evidence(undo_id, json.dumps(evidence))

    result = verifier.verify_undo(undo_id)
    assert result["verdict"] == "failed"
    assert failed_ids(result) == {"unrelated_rows_unchanged"}


@pytest.mark.parametrize("evidence_json", [
    None,
    json.dumps({"other_customers": "garbage"}),
    json.dumps({"other_customers": {"sha256": "abc", "row_count": 7}}),
])
def test_missing_or_unreadable_snapshot_is_never_verified(client, evidence_json):
    _, undo_id = recovered(client)
    set_evidence(undo_id, evidence_json)

    result = verifier.verify_undo(undo_id)
    assert result["verdict"] == "manual_review_required"
    # The check is omitted rather than claimed, and the scope says so.
    assert "unrelated_rows_unchanged" not in {c["id"] for c in result["checks"]}
    assert "did NOT check unrelated rows" in result["scope"]


def test_tampering_between_commit_and_verifier_makes_undo_response_unverified(client, monkeypatch):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    real_verify_undo = verifier.verify_undo

    def tamper_then_verify(undo_id):
        with db.transaction() as conn:
            conn.execute("UPDATE customers SET name = 'Sneaky' WHERE account_id = 'ACC-105'")
        return real_verify_undo(undo_id)

    monkeypatch.setattr(verifier, "verify_undo", tamper_then_verify)
    response = undo(client, action_id)
    assert response.status_code == 500
    assert response.json()["data"]["status"] == "recovered"
    assert response.json()["data"]["verification"]["verdict"] == "failed"


def test_every_listed_check_compares_shown_evidence(client):
    _, undo_id = recovered(client)
    for check in verifier.verify_undo(undo_id)["checks"]:
        assert check["passed"] == (check["expected"] == check["actual"])


def test_backend_logic_has_no_hardcoded_account_ids():
    from pathlib import Path
    backend = Path(__file__).resolve().parents[1]
    offenders = [p.name for p in backend.glob("*.py")
                 if p.name != "demo.py" and "ACC-" in p.read_text(encoding="utf-8")]
    assert offenders == []
