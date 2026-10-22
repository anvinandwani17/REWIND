import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

DB_PATH = Path(__file__).with_name("rewind.db")
app = FastAPI(title="REWIND API")


class StatusUpdateRequest(BaseModel):
    account_id: str
    new_status: Literal["Active", "Archived"]
    run_id: str | None = None


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def customer_dict(row):
    return {**dict(row), "protected": bool(row["protected"])}


def action_dict(row):
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "tool_name": row["tool_name"],
        "arguments": json.loads(row["arguments_json"]),
        "before": json.loads(row["before_json"]) if row["before_json"] else None,
        "after": json.loads(row["after_json"]) if row["after_json"] else None,
        "risk": row["risk"],
        "status": row["status"],
        "recovery_state": row["recovery_state"],
        "verification": (
            json.loads(row["verification_json"])
            if row["verification_json"] else None
        ),
        "created_at": row["created_at"],
        "recovered_at": row["recovered_at"],
    }


def initialize_database():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                account_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                last_activity_at TEXT NOT NULL,
                protected INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                before_json TEXT,
                after_json TEXT,
                risk TEXT NOT NULL,
                status TEXT NOT NULL,
                recovery_state TEXT NOT NULL,
                verification_json TEXT,
                created_at TEXT NOT NULL,
                recovered_at TEXT
            )
        """)

        count = db.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
        if count == 0:
            db.executemany(
                """INSERT INTO customers
                   (account_id, name, status, last_activity_at, protected)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    ("ACC-101", "Aarav Sharma", "Active", "2026-10-15", 0),
                    ("ACC-102", "Meera Iyer", "Active", "2024-03-10", 0),
                    ("ACC-103", "Rohan Gupta", "Active", "2023-08-22", 1),
                ],
            )


initialize_database()


def save_action(
    db, action_id, request, before, after,
    risk, status, recovery_state, verification,
):
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
            action_id,
            request.run_id,
            "update_customer_status",
            json.dumps({
                "account_id": request.account_id,
                "new_status": request.new_status,
            }),
            json.dumps(before) if before else None,
            json.dumps(after) if after else None,
            risk,
            status,
            recovery_state,
            json.dumps(verification),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def execute_status_update(request: StatusUpdateRequest):
    action_id = str(uuid4())

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT * FROM customers WHERE account_id = ?",
            (request.account_id,),
        ).fetchone()

        if row is None:
            raise HTTPException(status_code=404, detail="Customer not found")

        before = customer_dict(row)

        if before["protected"]:
            reason = "Protected customer: update blocked"
            save_action(
                db, action_id, request, before, before,
                "high", "blocked", "unavailable",
                {"policy": "blocked", "reason": reason},
            )
            return 403, {
                "action_id": action_id,
                "status": "blocked",
                "reason": reason,
                "before": before,
                "after": before,
            }

        if before["status"] == request.new_status:
            reason = "Customer already has the requested status"
            save_action(
                db, action_id, request, before, before,
                "low", "blocked", "unavailable",
                {"policy": "blocked", "reason": reason},
            )
            return 409, {
                "action_id": action_id,
                "status": "blocked",
                "reason": reason,
                "before": before,
                "after": before,
            }

        risk = "medium" if request.new_status == "Archived" else "low"

        result = db.execute(
            """
            UPDATE customers
            SET status = ?, version = version + 1
            WHERE account_id = ? AND version = ?
            """,
            (request.new_status, request.account_id, before["version"]),
        )

        if result.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="Customer changed during the update",
            )

        updated_row = db.execute(
            "SELECT * FROM customers WHERE account_id = ?",
            (request.account_id,),
        ).fetchone()
        after = customer_dict(updated_row)

        save_action(
            db, action_id, request, before, after,
            risk, "executed", "available",
            {"post_action": "pending"},
        )

    return 200, {
        "action_id": action_id,
        "status": "executed",
        "risk": risk,
        "recovery_state": "available",
        "before": before,
        "after": after,
    }


def verify_recovery(before, after, other_customers_before):
    """Read the database again; do not rely on the agent's answer."""
    with connect() as db:
        current_row = db.execute(
            "SELECT * FROM customers WHERE account_id = ?",
            (before["account_id"],),
        ).fetchone()
        current = customer_dict(current_row) if current_row else None

        other_rows = db.execute(
            "SELECT * FROM customers WHERE account_id != ? ORDER BY account_id",
            (before["account_id"],),
        ).fetchall()
        other_customers_after = [
            customer_dict(row) for row in other_rows
        ]

        integrity_result = db.execute(
            "PRAGMA integrity_check"
        ).fetchone()[0]

    checks = {
        "original_status_restored": (
            current is not None and current["status"] == before["status"]
        ),
        "version_incremented": (
            current is not None
            and current["version"] == after["version"] + 1
        ),
        "unrelated_records_unchanged": (
            other_customers_after == other_customers_before
        ),
        "database_integrity_ok": integrity_result == "ok",
    }

    return {
        "status": "verified" if all(checks.values()) else "failed",
        "checks": checks,
        "current_customer": current,
    }


def execute_undo(action_id: str):
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        action_row = db.execute(
            "SELECT * FROM actions WHERE id = ?",
            (action_id,),
        ).fetchone()

        if action_row is None:
            raise HTTPException(status_code=404, detail="Action not found")

        action = action_dict(action_row)

        if action["recovery_state"] != "available":
            return 409, {
                "action_id": action_id,
                "status": "refused",
                "reason": (
                    "Undo unavailable; current recovery state is "
                    + action["recovery_state"]
                ),
            }

        before = action["before"]
        after = action["after"]
        account_id = after["account_id"]

        current_row = db.execute(
            "SELECT * FROM customers WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        current = customer_dict(current_row) if current_row else None

        # A newer edit must never be overwritten by Undo.
        if current != after:
            verification = {
                "status": "conflict_detected",
                "reason": "Customer no longer matches the saved after-state",
                "expected_after": after,
                "current_customer": current,
            }
            db.execute(
                """
                UPDATE actions
                SET recovery_state = ?, verification_json = ?
                WHERE id = ?
                """,
                ("conflict_detected", json.dumps(verification), action_id),
            )
            return 409, {
                "action_id": action_id,
                **verification,
            }

        other_rows = db.execute(
            "SELECT * FROM customers WHERE account_id != ? ORDER BY account_id",
            (account_id,),
        ).fetchall()
        other_customers_before = [
            customer_dict(row) for row in other_rows
        ]

        result = db.execute(
            """
            UPDATE customers
            SET status = ?, version = version + 1
            WHERE account_id = ? AND version = ? AND status = ?
            """,
            (
                before["status"],
                account_id,
                after["version"],
                after["status"],
            ),
        )

        if result.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="Customer changed during Undo",
            )

        db.execute(
            """
            UPDATE actions
            SET recovery_state = ?, recovered_at = ?
            WHERE id = ?
            """,
            (
                "verification_pending",
                datetime.now(timezone.utc).isoformat(),
                action_id,
            ),
        )

    # This runs after the restoration transaction commits.
    verification = verify_recovery(
        before, after, other_customers_before
    )

    with connect() as db:
        db.execute(
            """
            UPDATE actions
            SET recovery_state = ?, verification_json = ?
            WHERE id = ?
            """,
            (
                verification["status"],
                json.dumps(verification),
                action_id,
            ),
        )

    return (
        200 if verification["status"] == "verified" else 500,
        {"action_id": action_id, **verification},
    )


@app.get("/api/customers")
def list_customers():
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM customers ORDER BY account_id"
        ).fetchall()
    return [customer_dict(row) for row in rows]


@app.get("/api/actions")
def list_actions():
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM actions ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [action_dict(row) for row in rows]


@app.post("/api/tools/update-customer-status")
def update_customer_status(request: StatusUpdateRequest):
    status_code, result = execute_status_update(request)
    return JSONResponse(status_code=status_code, content=result)


@app.post("/api/actions/{action_id}/undo")
def undo_action(action_id: str):
    status_code, result = execute_undo(action_id)
    return JSONResponse(status_code=status_code, content=result)