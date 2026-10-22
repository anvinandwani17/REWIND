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
        "created_at": row["created_at"],
        "recovered_at": row["recovered_at"],
        "actor": row["actor"],
        "decision": row["decision"],
        "reason": row["reason"],
        "account_id": row["account_id"],
        "before_version": row["before_version"],
        "after_version": row["after_version"],
        "mode": row["mode"],
        "scenario": row["scenario"],
        "undo_of_action_id": row["undo_of_action_id"],
    }


def record_action(
    db, *, action_id, tool, args, before, after, risk, status,
    recovery_state, verification, reason=None, run_id=None,
    actor="api", mode=None, scenario=None, undo_of_action_id=None,
):
    """Insert a ledger row using the caller's transaction."""
    db.execute(
        """
        INSERT INTO actions (
            action_id, created_at, actor, tool, args_json, decision, reason,
            risk, status, account_id, before_json, after_json,
            before_version, after_version, run_id, mode, scenario,
            undo_of_action_id, recovery_state, verification_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id, now(), actor, tool, json.dumps(args),
            "block" if status == "blocked" else "allow", reason,
            risk, status, args.get("account_id"),
            _dumps(before), _dumps(after),
            before["version"] if before else None,
            after["version"] if after else None,
            run_id, mode, scenario, undo_of_action_id,
            recovery_state, _dumps(verification),
        ),
    )


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


def set_recovery_state(db, action_id, recovery_state, verification, recovered_at=None):
    db.execute(
        """
        UPDATE actions
        SET recovery_state = ?, verification_json = ?,
            recovered_at = COALESCE(?, recovered_at)
        WHERE action_id = ?
        """,
        (recovery_state, _dumps(verification), recovered_at, action_id),
    )
