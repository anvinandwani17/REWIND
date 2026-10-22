"""GET /api/metrics: counts and timings measured from the ledger and the last lab run.

Read-only. Nothing here re-runs the verifier or estimates a value: anything that
cannot be measured is null, and every definition is listed in ASSUMPTIONS.
"""
import json
import os
import statistics
from datetime import datetime
from pathlib import Path

import ledger

DEFAULT_LAB_RESULTS_PATH = Path(__file__).parent / "data" / "lab_report.json"
REQUIRED_RESTORE_CHECKS = {
    "restored_value", "version_transition", "ledger_consistency", "db_integrity",
    "unrelated_rows_unchanged",
}

ASSUMPTIONS = [
    "actions.* counts forward actions only (every ledger row whose tool is not 'undo'); "
    "actions.by_status has the full breakdown.",
    "actions.executed = status 'executed' (includes approved-then-executed); "
    "actions.blocked = status 'blocked' (policy blocks).",
    "actions.flagged = forward actions the post-action eligibility check marked flagged_unsafe, "
    "or for which an Undo was requested (either counts as flagging the action as wrong). Flagged "
    "overlaps executed.",
    "recoveries.* counts every Undo attempt row by its stored post-commit verdict; 'unverified' "
    "means no verdict was stored.",
    "time_to_verified_recovery: from the moment an action was flagged - the earlier of its "
    "flagged_unsafe post_action_check.checked_at and its first Undo attempt's created_at - to "
    "verification.checked_at of the attempt whose stored verdict is 'verified'. Recoveries "
    "without a stored checked_at (older rows) are counted in excluded_no_timestamp, not estimated.",
    "false_verified.ledger = Undo rows whose stored verdict is 'verified' but whose own evidence "
    "does not support it (a required check missing, any check not passed, expected != actual, or "
    "stage not post_commit). It checks the stored evidence; it does not re-read current data.",
    "false_verified.lab = scenarios in the last saved lab report whose actual 'verdict' is "
    "'verified' when the expected 'verdict' is not. null when no lab report is saved.",
    "false_verified.count = ledger + lab (lab counted only when a lab run is saved). It must be 0.",
    "lab.* comes from the report the scenario lab writes (python -m backend.scenarios or "
    "POST /api/lab/run), not from this database.",
]


def lab_results_path():
    return Path(os.environ.get("REWIND_LAB_RESULTS_PATH", DEFAULT_LAB_RESULTS_PATH))


def _verdict(action):
    return (action["verification"] or {}).get("verdict")


def _supports_verified(verification):
    checks = verification.get("checks") or []
    return (
        verification.get("stage") == "post_commit"
        and REQUIRED_RESTORE_CHECKS <= {check.get("id") for check in checks}
        and all(check.get("passed") is True and check.get("expected") == check.get("actual")
                for check in checks)
    )


def _seconds(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _lab():
    path = lab_results_path()
    if not path.exists():
        return {"available": False, "file": path.name}, None
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
        scenarios = run["scenarios"]
        false_verified = sum(
            s["actual"].get("verdict") == "verified" and s["expected"].get("verdict") != "verified"
            for s in scenarios
        )
        summary = {
            "available": True,
            "file": path.name,
            "ran_at": run.get("ran_at"),
            "filter": run.get("filter"),
            "total": len(scenarios),
            "passed": sum(bool(s["ok"]) for s in scenarios),
            "failed": sum(not s["ok"] for s in scenarios),
        }
        return summary, false_verified
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"available": False, "file": path.name, "error": type(exc).__name__}, None


def compute(conn):
    actions = ledger.list_actions(conn)
    forward = [a for a in actions if a["tool_name"] != "undo"]
    attempts = [a for a in actions if a["tool_name"] == "undo"]

    by_status = {}
    for action in forward:
        by_status[action["status"]] = by_status.get(action["status"], 0) + 1
    attempts_by_action = {}
    for attempt in attempts:
        attempts_by_action.setdefault(attempt["undo_of_action_id"], []).append(attempt)

    verdicts = [_verdict(a) for a in attempts]
    recoveries = {
        "total": len(attempts),
        "verified": verdicts.count("verified"),
        "conflict_detected": verdicts.count("conflict_detected"),
        "failed": verdicts.count("failed"),
        "manual_review_required": verdicts.count("manual_review_required"),
        "unverified": verdicts.count(None),
    }

    flagged_at = {}
    for action in forward:
        moments = [a["created_at"] for a in attempts_by_action.get(action["id"], [])]
        if action["flag"] == "flagged_unsafe":
            moments.append(action["post_action_check"]["checked_at"])
        if moments:
            flagged_at[action["id"]] = min(moments, key=datetime.fromisoformat)

    samples, excluded = [], 0
    for tries in attempts_by_action.values():
        verified = [a for a in tries if _verdict(a) == "verified"]
        if not verified:
            continue
        checked_at = verified[0]["verification"].get("checked_at")
        if not checked_at:
            excluded += 1
            continue
        samples.append(_seconds(flagged_at[tries[0]["undo_of_action_id"]], checked_at))

    ledger_false_verified = sum(
        _verdict(a) == "verified" and not _supports_verified(a["verification"]) for a in attempts
    )
    lab, lab_false_verified = _lab()

    return {
        "measured_at": ledger.now(),
        "actions": {
            "total": len(forward),
            "executed": by_status.get("executed", 0),
            "blocked": by_status.get("blocked", 0),
            "flagged": len(flagged_at),
            "by_status": dict(sorted(by_status.items())),
        },
        "recoveries": recoveries,
        "time_to_verified_recovery_seconds": {
            "samples": len(samples),
            "median": statistics.median(samples) if samples else None,
            "max": max(samples) if samples else None,
            "excluded_no_timestamp": excluded,
        },
        "false_verified": {
            "count": ledger_false_verified + (lab_false_verified or 0),
            "ledger": ledger_false_verified,
            "lab": lab_false_verified,
        },
        "lab": lab,
        "assumptions": ASSUMPTIONS,
    }
