import importlib

import pytest
from fastapi.testclient import TestClient

import db
import demo
import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    with TestClient(main.app) as c:
        yield c


def snapshot():
    with db.connect() as conn:
        return (
            [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY account_id")],
            [dict(r) for r in conn.execute("SELECT * FROM actions ORDER BY action_id")],
        )


def archive(client, account_id):
    return client.post(
        "/api/tools/update-customer-status",
        json={"account_id": account_id, "new_status": "Archived", "run_id": "run-1"},
    )


def customer(client, account_id):
    return next(c for c in client.get("/api/customers").json() if c["account_id"] == account_id)


def test_import_and_startup_never_wipe_data(client):
    assert archive(client, "ACC-102").status_code == 200
    importlib.reload(main)
    with TestClient(main.app) as fresh:
        assert customer(fresh, "ACC-102")["status"] == "Archived"
        assert len(fresh.get("/api/actions").json()) == 1


def test_archive_executes_and_is_logged(client):
    response = archive(client, "ACC-102")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "executed"
    assert body["before"]["status"] == "Active"
    assert body["after"]["status"] == "Archived"
    assert body["after"]["version"] == body["before"]["version"] + 1

    [action] = client.get("/api/actions").json()
    assert action["id"] == body["action_id"]
    assert action["run_id"] == "run-1"
    assert action["recovery_state"] == "available"


def test_protected_customer_is_blocked_and_logged(client):
    response = archive(client, "ACC-103")
    assert response.status_code == 403
    assert response.json()["status"] == "blocked"
    assert customer(client, "ACC-103")["status"] == "Active"
    assert customer(client, "ACC-103")["version"] == 1
    [action] = client.get("/api/actions").json()
    assert action["status"] == "blocked"
    assert action["recovery_state"] == "unavailable"


def test_noop_update_is_blocked(client):
    response = client.post(
        "/api/tools/update-customer-status",
        json={"account_id": "ACC-101", "new_status": "Active"},
    )
    assert response.status_code == 409
    assert response.json()["status"] == "blocked"
    assert customer(client, "ACC-101")["version"] == 1


def test_undo_restores_and_verifies(client):
    action_id = archive(client, "ACC-102").json()["action_id"]
    response = client.post(f"/api/actions/{action_id}/undo")
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "verified"
    assert all(body["checks"].values())
    assert customer(client, "ACC-102")["status"] == "Active"
    assert customer(client, "ACC-102")["version"] == 3

    again = client.post(f"/api/actions/{action_id}/undo")
    assert again.status_code == 409
    assert again.json()["status"] == "refused"


def test_undo_refuses_to_overwrite_newer_edit(client):
    action_id = archive(client, "ACC-102").json()["action_id"]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE customers SET name = 'Edited', version = version + 1"
            " WHERE account_id = 'ACC-102'"
        )

    response = client.post(f"/api/actions/{action_id}/undo")
    assert response.status_code == 409
    assert response.json()["status"] == "conflict_detected"
    current = customer(client, "ACC-102")
    assert (current["name"], current["status"], current["version"]) == ("Edited", "Archived", 3)


def test_errors_use_status_reason_shape(client):
    missing = archive(client, "ACC-999")
    assert missing.status_code == 404
    assert missing.json() == {"status": "error", "reason": "Customer not found"}

    undo = client.post("/api/actions/nope/undo")
    assert undo.status_code == 404
    assert undo.json() == {"status": "error", "reason": "Action not found"}


def test_update_is_verified_in_its_own_transaction(client):
    body = archive(client, "ACC-102").json()
    assert body["verification"]["status"] == "verified"
    assert body["verification"]["checks"] == {
        "requested_status_applied": True,
        "version_incremented": True,
        "unrelated_records_unchanged": True,
        "database_integrity_ok": True,
    }


def failed_check(*args):
    return {"status": "failed", "checks": {"forced": False}, "current_customer": None}


def test_failed_update_verification_rolls_back_write_but_logs(client, monkeypatch):
    import verification
    monkeypatch.setattr(verification, "verify_update", failed_check)

    response = archive(client, "ACC-102")
    assert response.status_code == 500
    assert response.json()["status"] == "failed"
    assert customer(client, "ACC-102")["status"] == "Active"
    assert customer(client, "ACC-102")["version"] == 1
    [action] = client.get("/api/actions").json()
    assert (action["status"], action["recovery_state"]) == ("failed", "unavailable")


def test_failed_restore_verification_rolls_back_restore(client, monkeypatch):
    import verification
    action_id = archive(client, "ACC-102").json()["action_id"]
    monkeypatch.setattr(verification, "verify_restore", failed_check)

    response = client.post(f"/api/actions/{action_id}/undo")
    assert response.status_code == 500
    assert response.json()["status"] == "failed"
    assert customer(client, "ACC-102")["status"] == "Archived"
    assert customer(client, "ACC-102")["version"] == 2
    [action] = client.get("/api/actions").json()
    assert action["recovery_state"] == "verification_failed"
    assert action["recovered_at"] is None


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
    customers = client.get("/api/customers").json()
    assert set(customers[0]) == {
        "account_id", "name", "status", "last_activity_at", "protected", "version",
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
    assert first.json()["summary"]["total"] == 8


def test_reset_refused_when_demo_mode_off(client, monkeypatch):
    archive(client, "ACC-102")
    before = snapshot()
    monkeypatch.delenv("REWIND_DEMO_MODE")

    response = client.post("/api/demo/reset")
    assert response.status_code == 403
    assert response.json()["status"] == "error"
    assert snapshot() == before
