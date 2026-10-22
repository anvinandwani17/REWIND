"""Deterministic policy rules. evaluate*() are pure: no I/O, no clock, no LLM."""
import os
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

import demo
from schemas import CustomerStatus

ALLOWED_STATUSES = {status.value for status in CustomerStatus}
DEFAULT_POLICIES_PATH = Path(__file__).with_name("policies.yaml")


def settings():
    """Read at call time so tests and demo runs can change them via env vars."""
    return {
        "recent_activity_days": int(os.environ.get("REWIND_RECENT_ACTIVITY_DAYS", "90")),
        "bulk_approval_threshold": int(os.environ.get("REWIND_BULK_APPROVAL_THRESHOLD", "3")),
        # Injected-fault switch: only honoured in demo mode, and every action
        # taken while it is on is labelled scenario="injected_fault".
        "recent_activity_disabled": (
            demo.demo_mode_enabled()
            and os.environ.get("REWIND_FAULT_DISABLE_RECENT_ACTIVITY") == "1"
        ),
    }


def days_since(timestamp, now):
    return (now - datetime.fromisoformat(timestamp)).days


def _result(decision, rule_id, reason, risk):
    return {"decision": decision, "rule_id": rule_id, "reason": reason, "risk": risk}


def evaluate(before, new_status, *, now, run_executed_count, settings):
    """Decide a status change for one customer.

    run_executed_count: changes this run has already executed (bulk rule).
    Returns {decision: allow|block|require_approval, rule_id, reason, risk}.
    """
    if new_status not in ALLOWED_STATUSES:
        return _result("block", "unknown_status",
                       f"Unknown status {new_status!r}; allowed: {sorted(ALLOWED_STATUSES)}", "high")
    if before["protected"]:
        return _result("block", "protected_customer", "Protected customer: update blocked", "high")
    if before["status"] == new_status:
        return _result("block", "no_change", "Customer already has the requested status", "low")

    risk = "medium" if new_status == CustomerStatus.ARCHIVED.value else "low"
    fault_note = None
    if new_status == CustomerStatus.ARCHIVED.value:
        days = days_since(before["last_activity_at"], now)
        limit = settings["recent_activity_days"]
        if days < limit:
            reason = f"Customer was active {days} days ago (rule requires {limit}+)"
            if not settings["recent_activity_disabled"]:
                return _result("block", "recent_activity", reason, "high")
            fault_note = f"INJECTED FAULT: recent_activity rule disabled. Would have blocked: {reason}"
            risk = "high"

    threshold = settings["bulk_approval_threshold"]
    if run_executed_count >= threshold:
        return _result("require_approval", "bulk_change",
                       f"Run already executed {run_executed_count} changes (threshold {threshold})", risk)

    if fault_note:
        return _result("allow", "recent_activity", fault_note, risk)
    return _result("allow", "default_allow", None, risk)


# --- credit limits (rules loaded from policies.yaml) -------------------------------

class CreditLimitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_limit: StrictInt = Field(ge=0)
    max_limit: StrictInt = Field(ge=0)
    block_protected_customers: StrictBool
    approval_above: StrictInt = Field(ge=0)
    approval_increase_pct: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _range(self):
        if self.min_limit > self.max_limit:
            raise ValueError("min_limit must not exceed max_limit")
        return self


def load_credit_limit_policy():
    """Returns (policy, None) or (None, error). Never raises: callers fail closed."""
    path = Path(os.environ.get("REWIND_POLICIES_PATH", DEFAULT_POLICIES_PATH))
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        return CreditLimitPolicy.model_validate(document["credit_limit"]), None
    except Exception as exc:  # missing file, bad YAML, wrong shape: all mean "no valid policy"
        # Type only: the message can contain server file paths, and this text is returned to
        # clients and stored in the ledger as the block reason.
        return None, type(exc).__name__


def evaluate_credit_limit(before, new_limit, *, protected, policy, policy_error):
    """Decide a credit-limit change for one credit line."""
    if policy is None:
        return _result("block", "policy_config_invalid",
                       f"Credit-limit policy could not be loaded ({policy_error}); "
                       "all credit-limit changes are blocked", "high")
    if protected and policy.block_protected_customers:
        return _result("block", "protected_customer", "Protected customer: update blocked", "high")
    current = before["credit_limit"]
    if new_limit == current:
        return _result("block", "no_change", "Credit line already has the requested limit", "low")
    if not policy.min_limit <= new_limit <= policy.max_limit:
        return _result("block", "limit_out_of_range",
                       f"New limit {new_limit} is outside {policy.min_limit}..{policy.max_limit}", "high")

    increase = new_limit > current
    risk = "medium" if increase else "low"
    if new_limit > policy.approval_above:
        return _result("require_approval", "approval_threshold",
                       f"New limit {new_limit} is above {policy.approval_above}", "high")
    if increase and (current == 0 or (new_limit - current) * 100 > current * policy.approval_increase_pct):
        return _result("require_approval", "large_increase",
                       f"Increase from {current} to {new_limit} exceeds "
                       f"{policy.approval_increase_pct}% of the current limit", risk)
    return _result("allow", "default_allow", None, risk)
