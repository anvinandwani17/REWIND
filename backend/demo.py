"""Deterministic synthetic demo fixture. All names and accounts are fictional."""
import os
from datetime import datetime, timedelta, timezone

import db

# Fixed clock so "days since last activity" is reproducible on any machine/date.
DEMO_NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _days_ago(days):
    return (DEMO_NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# (account_id, name, status, days since last activity, protected, role)
_FIXTURE = [
    ("ACC-101", "Aarav Sharma", "Active", 7, 0, "recently active"),
    ("ACC-102", "Meera Iyer", "Active", 591, 0, "inactive, eligible to archive"),
    ("ACC-103", "Rohan Gupta", "Active", 792, 1, "inactive but protected"),
    ("ACC-104", "Priya Nair", "Active", 30, 0, "recently active"),
    ("ACC-105", "Kabir Mehta", "Active", 420, 0, "inactive, eligible to archive"),
    ("ACC-106", "Ananya Rao", "Active", 2, 1, "recently active, protected"),
    ("ACC-107", "Vikram Singh", "Archived", 1000, 0, "already archived"),
    ("ACC-108", "Sara Thomas", "Active", 150, 0, "moderately inactive"),
]

CUSTOMERS = [
    {
        "account_id": account_id,
        "name": name,
        # Obviously synthetic: +91 90000 00<account number>.
        "phone": f"+91 90000 00{account_id[-3:]}",
        "status": status,
        "last_activity_at": _days_ago(days),
        "protected": protected,
        "role": role,
    }
    for account_id, name, status, days, protected, role in _FIXTURE
]


# Credit lines (whole INR). ACC-107 and ACC-108 have none.
CREDIT_LINES = [
    {"account_id": account_id, "credit_limit": limit}
    for account_id, limit in [
        ("ACC-101", 50000), ("ACC-102", 20000), ("ACC-103", 100000),
        ("ACC-104", 75000), ("ACC-105", 30000), ("ACC-106", 150000),
    ]
]


def demo_mode_enabled():
    return os.environ.get("REWIND_DEMO_MODE") == "1"


def clock():
    """Fixed demo clock in demo mode so policy results are reproducible."""
    return DEMO_NOW if demo_mode_enabled() else datetime.now(timezone.utc)


def seed_if_empty():
    """First-run convenience: fill an empty database. Never touches existing rows."""
    with db.transaction() as conn:
        if conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
            db.insert_customers(conn, CUSTOMERS)
        if conn.execute("SELECT COUNT(*) FROM credit_lines").fetchone()[0] == 0:
            existing = {row[0] for row in conn.execute("SELECT account_id FROM customers")}
            db.insert_credit_lines(conn, [c for c in CREDIT_LINES if c["account_id"] in existing])


def reset():
    """Drop, recreate and reseed every table in one transaction.

    Recreating (not just deleting) also upgrades a database made with an
    older schema.
    """
    with db.transaction() as conn:
        conn.execute("DROP TABLE IF EXISTS actions")
        conn.execute("DROP TABLE IF EXISTS credit_lines")
        conn.execute("DROP TABLE IF EXISTS customers")
        db.create_tables(conn)
        db.insert_customers(conn, CUSTOMERS)
        db.insert_credit_lines(conn, CREDIT_LINES)
        customers = db.list_customers(conn)
        credit_lines = db.list_credit_lines(conn)

    return {
        "status": "reset",
        "demo_now": DEMO_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "customers": customers,
        "credit_lines": credit_lines,
        "summary": {
            "total": len(customers),
            "active": sum(c["status"] == "Active" for c in customers),
            "archived": sum(c["status"] == "Archived" for c in customers),
            "protected": sum(c["protected"] for c in customers),
            "roles": {c["account_id"]: c["role"] for c in CUSTOMERS},
        },
        "ledger_entries": 0,
    }
