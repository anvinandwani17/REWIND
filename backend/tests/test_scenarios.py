"""The scenario lab, the post-action eligibility check, snapshot integrity and run evidence."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import db
import main
import scenarios
import verification

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = {"X-Role": "operator", "X-Role-Key": "demo-operator-key"}
STATUS = "/api/tools/update-customer-status"
LISTED = ["clean_archive", "protected_block", "recent_activity_block", "stale_report_fault",
          "conflict_after_edit", "double_undo", "tampered_snapshot", "unrelated_row_tamper",
          "malformed_tool_call", "provider_failure", "second_customer"]
FAULTS = {"stale_report_fault", "tampered_snapshot", "unrelated_row_tamper", "provider_failure",
          "credit_unrelated_row_tamper", "credit_broken_policy"}


# --- the lab ------------------------------------------------------------------------------

def test_every_scenario_behaves_as_expected():
    results = scenarios.run_all()
    assert [r["name"] for r in results][:len(LISTED)] == LISTED
    assert {r["name"] for r in results if r["label"] == "injected_fault"} == FAULTS
    assert [(r["name"], r["mismatches"]) for r in results if not r["ok"]] == []
    for r in results:
        if r["label"] == "injected_fault":
            assert r["description"].startswith("INJECTED FAULT")


def test_lab_reports_failures_instead_of_hiding_them(monkeypatch):
    # A verifier that passes every check must make the fault scenarios FAIL, not pass.
    monkeypatch.setattr(verification, "_all_passed", lambda checks: True)
    failed = {r["name"] for r in scenarios.run_all() if not r["ok"]}
    assert {"stale_report_fault", "tampered_snapshot", "unrelated_row_tamper",
            "credit_unrelated_row_tamper"} <= failed


def test_report_totals_and_save(tmp_path):
    results = scenarios.run_all("double_undo")
    report = scenarios.build_report(results, "double_undo")
    assert report["totals"] == {"total": 1, "passed": 1, "failed": 0, "injected_faults": 0}
    path = scenarios.save_report(report, tmp_path / "lab_report.json")
    assert json.loads(path.read_text(encoding="utf-8"))["scenarios"][0]["name"] == "double_undo"


def test_offline_demo_story_runs_in_replay_and_every_step_checks_out():
    lines = []
    assert scenarios.run_demo(out=lines.append) is True
    text = "\n".join(lines)
    assert "UNEXPECTED" not in text
    assert "mode=replay" in text and "scenario=injected_fault" in text and "mode=live" not in text
    for expected in ("flag=flagged_unsafe", "verdict=verified", "HTTP 409 conflict_detected",
                     "the newer edit is intact"):
        assert expected in text


def test_offline_demo_story_reports_a_broken_step(monkeypatch):
    # If REWIND stopped detecting the unsafe archive, the backup must say so, not print "OK".
    monkeypatch.setattr(verification, "post_action_check",
                        lambda action_id: {"verdict": "eligible", "checks": []})
    lines = []
    assert scenarios.run_demo(out=lines.append) is False
    assert any("REWIND detects the error" in line and "UNEXPECTED" in line for line in lines)


def test_cli_runs_as_a_module_from_the_repo_root(tmp_path):
    report_path = tmp_path / "lab_report.json"
    completed = subprocess.run(
        [sys.executable, "-m", "backend.scenarios", "second_customer"], cwd=ROOT,
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "REWIND_LAB_RESULTS_PATH": str(report_path)},
    )
    assert completed.returncode == 0, completed.stderr
    assert "second_customer" in completed.stdout and "PASS" in completed.stdout
    assert "1/1 scenarios behaved as expected" in completed.stdout
    assert json.loads(report_path.read_text(encoding="utf-8"))["totals"]["passed"] == 1


# --- API: lab run, evidence ----------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "scenarios.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    monkeypatch.setenv("REWIND_LAB_RESULTS_PATH", str(tmp_path / "lab_report.json"))
    for name in ("REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
                 "REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "REWIND_ROLE_KEYS", "REWIND_POLICIES_PATH"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(main.app, headers=OPERATOR) as c:
        yield c


def snapshot():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY account_id")]


def test_lab_route_runs_in_its_own_process_and_leaves_the_app_db_alone(client, tmp_path):
    before = snapshot()
    response = client.post("/api/lab/run", params={"filter": "clean_archive"})
    assert response.status_code == 200, response.json()
    report = response.json()["data"]
    assert report["totals"] == {"total": 1, "passed": 1, "failed": 0, "injected_faults": 0}
    assert snapshot() == before
    assert (tmp_path / "lab_report.json").exists()
    assert client.get("/api/metrics").json()["data"]["lab"]["total"] == 1


def test_lab_route_needs_operator_and_a_safe_filter(client):
    forbidden = client.post("/api/lab/run", headers={"X-Role": "auditor", "X-Role-Key": "demo-auditor-key"})
    assert forbidden.status_code == 403
    assert client.post("/api/lab/run", params={"filter": "../../etc"}).status_code == 422


def test_evidence_for_a_replay_run(client):
    run = client.post("/api/agent/runs", json={
        "instruction": "Archive customers inactive for over a year", "mode": "replay",
        "scenario": "archive_inactive"}).json()["data"]
    action_id = run["action_ids"][0]
    client.post(f"/api/actions/{action_id}/undo")

    response = client.get(f"/api/runs/{run['run_id']}/evidence")
    assert response.status_code == 200
    evidence = response.json()["data"]
    assert (evidence["run_id"], evidence["mode"], evidence["recorded_run"]) == (run["run_id"], "replay", True)
    assert evidence["instruction"] == "Archive customers inactive for over a year"
    assert [s["tool"] for s in evidence["steps"]] == [s["tool"] for s in run["steps"]]
    first = evidence["actions"][0]
    assert first["args"]["account_id"] == "ACC-102"
    assert first["policy"]["decision"] == "allow"
    assert (first["before"]["status"], first["after"]["status"]) == ("Active", "Archived")
    assert first["pre_commit_guard"]["verdict"] == "verified"
    assert first["post_action_check"]["verdict"] == "eligible"
    [attempt] = first["recovery"]["attempts"]
    assert attempt["verification"]["verdict"] == "verified"
    assert {c["id"] for c in attempt["verification"]["checks"]} >= {"restored_value", "snapshot_matches_stored_digest", "before_differs_from_after"}
    assert evidence["final_state"]["customers/ACC-102"]["status"] == "Active"


def test_evidence_for_ledger_rows_outside_an_agent_run(client):
    client.post(STATUS, json={"account_id": "ACC-102", "new_status": "Archived", "reason": "x",
                              "run_id": "manual-1"})
    evidence = client.get("/api/runs/manual-1/evidence").json()["data"]
    assert evidence["recorded_run"] is False and evidence["instruction"] is None
    assert len(evidence["actions"]) == 1


def test_evidence_for_unknown_run(client):
    response = client.get("/api/runs/nope/evidence")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "run_not_found"


# --- post-action eligibility check -----------------------------------------------------------------

def archive(client, account_id):
    return client.post(STATUS, json={"account_id": account_id, "new_status": "Archived", "reason": "x"})


def test_post_action_check_passes_an_eligible_archive(client):
    data = archive(client, "ACC-102").json()["data"]
    assert data["post_action_check"]["verdict"] == "eligible"
    assert data["post_action_check"]["stage"] == "post_action"
    action = client.get(f"/api/actions/{data['action_id']}").json()["data"]
    assert action["flag"] is None and action["post_action_check"] == data["post_action_check"]


def test_post_action_check_flags_an_archive_the_rule_should_have_blocked(client, monkeypatch):
    monkeypatch.setenv("REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "1")
    data = archive(client, "ACC-101").json()["data"]  # recently active
    assert data["status"] == "executed" and data["scenario"] == "injected_fault"
    assert data["post_action_check"]["verdict"] == "flagged_unsafe"
    failed = [c["id"] for c in data["post_action_check"]["checks"] if not c["passed"]]
    assert failed == ["inactive_beyond_threshold"]
    action = client.get(f"/api/actions/{data['action_id']}").json()["data"]
    assert action["flag"] == "flagged_unsafe"
    metrics = client.get("/api/metrics").json()["data"]
    assert metrics["actions"]["flagged"] == 1


def test_post_action_check_only_runs_for_archives(client):
    client.post(STATUS, json={"account_id": "ACC-107", "new_status": "Active", "reason": "x"})
    [action] = client.get("/api/actions").json()["data"]
    assert action["status"] == "executed" and action["post_action_check"] is None


# --- snapshot integrity -----------------------------------------------------------------------------

def test_tampered_before_state_blocks_the_restore(client):
    action_id = archive(client, "ACC-102").json()["data"]["action_id"]
    with db.transaction() as conn:
        before = json.loads(conn.execute("SELECT before_json FROM actions WHERE action_id = ?",
                                         (action_id,)).fetchone()[0])
        before["status"] = "Archived"
        conn.execute("UPDATE actions SET before_json = ? WHERE action_id = ?", (json.dumps(before), action_id))
    customer_before = snapshot()

    response = client.post(f"/api/actions/{action_id}/undo")
    assert response.status_code == 500
    data = response.json()["data"]
    assert data["verification"]["verdict"] == "failed"
    assert [c["id"] for c in data["pre_commit_guard"]["checks"] if not c["passed"]] == [
        "snapshot_matches_stored_digest", "before_differs_from_after"]
    assert snapshot() == customer_before


def test_every_ledger_row_carries_a_snapshot_digest(client):
    import ledger
    archive(client, "ACC-102")
    [action] = client.get("/api/actions").json()["data"]
    assert action["snapshot_sha256"] == ledger.snapshot_digest(action["before"], action["after"])
