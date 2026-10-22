"""Conflict-aware Undo for the changes a recovery adapter supports. Not a universal rollback."""
from uuid import uuid4

import adapters
import gateway
import verification
from db import connect, transaction
from ledger import get_action, get_executed_undo, now, record_action, set_recovery_state
from schemas import envelope, error_body, error_obj


def _current(conn, action):
    adapter = adapters.for_tool(action["tool_name"])
    if adapter is None or not action["account_id"]:
        return None
    return adapter.snapshot(conn, action["account_id"])


def undo_eligibility(action, current):
    """The single definition of when Undo may run. Returns (available, reason, code)."""
    if action["tool_name"] == "undo":
        return False, "This is an Undo record; undoing an Undo is not supported", "not_undoable"
    adapter = adapters.for_tool(action["tool_name"])
    if adapter is None:
        supported = ", ".join(sorted(adapters.ADAPTERS))
        return False, f"Only {supported} actions can be undone", "not_undoable"
    return adapter.classify_reversibility(action, current)


def with_undo_fields(conn, actions):
    """Add undo_available / undo_disabled_reason, evaluated against current rows."""
    rows = {}
    for action in actions:
        cache_key = (action["tool_name"], action["account_id"])
        if cache_key not in rows:
            rows[cache_key] = _current(conn, action)
        available, reason, _ = undo_eligibility(action, rows[cache_key])
        action["undo_available"] = available
        action["undo_disabled_reason"] = reason
    return actions


def _differences(entity, expected, current):
    if current is None:
        return {entity: {"expected": "present", "current": None}}
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

        current = _current(db, action)
        _, reason, code = undo_eligibility(action, current)

        if code == "already_recovered":
            return _recovered(action_id, get_executed_undo(db, action_id), True)

        if code == "not_undoable":
            return 409, error_body(
                "not_undoable", reason,
                {"action_id": action_id, "status": action["status"], "tool": action["tool_name"]},
            )

        adapter = adapters.for_tool(action["tool_name"])
        current_key = f"current_{adapter.entity}"
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
                current_key: current,
                "differences": _differences(adapter.entity, expected, current),
            }
            record_action(
                db, **common, after=current, decision="block", rule_id="undo_conflict",
                reason=reason, status="conflict_detected", evidence=evidence,
            )
            set_recovery_state(db, action_id, "conflict_detected")
        else:
            restored, after, guard, others_before = gateway.apply_restore(db, adapter, action, current)
            outcome = "recovered" if restored else "failed"
            reason = None if restored else "Pre-commit guard failed; restore rolled back"
            evidence = {f"other_{adapter.table}": others_before, "pre_commit_guard": guard}
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
             "reason": reason, current_key: after, "pre_commit_guard": guard,
             "verification": result},
            error_obj("restore_failed", reason),
        )
    with connect() as db:
        undo_row = get_action(db, undo_id)
    return _recovered(action_id, undo_row, False)
