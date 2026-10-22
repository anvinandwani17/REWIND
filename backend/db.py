import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from schemas import CustomerStatus

DEFAULT_DB_PATH = Path(__file__).parent / "data" / "rewind.db"

_STATUSES = ", ".join(f"'{status.value}'" for status in CustomerStatus)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS customers (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ({_STATUSES})),
    last_activity_at TEXT NOT NULL,              -- ISO-8601 UTC, e.g. 2026-10-15T12:00:00Z
    protected INTEGER NOT NULL DEFAULT 0 CHECK (protected IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor TEXT NOT NULL,                         -- api | agent | ops_user | system
    tool TEXT NOT NULL,                          -- update_customer_status | undo
    args_json TEXT NOT NULL,
    decision TEXT NOT NULL
        CHECK (decision IN ('allow', 'block', 'require_approval', 'reject')),
    rule_id TEXT,
    reason TEXT,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')),
    status TEXT NOT NULL CHECK (
        status IN ('executed', 'blocked', 'rejected', 'pending_approval', 'failed',
                   'conflict_detected')
    ),
    account_id TEXT REFERENCES customers(account_id),  -- NULL when the account does not exist
    before_json TEXT,
    after_json TEXT,
    before_version INTEGER,
    after_version INTEGER,
    run_id TEXT,
    mode TEXT CHECK (mode IN ('live', 'replay')),  -- NULL for direct API calls
    scenario TEXT,                               -- e.g. injected_fault
    undo_of_action_id TEXT REFERENCES actions(action_id),
    recovery_state TEXT NOT NULL,
    verification_json TEXT,                      -- undo rows: post-commit verifier result
    evidence_json TEXT,                          -- undo rows: pre-restore snapshot, guard, conflict evidence
    recovery_verification_json TEXT,             -- originals: latest undo attempt's verifier result
    recovered_at TEXT
);

-- At most one successful undo per action, enforced by SQLite itself.
CREATE UNIQUE INDEX IF NOT EXISTS one_executed_undo_per_action
    ON actions(undo_of_action_id) WHERE status = 'executed';
"""


def db_path():
    # Read on every call so tests can point at a temporary database.
    return Path(os.environ.get("REWIND_DB_PATH", DEFAULT_DB_PATH))


@contextmanager
def connect():
    """Autocommit connection that is always closed (Windows keeps files locked)."""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
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


def create_tables(db):
    # Statement by statement: executescript() would commit the caller's transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)


def init_schema():
    """Idempotent: creates missing tables, never drops or deletes anything."""
    with transaction() as db:
        create_tables(db)


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


def insert_customers(db, customers):
    db.executemany(
        """INSERT INTO customers
           (account_id, name, status, last_activity_at, protected, version)
           VALUES (:account_id, :name, :status, :last_activity_at, :protected, 1)""",
        customers,
    )
