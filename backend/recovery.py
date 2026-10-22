"""Conflict-aware Undo for customer status changes. Not a universal rollback."""
import verification
from db import get_customer, other_customers, transaction
from ledger import get_action, now, set_recovery_state
from schemas import error_body


def undo(action_id):
    """Conflict check, restore, verification and ledger update in one transaction.

    A failed verification rolls the restore back, so the customer is never
    left changed by an Undo that did not verify. Returns (http_status, body).
    """
    with transaction() as db:
        action = get_action(db, action_id)
        if action is None:
            return 404, error_body("action_not_found", "Action not found", {"action_id": action_id})

        if action["recovery_state"] != "available":
            return 409, {
                "action_id": action_id,
                "status": "refused",
                "reason": (
                    "Undo unavailable; current recovery state is "
                    + action["recovery_state"]
                ),
            }

        before, after = action["before"], action["after"]
        account_id = after["account_id"]
        current = get_customer(db, account_id)

        # A newer edit must never be overwritten by Undo.
        if current != after:
            result = {
                "status": "conflict_detected",
                "reason": "Customer no longer matches the saved after-state",
                "expected_after": after,
                "current_customer": current,
            }
            set_recovery_state(db, action_id, "conflict_detected", result)
            return 409, {"action_id": action_id, **result}

        others_before = other_customers(db, account_id)
        db.execute("SAVEPOINT restore")
        db.execute(
            """
            UPDATE customers
            SET status = ?, version = version + 1
            WHERE account_id = ? AND version = ? AND status = ?
            """,
            (before["status"], account_id, after["version"], after["status"]),
        )
        result = verification.verify_restore(db, before, after, others_before)
        verified = result["status"] == "verified"
        if not verified:
            db.execute("ROLLBACK TO restore")
            result["current_customer"] = get_customer(db, account_id)
        db.execute("RELEASE restore")

        set_recovery_state(
            db, action_id,
            "verified" if verified else "verification_failed",
            result,
            now() if verified else None,
        )

    return (200 if verified else 500), {"action_id": action_id, **result}
