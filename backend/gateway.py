"""The ONLY entry point for supported mutations (including approved ones)."""
import json
from uuid import uuid4

from pydantic import ValidationError

import adapters
import policies
import privacy
import verification
from db import list_customers, transaction
from ledger import deny_pending, get_action, now, record_action, resolve_pending
from schemas import envelope, error_body, error_obj


def _not_pending(action_id, action):
    if action is None:
        return 404, error_body("action_not_found", "Action not found", {"action_id": action_id})
    return 409, error_body(
        "not_pending", "Only pending_approval actions can be approved or rejected",
        {"action_id": action_id, "status": action["status"]},
    )


def execute(tool_name, arguments, run_id=None, actor="api", approves=None, approval_note=None,
            mode=None):
    """validate -> BEGIN IMMEDIATE -> before-state -> policy -> version check
    -> UPDATE version+1 -> pre-commit guard on after-state -> ledger row -> COMMIT.

    The policy runs inside the lock because it needs the before-state; checking
    it outside would let the customer change between check and write.

    approves: id of a pending_approval action. Its recorded arguments go through
    every step again; the approval satisfies only a require_approval decision
    (never a block), the customer must be exactly as it was when approval was
    requested, and the pending row itself is resolved instead of adding a row.

    Returns (http_status, {data, error}). Non-2xx always carries an error;
    outcomes that were recorded (blocked, failed) also carry their data.
    """
    adapter = adapters.for_tool(tool_name)
    if adapter is None:
        return 400, error_body("unsupported_tool", f"Unsupported tool: {tool_name}")
    if run_id is not None and not isinstance(run_id, str):
        return 422, error_body("invalid_arguments", "run_id must be a string")
    try:
        args = adapter.args_model.model_validate(arguments)
    except ValidationError as exc:
        # Nothing is written: malformed input cannot be trusted enough to log
        # against an account.
        return 422, error_body(
            "invalid_arguments", "Tool arguments failed validation",
            json.loads(exc.json(include_url=False)),
        )

    settings = policies.settings()
    action_id = approves or str(uuid4())
    target_id, new_value = adapter.target_id(args), adapter.new_value(args)
    title = adapter.title_noun

    with transaction() as db:
        names = [c["name"] for c in list_customers(db)]
        common = {
            "action_id": action_id, "run_id": run_id, "tool": tool_name,
            # Free-text args (reason) are stored with customer names/phones masked.
            "args": privacy.mask_args(args.model_dump(), names), "actor": actor,
            "scenario": adapter.scenario(settings),
            "mode": mode,  # "live" | "replay" for agent runs, None for direct API calls
        }

        pending = None
        if approves is not None:
            pending = get_action(db, approves)
            if pending is None or pending["status"] != "pending_approval":
                return _not_pending(approves, pending)

        before = adapter.snapshot(db, target_id)
        if pending is not None and before != pending["before"]:
            # Nothing written; the request stays pending so an approver can reject it.
            return 409, error_body(
                "approval_stale",
                f"{title} changed since approval was requested; reject and re-request",
                {"requested_before": pending["before"], f"current_{adapter.entity}": before},
            )

        if before is None:
            message = f"{title} not found"
            record_action(
                db, **common, before=None, after=None, decision="reject",
                rule_id="account_not_found", reason=message, risk="low",
                status="rejected", recovery_state="unavailable", verification=None,
            )
            return 404, error_body(
                f"{adapter.entity}_not_found", message,
                {adapter.key: target_id, "action_id": action_id},
            )

        decision = adapter.evaluate(db, before, new_value, run_id=run_id, settings=settings)
        if pending is not None and decision["decision"] == "require_approval":
            decision = {**decision, "decision": "allow",
                        "reason": f"Approved by {actor}: {decision['reason']}"}
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
            if pending is not None:
                # An approval never overrides a block; the request stays pending.
                return 409, error_body("policy_blocked", decision["reason"],
                                       {"rule_id": decision["rule_id"]})
            awaiting = decision["decision"] == "require_approval"
            status = "pending_approval" if awaiting else "blocked"
            record_action(
                db, **common, before=before, after=before,
                decision=decision["decision"], rule_id=decision["rule_id"],
                reason=decision["reason"], risk=decision["risk"], status=status,
                recovery_state="unavailable", verification=None,
            )
            data = {**outcome, "status": status, "after": before}
            if awaiting:
                return 202, envelope(data)
            http_status = 409 if decision["rule_id"] == "no_change" else 403
            return http_status, envelope(data, error_obj(
                "policy_blocked", decision["reason"], {"rule_id": decision["rule_id"]},
            ))

        if args.expected_version is not None and args.expected_version != before["version"]:
            # Nothing written: the caller acted on a stale read.
            return 409, error_body(
                "version_conflict", f"{title} changed since it was read",
                {"expected_version": args.expected_version,
                 "current_version": before["version"]},
            )

        others_before = adapter.snapshot_unrelated(db, target_id)
        db.execute("SAVEPOINT write")
        rowcount = adapter.apply(db, before, new_value)
        result = verification.guard_update(adapter, db, before, new_value, others_before, rowcount)
        # Never "executed" unless exactly one row changed and every guard check passed.
        executed = result["verdict"] == "verified"
        if not executed:
            db.execute("ROLLBACK TO write")
        db.execute("RELEASE write")

        after = adapter.snapshot(db, target_id)
        status, recovery_state = (
            ("executed", "available") if executed else ("failed", "unavailable")
        )
        reason = decision["reason"] if executed else "Pre-commit guard failed; write rolled back"
        if pending is not None:
            resolve_pending(
                db, approves, status=status, after=after, recovery_state=recovery_state,
                verification=result, reason=reason,
                approval={"decision": "approved", "role": actor, "at": now(),
                          "note": privacy.mask_text(approval_note, names) if approval_note else None},
            )
        else:
            record_action(
                db, **common, before=before, after=after,
                decision=decision["decision"], rule_id=decision["rule_id"],
                reason=reason, risk=decision["risk"], status=status,
                recovery_state=recovery_state, verification=result,
            )

    data = {
        **outcome,
        "status": status,
        "reason": reason,
        "recovery_state": recovery_state,
        "after": after,
        "verification": result,
    }
    if executed:
        return 200, envelope(data)
    return 500, envelope(data, error_obj("write_not_verified", reason))


def reject(action_id, actor, note=None):
    """Resolve a pending_approval action as denied. No customer change."""
    with transaction() as db:
        pending = get_action(db, action_id)
        if pending is None or pending["status"] != "pending_approval":
            return _not_pending(action_id, pending)
        names = [c["name"] for c in list_customers(db)]
        deny_pending(db, action_id, {
            "decision": "rejected", "role": actor, "at": now(),
            "note": privacy.mask_text(note, names) if note else None,
        })
    return 200, envelope({"action_id": action_id, "status": "denied"})
