"""Deterministic checks that re-read SQLite. Never calls the LLM or trusts agent output.

Callers run these inside the write transaction, after the write and before
commit, so a concurrent edit cannot make a correct write look failed.
"""
from db import get_customer, other_customers


def _verify(db, account_id, expected_status, expected_version, others_before, status_check):
    current = get_customer(db, account_id)
    checks = {
        status_check: current is not None and current["status"] == expected_status,
        "version_incremented": (
            current is not None and current["version"] == expected_version
        ),
        "unrelated_records_unchanged": (
            other_customers(db, account_id) == others_before
        ),
        "database_integrity_ok": (
            db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        ),
    }
    return {
        "status": "verified" if all(checks.values()) else "failed",
        "checks": checks,
        "current_customer": current,
    }


def verify_update(db, before, new_status, others_before):
    return _verify(
        db, before["account_id"], new_status, before["version"] + 1,
        others_before, "requested_status_applied",
    )


def verify_restore(db, before, after, others_before):
    return _verify(
        db, after["account_id"], before["status"], after["version"] + 1,
        others_before, "original_status_restored",
    )
