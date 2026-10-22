"""Tools the agent may call: bounded reads, plus one write that goes through the gateway.

Everything returned here is agent context, so customer names and phones are masked.
The FinCore UI reads unmasked customers from db directly (main.py), not from here.
"""
import adapters
import db
import demo
import gateway
import policies
import privacy
from schemas import GetCustomerArgs, InactivityReportArgs, ListCustomersArgs


def list_customers():
    ListCustomersArgs()
    with db.connect() as conn:
        return privacy.mask_pii(db.list_customers(conn))


def get_customer(account_id):
    args = GetCustomerArgs(account_id=account_id)
    with db.connect() as conn:
        return privacy.mask_pii(db.get_customer(conn, args.account_id))


def get_inactivity_report(*, min_inactive_days, as_of=None):
    """Per-customer inactivity age so any archive decision is explainable."""
    args = InactivityReportArgs(min_inactive_days=min_inactive_days, as_of=as_of)
    now = args.as_of or demo.clock()
    with db.connect() as conn:
        customers = db.list_customers(conn)
    return {
        "as_of": now.isoformat(),
        "min_inactive_days": args.min_inactive_days,
        "customers": [
            {
                "account_id": c["account_id"],
                "name": privacy.mask_name(c["name"]),
                "status": c["status"],
                "protected": c["protected"],
                "last_activity_at": c["last_activity_at"],
                "inactive_days": days,
                "meets_threshold": days >= args.min_inactive_days,
            }
            for c in customers
            for days in [policies.days_since(c["last_activity_at"], now)]
        ],
    }


def get_credit_line(account_id):
    args = GetCustomerArgs(account_id=account_id)
    with db.connect() as conn:
        return adapters.for_tool("update_credit_limit").snapshot(conn, args.account_id)


def update_credit_limit(account_id, new_limit, reason, expected_version=None, run_id=None):
    args = {"account_id": account_id, "new_limit": new_limit, "reason": reason}
    if expected_version is not None:
        args["expected_version"] = expected_version
    status, body = gateway.execute("update_credit_limit", args, run_id=run_id, actor="agent")
    return status, privacy.mask_pii(body)


def update_customer_status(account_id, new_status, reason, expected_version=None, run_id=None,
                           mode=None):
    args = {"account_id": account_id, "new_status": new_status, "reason": reason}
    if expected_version is not None:
        args["expected_version"] = expected_version
    status, body = gateway.execute("update_customer_status", args, run_id=run_id, actor="agent",
                                   mode=mode)
    return status, privacy.mask_pii(body)
