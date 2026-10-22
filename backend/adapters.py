"""Recovery adapters: one per supported, reversible kind of change.

REWIND does not offer universal rollback. An action can be applied through the
gateway and recovered only if an adapter here supports its tool. An adapter owns:

- snapshot / snapshot_unrelated: read the target row; fingerprint the rest of its table
- apply / restore: the guarded single-row writes (caller holds BEGIN IMMEDIATE)
- verify: check an Undo attempt against committed state (post-commit)
- classify_reversibility: whether Undo may run right now, and if not, why
- evaluate: the deterministic policy for a requested change
"""
import hashlib
import json
from abc import ABC, abstractmethod

import db
import demo
import policies
import verification
from ledger import count_executed_in_run
from schemas import UpdateCreditLimitArgs, UpdateCustomerStatusArgs


class Adapter(ABC):
    tool: str            # ledger tool name this adapter applies and undoes
    args_model: type     # pydantic model of the tool's arguments
    table: str
    key: str             # primary-key column; stored as the ledger's account_id
    field: str           # the one column this adapter changes
    entity: str          # snake_case name of one row, used in evidence keys
    noun: str            # human name of one row, used in messages
    update_label: str    # e.g. "status update"

    @property
    def title_noun(self):
        return self.noun[0].upper() + self.noun[1:]

    def target_id(self, args):
        return getattr(args, self.key)

    def scenario(self, settings):
        """Label for every action taken under an active injected fault, else None."""
        return None

    @abstractmethod
    def new_value(self, args): ...

    @abstractmethod
    def evaluate(self, db, before, new_value, *, run_id, settings):
        """Return {decision: allow|block|require_approval, rule_id, reason, risk}."""

    @abstractmethod
    def snapshot(self, db, target_id): ...

    @abstractmethod
    def snapshot_unrelated(self, db, target_id): ...

    @abstractmethod
    def apply(self, db, before, new_value):
        """Write new_value if the row is still at before's version. Returns rowcount."""

    @abstractmethod
    def restore(self, db, action, current):
        """Write the action's before-value if the row is still at current. Returns rowcount."""

    @abstractmethod
    def verify(self, db, attempt, original):
        """Post-commit verdict for one Undo attempt of `original`."""

    @abstractmethod
    def classify_reversibility(self, action, current):
        """Returns (available, reason, code); code in None|not_undoable|already_recovered|conflict."""


class VersionedFieldAdapter(Adapter):
    """One versioned row per target; the change is a single column. SQL identifiers
    come from class constants, never from input."""

    def to_dict(self, row):
        return dict(row)

    def snapshot(self, conn, target_id):
        row = conn.execute(
            f"SELECT * FROM {self.table} WHERE {self.key} = ?", (target_id,)
        ).fetchone()
        return self.to_dict(row) if row else None

    def snapshot_unrelated(self, conn, target_id):
        """SHA-256 over every other row of the table, all columns, in key order."""
        rows = [self.to_dict(row) for row in conn.execute(
            f"SELECT * FROM {self.table} WHERE {self.key} != ? ORDER BY {self.key}",
            (target_id,),
        )]
        canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
        return {
            "scope": f"all other rows in {self.table} table",
            f"excluded_{self.key}": target_id,
            "row_count": len(rows),
            "sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        }

    def apply(self, conn, before, new_value):
        return conn.execute(
            f"UPDATE {self.table} SET {self.field} = ?, version = version + 1"
            f" WHERE {self.key} = ? AND version = ?",
            (new_value, before[self.key], before["version"]),
        ).rowcount

    def restore(self, conn, action, current):
        return conn.execute(
            f"UPDATE {self.table} SET {self.field} = ?, version = version + 1"
            f" WHERE {self.key} = ? AND version = ? AND {self.field} = ?",
            (action["before"][self.field], current[self.key],
             current["version"], current[self.field]),
        ).rowcount

    def verify(self, conn, attempt, original):
        current = self.snapshot(conn, original["account_id"])
        return verification.verify_attempt(self, conn, attempt, original, current)

    def classify_reversibility(self, action, current):
        if action["status"] != "executed":
            return False, f"Action was {action['status']}; there is no change to undo", "not_undoable"
        if action["recovery_state"] == "recovered":
            return False, "Already undone", "already_recovered"
        if (current is None or current != action["after"]
                or current["version"] != action["after_version"]):
            return False, (f"{self.title_noun} changed since this action; "
                           "Undo would overwrite a newer edit"), "conflict"
        return True, None, None


class CustomerStatusAdapter(VersionedFieldAdapter):
    tool = "update_customer_status"
    args_model = UpdateCustomerStatusArgs
    table = "customers"
    key = "account_id"
    field = "status"
    entity = "customer"
    noun = "customer"
    update_label = "status update"

    def to_dict(self, row):
        return db.customer_dict(row)

    def new_value(self, args):
        return args.new_status

    def scenario(self, settings):
        return "injected_fault" if settings["recent_activity_disabled"] else None

    def evaluate(self, conn, before, new_value, *, run_id, settings):
        return policies.evaluate(
            before, new_value,
            now=demo.clock(),
            run_executed_count=count_executed_in_run(conn, run_id),
            settings=settings,
        )


class CreditLimitAdapter(VersionedFieldAdapter):
    tool = "update_credit_limit"
    args_model = UpdateCreditLimitArgs
    table = "credit_lines"
    key = "account_id"
    field = "credit_limit"
    entity = "credit_line"
    noun = "credit line"
    update_label = "credit limit update"

    def new_value(self, args):
        return args.new_limit

    def evaluate(self, conn, before, new_value, *, run_id, settings):
        policy, error = policies.load_credit_limit_policy()  # policies.yaml, read per request
        owner = db.get_customer(conn, before["account_id"])
        return policies.evaluate_credit_limit(
            before, new_value,
            protected=bool(owner and owner["protected"]),
            policy=policy, policy_error=error,
        )


ADAPTERS = {adapter.tool: adapter for adapter in (CustomerStatusAdapter(), CreditLimitAdapter())}


def for_tool(tool_name):
    return ADAPTERS.get(tool_name)
