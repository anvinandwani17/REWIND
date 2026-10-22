import json

import pytest
from fastapi.testclient import TestClient

import db
import main
import scenarios

OPERATOR = {"X-Role": "operator", "X-Role-Key": "demo-operator-key"}
STATUS = "/api/tools/update-customer-status"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "metrics.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    monkeypatch.setenv("REWIND_LAB_RESULTS_PATH", str(tmp_path / "no-lab-run.json"))
    for name in ("REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
                 "REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "REWIND_ROLE_KEYS", "REWIND_POLICIES_PATH"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(main.app, headers=OPERATOR) as c:
        yield c


def metrics(client):
    response = client.get("/api/metrics", headers={})  # open read
    assert response.status_code == 200
    assert response.json()["error"] is None
    return response.json()["data"]


def archive(client, account_id, new_status="Archived"):
    return client.post(STATUS, json={"account_id": account_id, "new_status": new_status, "reason": "m"})


def undo(client, action_id):
    return client.post(f"/api/actions/{action_id}/undo")


def snapshot():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM actions ORDER BY action_id")]


def test_empty_ledger_reports_zeros_and_nulls_not_guesses(client):
    m = metrics(client)
    assert m["actions"] == {"total": 0, "executed": 0, "blocked": 0, "flagged": 0, "by_status": {}}
    assert m["recoveries"]["total"] == 0
    assert m["time_to_verified_recovery_seconds"] == {
        "samples": 0, "median": None, "max": None, "excluded_no_timestamp": 0}
    assert m["false_verified"] == {"count": 0, "ledger": 0, "lab": None}
    assert m["lab"]["available"] is False
    assert all(isinstance(line, str) for line in m["assumptions"]) and m["assumptions"]


def test_counts_follow_the_ledger(client):
    verified = archive(client, "ACC-102").json()["data"]["action_id"]
    conflicted = archive(client, "ACC-105").json()["data"]["action_id"]
    archive(client, "ACC-103")                      # blocked: protected
    archive(client, "ACC-105", "Active")            # newer edit -> conflict for `conflicted`
    assert undo(client, verified).status_code == 200
    assert undo(client, conflicted).status_code == 409

    m = metrics(client)
    assert m["actions"] == {"total": 4, "executed": 3, "blocked": 1, "flagged": 2,
                            "by_status": {"blocked": 1, "executed": 3}}
    assert m["recoveries"] == {"total": 2, "verified": 1, "conflict_detected": 1, "failed": 0,
                               "manual_review_required": 0, "unverified": 0}
    timing = m["time_to_verified_recovery_seconds"]
    assert timing["samples"] == 1
    assert timing["median"] == timing["max"] >= 0
    assert m["false_verified"]["count"] == 0


def set_times(action_id, flagged_at, checked_at):
    with db.transaction() as conn:
        row = conn.execute("SELECT action_id, verification_json FROM actions WHERE undo_of_action_id = ?",
                           (action_id,)).fetchone()
        verification = json.loads(row["verification_json"])
        verification["checked_at"] = checked_at
        conn.execute("UPDATE actions SET created_at = ?, verification_json = ? WHERE action_id = ?",
                     (flagged_at, json.dumps(verification), row["action_id"]))


def test_median_and_max_time_to_verified_recovery(client):
    first = archive(client, "ACC-102").json()["data"]["action_id"]
    second = archive(client, "ACC-105").json()["data"]["action_id"]
    third = archive(client, "ACC-108").json()["data"]["action_id"]
    for action_id in (first, second, third):
        assert undo(client, action_id).status_code == 200
    set_times(first, "2026-10-22T12:00:00+00:00", "2026-10-22T12:00:03+00:00")    # 3 s
    set_times(second, "2026-10-22T12:00:00+00:00", "2026-10-22T12:00:10+00:00")   # 10 s
    set_times(third, "2026-10-22T12:00:00+00:00", "2026-10-22T12:00:04.5+00:00")  # 4.5 s

    timing = metrics(client)["time_to_verified_recovery_seconds"]
    assert timing == {"samples": 3, "median": 4.5, "max": 10.0, "excluded_no_timestamp": 0}


def test_recoveries_without_a_verdict_time_are_excluded_not_estimated(client):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    undo(client, action_id)
    with db.transaction() as conn:
        row = conn.execute("SELECT action_id, verification_json FROM actions WHERE undo_of_action_id = ?",
                           (action_id,)).fetchone()
        verification = json.loads(row["verification_json"])
        del verification["checked_at"]
        conn.execute("UPDATE actions SET verification_json = ? WHERE action_id = ?",
                     (json.dumps(verification), row["action_id"]))
    timing = metrics(client)["time_to_verified_recovery_seconds"]
    assert timing == {"samples": 0, "median": None, "max": None, "excluded_no_timestamp": 1}


@pytest.mark.parametrize("tamper", [
    lambda v: v["checks"][0].update(passed=False),                          # a failed check
    lambda v: v.update(checks=[c for c in v["checks"] if c["id"] != "db_integrity"]),  # missing check
    lambda v: v["checks"][1].update(actual="something else"),               # expected != actual
    lambda v: v.update(stage="pre_commit_guard"),                           # wrong stage
])
def test_verified_verdict_without_supporting_evidence_counts_as_false_verified(client, tamper):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    undo(client, action_id)
    assert metrics(client)["false_verified"]["count"] == 0
    with db.transaction() as conn:
        row = conn.execute("SELECT action_id, verification_json FROM actions WHERE undo_of_action_id = ?",
                           (action_id,)).fetchone()
        verification = json.loads(row["verification_json"])
        tamper(verification)
        assert verification["verdict"] == "verified"
        conn.execute("UPDATE actions SET verification_json = ? WHERE action_id = ?",
                     (json.dumps(verification), row["action_id"]))
    assert metrics(client)["false_verified"] == {"count": 1, "ledger": 1, "lab": None}


def test_last_lab_run_is_reported(client, tmp_path, monkeypatch):
    path = tmp_path / "lab_report.json"
    monkeypatch.setenv("REWIND_LAB_RESULTS_PATH", str(path))
    results = scenarios.run_all()
    scenarios.save_report(scenarios.build_report(results), path)

    m = metrics(client)
    assert m["lab"]["available"] is True
    assert (m["lab"]["total"], m["lab"]["passed"], m["lab"]["failed"]) == (len(results), len(results), 0)
    assert m["false_verified"] == {"count": 0, "ledger": 0, "lab": 0}


def test_lab_false_verified_is_counted(client, tmp_path, monkeypatch):
    path = tmp_path / "lab_report.json"
    monkeypatch.setenv("REWIND_LAB_RESULTS_PATH", str(path))
    scenarios.save_report(scenarios.build_report([
        {"name": "tamper", "adapter": "customer_status", "label": "injected_fault",
         "expected": {"verdict": "failed"}, "actual": {"verdict": "verified"}, "ok": False},
        {"name": "undo", "adapter": "credit_limit", "label": "normal",
         "expected": {"verdict": "verified"}, "actual": {"verdict": "verified"}, "ok": True},
    ]), path)
    m = metrics(client)
    assert m["false_verified"] == {"count": 1, "ledger": 0, "lab": 1}
    assert (m["lab"]["passed"], m["lab"]["failed"]) == (1, 1)


def test_unreadable_lab_file_is_reported_not_guessed(client, tmp_path, monkeypatch):
    path = tmp_path / "lab_report.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("REWIND_LAB_RESULTS_PATH", str(path))
    m = metrics(client)
    assert m["lab"]["available"] is False
    assert "error" in m["lab"]
    assert m["false_verified"]["lab"] is None


def test_metrics_is_read_only(client):
    archive(client, "ACC-102")
    before = snapshot()
    metrics(client)
    metrics(client)
    assert snapshot() == before
