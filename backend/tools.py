"""Tools the agent may call: bounded reads plus mutations that go through the gateway."""
import db
import gateway


def list_customers():
    with db.connect() as conn:
        return db.list_customers(conn)


def get_customer(account_id):
    with db.connect() as conn:
        return db.get_customer(conn, account_id)


def update_customer_status(account_id, new_status, run_id=None):
    return gateway.execute(
        "update_customer_status",
        {"account_id": account_id, "new_status": new_status},
        run_id,
    )
