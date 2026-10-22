import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def _dumps(value):
    return json.dumps(value) if value is not None else None


def _loads(value):
    return json.loads(value) if value else None


def action_dict(row):
    # Original keys (id, tool_name, arguments, ...) kept for the frontend.
    return {
        "id": row["action_id"],
        "run_id": row["run_id"],
        "tool_name": row["tool"],
        "arguments": json.loads(row["args_json"]),
        "before": _loads(row["before_json"]),
        "after": _loads(row["after_json"]),
        "risk": row["risk"],
        "status": row["status"],
        "recovery_state": row["recovery_state"],
        "verification": _loads(row["verification_json"]),
        "evidence": _loads(row["evidence_json"]),
        "recovery_verification": _loads(row["recovery_verification_json"]),
        "approval": _loads(row["approval_json"]),
        "created_at": row["created_at"],
        "recovered_at": row["recovered_at"],
        "actor": row["actor"],
        "decision": row["decision"],
        "rule_id": row["rule_id"],
        "reason": row["reason"],
        "account_id": row["account_id"],
        "before_version": row["before_version"],
        "after_version": row["after_version"],
        "mode": row["mode"],
        "scenario": row["scenario"],
        "undo_of_action_id": row["undo_of_action_id"],
    }


def record_action(
    db, *, action_id, tool, args, before, after, decision, rule_id, risk,
    status, recovery_state, verification, reason=None, run_id=None,
    actor="api", mode=None, scenario=None, undo_of_action_id=None, evidence=None,
):
    """Insert a ledger row using the caller's transaction."""
    db.execute(
        """
        INSERT INTO actions (
            action_id, created_at, actor, tool, args_json, decision, rule_id,
            reason, risk, status, account_id, before_json, after_json,
            before_version, after_version, run_id, mode, scenario,
            undo_of_action_id, recovery_state, verification_json, evidence_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id, now(), actor, tool, json.dumps(args), decision, rule_id,
            reason, risk, status,
            before["account_id"] if before else None,
            _dumps(before), _dumps(after),
            before["version"] if before else None,
            after["version"] if after else None,
            run_id, mode, scenario, undo_of_action_id,
            recovery_state, _dumps(verification), _dumps(evidence),
        ),
    )


def resolve_pending(db, action_id, *, status, after, recovery_state, verification, reason, approval):
    """Approve: the pending row itself becomes the executed (or failed) action."""
    cursor = db.execute(
        """
        UPDATE actions
        SET status = ?, after_json = ?, after_version = ?, recovery_state = ?,
            verification_json = ?, reason = ?, approval_json = ?
        WHERE action_id = ? AND status = 'pending_approval'
        """,
        (status, _dumps(after), after["version"] if after else None, recovery_state,
         _dumps(verification), reason, _dumps(approval), action_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"Action {action_id} was not pending approval")


def deny_pending(db, action_id, approval):
    cursor = db.execute(
        "UPDATE actions SET status = 'denied', approval_json = ?"
        " WHERE action_id = ? AND status = 'pending_approval'",
        (_dumps(approval), action_id),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(f"Action {action_id} was not pending approval")


def store_undo_verification(db, undo_action_id, original_action_id, result):
    db.execute(
        "UPDATE actions SET verification_json = ? WHERE action_id = ?",
        (_dumps(result), undo_action_id),
    )
    db.execute(
        "UPDATE actions SET recovery_verification_json = ? WHERE action_id = ?",
        (_dumps(result), original_action_id),
    )


def count_executed_in_run(db, run_id):
    if run_id is None:
        return 0
    return db.execute(
        "SELECT COUNT(*) FROM actions WHERE run_id = ? AND status = 'executed'",
        (run_id,),
    ).fetchone()[0]


def get_executed_undo(db, action_id):
    row = db.execute(
        "SELECT * FROM actions WHERE undo_of_action_id = ? AND status = 'executed'",
        (action_id,),
    ).fetchone()
    return action_dict(row) if row else None


def list_actions_for_run(db, run_id):
    rows = db.execute(
        "SELECT * FROM actions WHERE run_id = ? ORDER BY created_at, action_id", (run_id,)
    ).fetchall()
    return [action_dict(row) for row in rows]


def get_action(db, action_id):
    row = db.execute(
        "SELECT * FROM actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    return action_dict(row) if row else None


def list_actions(db):
    rows = db.execute(
        "SELECT * FROM actions ORDER BY created_at DESC, action_id DESC"
    ).fetchall()
    return [action_dict(row) for row in rows]


def set_recovery_state(db, action_id, recovery_state, recovered_at=None):
    # The original action's own verification is kept; undo evidence lives on
    # the undo ledger row.
    db.execute(
        """
        UPDATE actions
        SET recovery_state = ?, recovered_at = COALESCE(?, recovered_at)
        WHERE action_id = ?
        """,
        (recovery_state, recovered_at, action_id),
    )
