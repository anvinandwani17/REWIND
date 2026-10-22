import lab
import verification


def test_every_lab_scenario_behaves_as_expected():
    results = lab.run_all()
    assert {r["adapter"] for r in results} == {"customer_status", "credit_limit"}
    assert [r["name"] for r in results if not r["ok"]] == []


def test_lab_reports_failures_instead_of_hiding_them(monkeypatch):
    # A verifier that passes everything must make the tamper scenarios FAIL.
    monkeypatch.setattr(verification, "_all_passed", lambda checks: True)
    failed = {r["name"] for r in lab.run_all("tamper") if not r["ok"]}
    assert failed == {
        "status: tamper with another customer after restore",
        "credit: tamper with another credit line after restore",
    }
