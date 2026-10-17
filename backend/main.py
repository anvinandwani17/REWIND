import sqlite3
from pathlib import Path

from fastapi import FastAPI

DB_PATH = Path(__file__).with_name("rewind.db")
app = FastAPI(title="REWIND API")


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


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


@app.get("/api/customers")
def list_customers():
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM customers ORDER BY account_id"
        ).fetchall()

    return [
        {
            **dict(row),
            "protected": bool(row["protected"]),
        }
        for row in rows
    ]