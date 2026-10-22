"""Conflict-aware Undo for customer status changes. Not a universal rollback."""
from uuid import uuid4

import verification
from db import connect, get_customer, transaction
from ledger import get_action, get_executed_undo, now, record_action, set_recovery_state
from schemas import error_body

UNDOABLE_TOOL = "update_customer_status"


def _differences(expected, current):
    if current is None:
        return {"customer": {"expected": "present", "current": None}}
    return {
        field: {"expected": expected.get(field), "current": current.get(field)}
        for field in sorted(expected.keys() | current.keys())
        if expected.get(field) != current.get(field)
    }


def _verified(undo_row):
    return (undo_row["verification"] or {}).get("verdict") == "verified"


def _recovered_body(action_id, undo_row, already_recovered):
    # Built from the stored undo row, so a repeat call returns the same result.
    return {
        "action_id": action_id,
        "undo_action_id": undo_row["id"],
        "status": "recovered",
        "already_recovered": already_recovered,
        "before": undo_row["before"],
        "after": undo_row["after"],
        "verification": undo_row["verification"],
    }


def undo(action_id, actor="api"):
    """Compare, restore and write the undo ledger row in one BEGIN IMMEDIATE
    transaction; then verify the committed result on a new connection.

    Returns (http_status, body). 200 only when the post-commit verdict is
    "verified"; a committed restore that fails verification returns 500.
    """
    with transaction() as db:
        action = get_action(db, action_id)
        if action is None:
            return 404, error_body("action_not_found", "Action not found", {"action_id": action_id})

        if action["recovery_state"] == "recovered":
            undo_row = get_executed_undo(db, action_id)
            return (200 if _verified(undo_row) else 500), _recovered_body(action_id, undo_row, True)

        if action["status"] != "executed" or action["tool_name"] != UNDOABLE_TOOL:
            return 409, error_body(
                "not_undoable",
                f"Only executed {UNDOABLE_TOOL} actions can be undone",
                {"action_id": action_id, "status": action["status"], "tool": action["tool_name"]},
            )

        expected = action["after"]
        current = get_customer(db, action["account_id"])
        undo_id = str(uuid4())
        common = {
            "action_id": undo_id, "tool": "undo", "args": {"action_id": action_id},
            "actor": actor, "undo_of_action_id": action_id, "before": current,
            "risk": "low", "recovery_state": "unavailable", "verification": None,
        }

        # A newer edit must never be overwritten by Undo.
        if (current is None or current != expected
                or current["version"] != action["after_version"]):
            outcome = "conflict"
            evidence = {
                "expected_after": expected,
                "expected_version": action["after_version"],
                "current_customer": current,
                "differences": _differences(expected, current),
            }
            reason = "Customer changed since the action; Undo would overwrite a newer edit"
            record_action(
                db, **common, after=current, decision="block", rule_id="undo_conflict",
                reason=reason, status="conflict_detected", evidence=evidence,
            )
            set_recovery_state(db, action_id, "conflict_detected")
        else:
            # Evidence for the post-commit unrelated-rows check, taken under the lock.
            others_before = verification.snapshot_other_customers(db, current["account_id"])
            db.execute("SAVEPOINT restore")
            cursor = db.execute(
                """
                UPDATE customers
                SET status = ?, version = version + 1
                WHERE account_id = ? AND version = ? AND status = ?
                """,
                (action["before"]["status"], current["account_id"],
                 current["version"], current["status"]),
            )
            guard = verification.guard_restore(db, action, others_before, cursor.rowcount)
            restored = guard["verdict"] == "verified"
            if not restored:
                db.execute("ROLLBACK TO restore")
            db.execute("RELEASE restore")

            outcome = "recovered" if restored else "failed"
            after = get_customer(db, current["account_id"])
            reason = None if restored else "Pre-commit guard failed; restore rolled back"
            evidence = {"other_customers": others_before, "pre_commit_guard": guard}
            record_action(
                db, **common, after=after, decision="allow", rule_id="undo",
                reason=reason, status="executed" if restored else "failed",
                evidence=evidence,
            )
            set_recovery_state(
                db, action_id,
                "recovered" if restored else "restore_failed",
                now() if restored else None,
            )

    # Committed. Independent check on a new connection; never trusts the above.
    result = verification.verify_undo(undo_id)

    if outcome == "conflict":
        return 409, {
            "action_id": action_id, "undo_action_id": undo_id,
            "status": "conflict_detected", "reason": reason, **evidence,
            "verification": result,
        }
    if outcome == "failed":
        return 500, {
            "action_id": action_id, "undo_action_id": undo_id, "status": "failed",
            "reason": reason, "current_customer": after, "pre_commit_guard": guard,
            "verification": result,
        }
    with connect() as db:
        undo_row = get_action(db, undo_id)
    return (200 if _verified(undo_row) else 500), _recovered_body(action_id, undo_row, False)
