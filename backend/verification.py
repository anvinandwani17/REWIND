"""Deterministic checks over SQLite. Never calls the LLM or trusts agent output.

Two stages, never confused with each other:
- guard_update / guard_restore run INSIDE the write transaction, before commit,
  and decide whether to roll back. They only prove what that transaction saw.
- verify_undo runs AFTER commit on a NEW connection, so it sees only committed
  data, and its result is persisted on the ledger.

A check is only listed when the code compared real evidence. A verdict is
"verified" only when every listed check passed and no required evidence was
missing.
"""
import hashlib
import json

from db import connect, get_customer, other_customers, transaction
from ledger import get_action, store_undo_verification

OTHER_ROWS_SCOPE = "all other rows in customers table"


def _check(check_id, description, expected, actual):
    # passed is always computed from the evidence shown, never asserted.
    return {
        "id": check_id,
        "description": description,
        "expected": expected,
        "actual": actual,
        "passed": expected == actual,
    }


def _result(verdict, stage, scope, checks):
    return {"verdict": verdict, "stage": stage, "scope": scope, "checks": checks}


def _all_passed(checks):
    return bool(checks) and all(check["passed"] for check in checks)


def snapshot_other_customers(db, account_id):
    """SHA-256 over every customers row except account_id, all columns, in key order."""
    rows = other_customers(db, account_id)
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return {
        "scope": OTHER_ROWS_SCOPE,
        "excluded_account_id": account_id,
        "row_count": len(rows),
        "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _fingerprint(snapshot):
    return {"row_count": snapshot["row_count"], "sha256": snapshot["sha256"]}


def _valid_snapshot(snapshot, account_id):
    return (
        isinstance(snapshot, dict)
        and isinstance(snapshot.get("sha256"), str)
        and len(snapshot["sha256"]) == 64
        and isinstance(snapshot.get("row_count"), int)
        and snapshot.get("excluded_account_id") == account_id
    )


# --- pre-commit guards ----------------------------------------------------------

def _guard(db, account_id, rowcount, expected_status, expected_version, others_before, what):
    current = get_customer(db, account_id)
    checks = [
        _check("one_row_updated", f"The {what} UPDATE changed exactly one customers row",
               1, rowcount),
        _check("status_written", "Target row's status, re-read inside the same transaction",
               expected_status, current["status"] if current else None),
        _check("version_incremented_by_one",
               "Target row's version, re-read inside the same transaction",
               expected_version, current["version"] if current else None),
        _check("other_customers_unchanged_in_transaction",
               f"Count and SHA-256 of {OTHER_ROWS_SCOPE}, before vs after the UPDATE, "
               "inside the same transaction",
               _fingerprint(others_before),
               _fingerprint(snapshot_other_customers(db, account_id))),
    ]
    scope = (
        f"Pre-commit guard for the {what} of customer {account_id}, inside the write "
        "transaction: UPDATE row count, the row's status and version as re-read in that "
        f"transaction, and a hash of {OTHER_ROWS_SCOPE}. Committed data is not re-read here."
    )
    return _result("verified" if _all_passed(checks) else "failed", "pre_commit_guard", scope, checks)


def guard_update(db, before, new_status, others_before, rowcount):
    return _guard(db, before["account_id"], rowcount, new_status, before["version"] + 1,
                  others_before, "status update")


def guard_restore(db, action, others_before, rowcount):
    return _guard(db, action["account_id"], rowcount, action["before"]["status"],
                  action["after_version"] + 1, others_before, "undo restore")


# --- post-commit verifier -----------------------------------------------------------

def _integrity(db):
    rows = [row[0] for row in db.execute("PRAGMA integrity_check")]
    return _check("db_integrity", "PRAGMA integrity_check on a new connection", "ok", "; ".join(rows))


def _ledger(attempt, original, expected_status, expected_original_state, extra=None):
    expected = {
        "tool_name": "undo",
        "status": expected_status,
        "undo_of_action_id": original["id"],
        "original_recovery_state": expected_original_state,
        **(extra or {}),
    }
    actual = {
        "tool_name": attempt["tool_name"],
        "status": attempt["status"],
        "undo_of_action_id": attempt["undo_of_action_id"],
        "original_recovery_state": original["recovery_state"],
        **{key: attempt[key] for key in (extra or {})},
    }
    return _check(
        "ledger_consistency",
        "Undo ledger row read back on a new connection (so it is committed), references "
        "the original action, and the original's recovery_state matches",
        expected, actual,
    )


def _verify_restore(db, attempt, original, current):
    account_id = original["account_id"]
    after_version = original["after_version"]
    checks = [
        _check("restored_value", "Customer status now equals the action's before-state status",
               original["before"]["status"], current["status"] if current else None),
        _check("version_transition", "Customer version now equals after_version + 1",
               after_version + 1, current["version"] if current else None),
        _ledger(attempt, original, "executed", "recovered",
                {"before_version": after_version, "after_version": after_version + 1}),
        _integrity(db),
    ]
    stored = (attempt["evidence"] or {}).get("other_customers")
    have_snapshot = _valid_snapshot(stored, account_id)
    if have_snapshot:
        checks.append(_check(
            "unrelated_rows_unchanged",
            f"Count and SHA-256 of {OTHER_ROWS_SCOPE} (excluding {account_id}): stored inside "
            "the recovery transaction just before the restore vs recomputed now",
            _fingerprint(stored), _fingerprint(snapshot_other_customers(db, account_id)),
        ))
        unrelated = f"and compared a hash of {OTHER_ROWS_SCOPE} with the one stored just before the restore"
    else:
        unrelated = ("but did NOT check unrelated rows: the pre-restore snapshot is missing "
                     "or unreadable")

    if not _all_passed(checks):
        verdict = "failed"
    elif not have_snapshot:
        verdict = "manual_review_required"
    else:
        verdict = "verified"
    scope = (
        f"After commit, on a new SQLite connection: checked customer {account_id}'s status and "
        f"version, the undo ledger row and the original action's recovery_state, and PRAGMA "
        f"integrity_check, {unrelated}."
    )
    return _result(verdict, "post_commit", scope, checks)


def _verify_conflict(db, attempt, original, current):
    checks = [
        _check("conflict_evidence",
               "Customer row still differs from the action's recorded after-state",
               {"matches_recorded_after_state": False},
               {"matches_recorded_after_state": current == original["after"]}),
        _check("customer_not_written",
               "Customer version unchanged since the conflict was detected (Undo wrote nothing)",
               attempt["before_version"], current["version"] if current else None),
        _ledger(attempt, original, "conflict_detected", "conflict_detected"),
        _integrity(db),
    ]
    scope = (
        f"After commit, on a new SQLite connection: confirmed customer "
        f"{original['account_id']} still differs from the recorded after-state and was not "
        "written by the Undo attempt, the attempt is in the ledger, and PRAGMA integrity_check."
    )
    verdict = "conflict_detected" if _all_passed(checks) else "manual_review_required"
    return _result(verdict, "post_commit", scope, checks)


def _verify_failed_restore(db, attempt, original, current):
    checks = [
        _check("customer_not_written",
               "Customer still equals the recorded after-state (restore was rolled back)",
               original["after"], current),
        _ledger(attempt, original, "failed", "restore_failed"),
        _integrity(db),
    ]
    scope = (
        f"After commit, on a new SQLite connection: confirmed the rolled-back restore left "
        f"customer {original['account_id']} at the recorded after-state, the failed attempt "
        "is in the ledger, and PRAGMA integrity_check."
    )
    # The restore did not happen, so this can never be "verified".
    verdict = "failed" if _all_passed(checks) else "manual_review_required"
    return _result(verdict, "post_commit", scope, checks)


def verify_undo(undo_action_id):
    """Re-read committed state for one Undo attempt, persist and return the result."""
    with connect() as db:  # new connection: sees committed data only
        attempt = get_action(db, undo_action_id)
        original_id = attempt["undo_of_action_id"] if attempt else None
        original = get_action(db, original_id) if original_id else None
        if attempt is None or original is None:
            return _result(
                "manual_review_required", "post_commit",
                f"Undo row {undo_action_id} or its original action could not be read; "
                "nothing was checked.", [],
            )
        current = get_customer(db, original["account_id"])
        branch = {
            "executed": _verify_restore,
            "conflict_detected": _verify_conflict,
            "failed": _verify_failed_restore,
        }.get(attempt["status"])
        if branch is None:
            result = _result("manual_review_required", "post_commit",
                             f"Unexpected undo row status {attempt['status']!r}; nothing was checked.", [])
        else:
            result = branch(db, attempt, original, current)

    with transaction() as db:
        store_undo_verification(db, undo_action_id, original_id, result)
    return result
