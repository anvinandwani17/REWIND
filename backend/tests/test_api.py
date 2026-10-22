import importlib

import pytest
from fastapi.testclient import TestClient

import db
import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "test.db"))
    with TestClient(main.app) as c:
        yield c


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


def test_reset_restores_seed_and_clears_ledger(client):
    archive(client, "ACC-102")
    response = client.post("/api/demo/reset")
    assert response.status_code == 200
    assert [c["status"] for c in response.json()["customers"]] == ["Active"] * 3
    assert client.get("/api/actions").json() == []
