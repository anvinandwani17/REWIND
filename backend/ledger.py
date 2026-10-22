import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def _dumps(value):
    return json.dumps(value) if value is not None else None


def _loads(value):
    return json.loads(value) if value else None


def action_dict(row):
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "tool_name": row["tool_name"],
        "arguments": json.loads(row["arguments_json"]),
        "before": _loads(row["before_json"]),
        "after": _loads(row["after_json"]),
        "risk": row["risk"],
        "status": row["status"],
        "recovery_state": row["recovery_state"],
        "verification": _loads(row["verification_json"]),
        "created_at": row["created_at"],
        "recovered_at": row["recovered_at"],
    }


def record_action(
    db, action_id, run_id, tool_name, arguments, before, after,
    risk, status, recovery_state, verification,
):
    """Insert a ledger row using the caller's transaction."""
    db.execute(
        """
        INSERT INTO actions (
            id, run_id, tool_name, arguments_json, before_json,
            after_json, risk, status, recovery_state,
            verification_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id, run_id, tool_name, json.dumps(arguments),
            _dumps(before), _dumps(after), risk, status, recovery_state,
            _dumps(verification), now(),
        ),
    )


def get_action(db, action_id):
    row = db.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    return action_dict(row) if row else None


def list_actions(db):
    rows = db.execute(
        "SELECT * FROM actions ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [action_dict(row) for row in rows]


def set_recovery_state(db, action_id, recovery_state, verification, recovered_at=None):
    db.execute(
        """
        UPDATE actions
        SET recovery_state = ?, verification_json = ?,
            recovered_at = COALESCE(?, recovered_at)
        WHERE id = ?
        """,
        (recovery_state, _dumps(verification), recovered_at, action_id),
    )
