"""Conflict-aware Undo for customer status changes. Not a universal rollback."""
from uuid import uuid4

import verification
from db import connect, get_customer, list_customers, transaction
from ledger import get_action, get_executed_undo, now, record_action, set_recovery_state
from schemas import envelope, error_body, error_obj

UNDOABLE_TOOL = "update_customer_status"
CONFLICT_REASON = "Customer changed since this action; Undo would overwrite a newer edit"


def undo_eligibility(action, current):
    """The single definition of when Undo may run. Returns (available, reason, code)."""
    if action["tool_name"] == "undo":
        return False, "This is an Undo record; undoing an Undo is not supported", "not_undoable"
    if action["tool_name"] != UNDOABLE_TOOL:
        return False, f"Only {UNDOABLE_TOOL} actions can be undone", "not_undoable"
    if action["status"] != "executed":
        return False, f"Action was {action['status']}; there is no change to undo", "not_undoable"
    if action["recovery_state"] == "recovered":
        return False, "Already undone", "already_recovered"
    if (current is None or current != action["after"]
            or current["version"] != action["after_version"]):
        return False, CONFLICT_REASON, "conflict"
    return True, None, None


def with_undo_fields(db, actions):
    """Add undo_available / undo_disabled_reason, evaluated against current customers."""
    customers = {c["account_id"]: c for c in list_customers(db)}
    for action in actions:
        available, reason, _ = undo_eligibility(action, customers.get(action["account_id"]))
        action["undo_available"] = available
        action["undo_disabled_reason"] = reason
    return actions


def _differences(expected, current):
    if current is None:
        return {"customer": {"expected": "present", "current": None}}
    return {
        field: {"expected": expected.get(field), "current": current.get(field)}
        for field in sorted(expected.keys() | current.keys())
        if expected.get(field) != current.get(field)
    }


def _recovered(action_id, undo_row, already_recovered):
    # Built from the stored undo row, so a repeat call returns the same result.
    data = {
        "action_id": action_id,
        "undo_action_id": undo_row["id"],
        "status": "recovered",
        "already_recovered": already_recovered,
        "before": undo_row["before"],
        "after": undo_row["after"],
        "verification": undo_row["verification"],
    }
    if (undo_row["verification"] or {}).get("verdict") == "verified":
        return 200, envelope(data)
    return 500, envelope(data, error_obj(
        "recovery_not_verified",
        "The restore was committed but the post-commit verifier did not verify it",
    ))


def undo(action_id, actor="api"):
    """Compare, restore and write the undo ledger row in one BEGIN IMMEDIATE
    transaction; then verify the committed result on a new connection.

    Returns (http_status, {data, error}). 200 only when the post-commit verdict
    is "verified"; a committed restore that fails verification returns 500.
    """
    with transaction() as db:
        action = get_action(db, action_id)
        if action is None:
            return 404, error_body("action_not_found", "Action not found", {"action_id": action_id})

        current = get_customer(db, action["account_id"]) if action["account_id"] else None
        _, reason, code = undo_eligibility(action, current)

        if code == "already_recovered":
            return _recovered(action_id, get_executed_undo(db, action_id), True)

        if code == "not_undoable":
            return 409, error_body(
                "not_undoable", reason,
                {"action_id": action_id, "status": action["status"], "tool": action["tool_name"]},
            )

        expected = action["after"]
        undo_id = str(uuid4())
        common = {
            "action_id": undo_id, "tool": "undo", "args": {"action_id": action_id},
            "actor": actor, "undo_of_action_id": action_id, "before": current,
            "risk": "low", "recovery_state": "unavailable", "verification": None,
        }

        # A newer edit must never be overwritten by Undo.
        if code == "conflict":
            outcome = "conflict"
            evidence = {
                "expected_after": expected,
                "expected_version": action["after_version"],
                "current_customer": current,
                "differences": _differences(expected, current),
            }
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
        return 409, envelope(
            {"action_id": action_id, "undo_action_id": undo_id,
             "status": "conflict_detected", "reason": reason, **evidence,
             "verification": result},
            error_obj("undo_conflict", reason, {"differences": evidence["differences"]}),
        )
    if outcome == "failed":
        return 500, envelope(
            {"action_id": action_id, "undo_action_id": undo_id, "status": "failed",
             "reason": reason, "current_customer": after, "pre_commit_guard": guard,
             "verification": result},
            error_obj("restore_failed", reason),
        )
    with connect() as db:
        undo_row = get_action(db, undo_id)
    return _recovered(action_id, undo_row, False)
