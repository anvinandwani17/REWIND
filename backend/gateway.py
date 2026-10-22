"""The single entry point for every supported mutation."""
from uuid import uuid4

from pydantic import ValidationError

import policies
import verification
from db import get_customer, other_customers, transaction
from ledger import record_action
from schemas import UpdateCustomerStatusArgs

SUPPORTED_TOOLS = {"update_customer_status": UpdateCustomerStatusArgs}


def error(reason):
    return {"status": "error", "reason": reason}


def execute(tool_name, arguments, run_id=None):
    """Policy check, write, verification and ledger row in one transaction.

    Returns (http_status, body).
    """
    if tool_name not in SUPPORTED_TOOLS:
        return 400, error(f"Unsupported tool: {tool_name}")
    try:
        args = SUPPORTED_TOOLS[tool_name].model_validate(arguments)
    except ValidationError as exc:
        return 422, error(str(exc))

    action_id = str(uuid4())
    logged_args = args.model_dump()

    with transaction() as db:
        before = get_customer(db, args.account_id)
        if before is None:
            return 404, error("Customer not found")

        decision = policies.evaluate(before, args.new_status)
        if not decision["allowed"]:
            record_action(
                db, action_id, run_id, tool_name, logged_args, before, before,
                decision["risk"], "blocked", "unavailable",
                {"policy": "blocked", "reason": decision["reason"]},
            )
            return decision["http_status"], {
                "action_id": action_id,
                "status": "blocked",
                "reason": decision["reason"],
                "before": before,
                "after": before,
            }

        others_before = other_customers(db, args.account_id)
        db.execute("SAVEPOINT write")
        db.execute(
            """
            UPDATE customers
            SET status = ?, version = version + 1
            WHERE account_id = ? AND version = ?
            """,
            (args.new_status, args.account_id, before["version"]),
        )
        result = verification.verify_update(db, before, args.new_status, others_before)
        verified = result["status"] == "verified"
        if not verified:
            # Undo just the customer write; the ledger still records the attempt.
            db.execute("ROLLBACK TO write")
        db.execute("RELEASE write")

        after = get_customer(db, args.account_id)
        status, recovery_state = (
            ("executed", "available") if verified else ("failed", "unavailable")
        )
        record_action(
            db, action_id, run_id, tool_name, logged_args, before, after,
            decision["risk"], status, recovery_state, result,
        )

    return (200 if verified else 500), {
        "action_id": action_id,
        "status": status,
        "risk": decision["risk"],
        "recovery_state": recovery_state,
        "before": before,
        "after": after,
        "verification": result,
    }
