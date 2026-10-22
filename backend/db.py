import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).with_name("rewind.db")

# Synthetic demo data only.
SEED_CUSTOMERS = [
    ("ACC-101", "Aarav Sharma", "Active", "2026-10-15", 0),
    ("ACC-102", "Meera Iyer", "Active", "2024-03-10", 0),
    ("ACC-103", "Rohan Gupta", "Active", "2023-08-22", 1),
]


def db_path():
    # Read on every call so tests can point at a temporary database.
    return Path(os.environ.get("REWIND_DB_PATH", DEFAULT_DB_PATH))


@contextmanager
def connect():
    """Autocommit connection that is always closed (Windows keeps files locked)."""
    connection = sqlite3.connect(db_path(), timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction():
    """BEGIN IMMEDIATE: commits on normal exit, rolls back on any exception."""
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def customer_dict(row):
    return {**dict(row), "protected": bool(row["protected"])}


def get_customer(db, account_id):
    row = db.execute(
        "SELECT * FROM customers WHERE account_id = ?", (account_id,)
    ).fetchone()
    return customer_dict(row) if row else None


def list_customers(db):
    rows = db.execute("SELECT * FROM customers ORDER BY account_id").fetchall()
    return [customer_dict(row) for row in rows]


def other_customers(db, account_id):
    rows = db.execute(
        "SELECT * FROM customers WHERE account_id != ? ORDER BY account_id",
        (account_id,),
    ).fetchall()
    return [customer_dict(row) for row in rows]


def _seed(db):
    db.executemany(
        """INSERT INTO customers
           (account_id, name, status, last_activity_at, protected)
           VALUES (?, ?, ?, ?, ?)""",
        SEED_CUSTOMERS,
    )


def init_schema():
    """Create tables and seed only an empty database. Never wipes data."""
    with transaction() as db:
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
        if db.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
            _seed(db)


def reset_demo():
    """Wipe customers and the ledger, then reseed. Only POST /api/demo/reset calls this."""
    init_schema()
    with transaction() as db:
        db.execute("DELETE FROM actions")
        db.execute("DELETE FROM customers")
        _seed(db)
