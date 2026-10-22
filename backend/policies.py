"""Deterministic policy rules. evaluate() is pure: no I/O, no clock, no LLM."""
import os
from datetime import datetime

import demo
from schemas import CustomerStatus

ALLOWED_STATUSES = {status.value for status in CustomerStatus}


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
