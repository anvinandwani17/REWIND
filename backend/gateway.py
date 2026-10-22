"""The ONLY entry point for supported mutations."""
import json
from uuid import uuid4

from pydantic import ValidationError

import demo
import policies
import verification
from db import get_customer, other_customers, transaction
from ledger import count_executed_in_run, record_action
from schemas import UpdateCustomerStatusArgs, error_body

SUPPORTED_TOOLS = {"update_customer_status": UpdateCustomerStatusArgs}


def execute(tool_name, arguments, run_id=None, actor="api"):
    """validate -> BEGIN IMMEDIATE -> before-state -> policy -> version check
    -> UPDATE version+1 -> verify after-state -> ledger row -> COMMIT.

    The policy runs inside the lock because it needs the before-state; checking
    it outside would let the customer change between check and write.
    Returns (http_status, body).
    """
    if tool_name not in SUPPORTED_TOOLS:
        return 400, error_body("unsupported_tool", f"Unsupported tool: {tool_name}")
    if run_id is not None and not isinstance(run_id, str):
        return 422, error_body("invalid_arguments", "run_id must be a string")
    try:
        args = SUPPORTED_TOOLS[tool_name].model_validate(arguments)
    except ValidationError as exc:
        # Nothing is written: malformed input cannot be trusted enough to log
        # against an account.
        return 422, error_body(
            "invalid_arguments", "Tool arguments failed validation",
            json.loads(exc.json(include_url=False)),
        )

    settings = policies.settings()
    action_id = str(uuid4())
    common = {
        "action_id": action_id, "run_id": run_id, "tool": tool_name,
        "args": args.model_dump(), "actor": actor,
        "scenario": "injected_fault" if settings["recent_activity_disabled"] else None,
    }

    with transaction() as db:
        before = get_customer(db, args.account_id)
        if before is None:
            message = "Customer not found"
            record_action(
                db, **common, before=None, after=None, decision="reject",
                rule_id="account_not_found", reason=message, risk="low",
                status="rejected", recovery_state="unavailable", verification=None,
            )
            return 404, error_body(
                "customer_not_found", message,
                {"account_id": args.account_id, "action_id": action_id},
            )

        decision = policies.evaluate(
            before, args.new_status,
            now=demo.clock(),
            run_executed_count=count_executed_in_run(db, run_id),
            settings=settings,
        )
        outcome = {
            "action_id": action_id,
            "decision": decision["decision"],
            "rule_id": decision["rule_id"],
            "reason": decision["reason"],
            "risk": decision["risk"],
            "scenario": common["scenario"],
            "before": before,
        }

        if decision["decision"] != "allow":
            pending = decision["decision"] == "require_approval"
            status = "pending_approval" if pending else "blocked"
            record_action(
                db, **common, before=before, after=before,
                decision=decision["decision"], rule_id=decision["rule_id"],
                reason=decision["reason"], risk=decision["risk"], status=status,
                recovery_state="unavailable", verification=None,
            )
            http_status = 202 if pending else (409 if decision["rule_id"] == "no_change" else 403)
            return http_status, {**outcome, "status": status, "after": before}

        if args.expected_version is not None and args.expected_version != before["version"]:
            # Nothing written: the caller acted on a stale read.
            return 409, error_body(
                "version_conflict", "Customer changed since it was read",
                {"expected_version": args.expected_version,
                 "current_version": before["version"]},
            )

        others_before = other_customers(db, args.account_id)
        db.execute("SAVEPOINT write")
        cursor = db.execute(
            """
            UPDATE customers
            SET status = ?, version = version + 1
            WHERE account_id = ? AND version = ?
            """,
            (args.new_status, args.account_id, before["version"]),
        )
        result = verification.verify_update(db, before, args.new_status, others_before)
        # Never "executed" unless exactly one row actually changed and verified.
        executed = cursor.rowcount == 1 and result["status"] == "verified"
        if not executed:
            db.execute("ROLLBACK TO write")
        db.execute("RELEASE write")

        after = get_customer(db, args.account_id)
        status, recovery_state = (
            ("executed", "available") if executed else ("failed", "unavailable")
        )
        reason = decision["reason"] if executed else "Write did not verify; rolled back"
        record_action(
            db, **common, before=before, after=after,
            decision=decision["decision"], rule_id=decision["rule_id"],
            reason=reason, risk=decision["risk"], status=status,
            recovery_state=recovery_state, verification=result,
        )

    return (200 if executed else 500), {
        **outcome,
        "status": status,
        "reason": reason,
        "recovery_state": recovery_state,
        "after": after,
        "verification": result,
    }
