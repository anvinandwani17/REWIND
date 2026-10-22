"""AI Operations Agent: live (LLM tool loop) and replay (recorded script) runs.

Both modes execute tool calls through the same path: validate the arguments
against schemas.py, run read tools from tools.py (PII-masked), and send the one
write tool, update_customer_status, through the gateway. The agent has no other
write path. The final summary is built from the ledger rows of this run, never
from what the model says it did.
"""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

import adapters
import db
import ledger
import privacy
import tools
from schemas import (
    GetCustomerArgs, InactivityReportArgs, ListCustomersArgs, UpdateCustomerStatusArgs,
    envelope, error_body, error_obj,
)

MAX_STEPS = 8
REPLAYS_DIR = Path(__file__).with_name("replays")
DEFAULT_REPLAY = "archive_inactive"
DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You are the FinCore operations agent. You change customer data only through the \
update_customer_status tool, and every change you request is checked, recorded and may be blocked \
by REWIND.

Rules:
1. Only archive customers that get_inactivity_report shows as inactive beyond the threshold you \
were asked to use (meets_threshold is true). Never archive anyone else.
2. Before every update_customer_status call, call get_customer for that account and use its \
current version as expected_version.
3. If REWIND blocks a change, or it needs approval, stop and report what happened. Do not retry \
and do not try another way around the block.
4. Give a short, specific reason with every update.
5. Customer names and phone numbers are masked; refer to customers by account_id.
When you are done, reply with a short summary and no tool call."""

AGENT_TOOLS = {
    "list_customers": (ListCustomersArgs, "List every customer (names and phones masked)."),
    "get_customer": (GetCustomerArgs, "Read one customer, including its current version."),
    "get_inactivity_report": (
        InactivityReportArgs,
        "Days since each customer's last activity, and whether it meets min_inactive_days.",
    ),
    "update_customer_status": (
        UpdateCustomerStatusArgs,
        "Request a status change (Active/Archived). Checked by REWIND policy; may be blocked "
        "or held for approval. Send expected_version from get_customer.",
    ),
}


def tool_specs():
    """Tool definitions for the model, with JSON schemas generated from schemas.py."""
    return [
        {"name": name, "description": description, "input_schema": model.model_json_schema()}
        for name, (model, description) in AGENT_TOOLS.items()
    ]


# --- providers --------------------------------------------------------------------

@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    text: str | None = None
    tool_calls: list = field(default_factory=list)
    raw: object = None  # provider-native assistant content, passed back unchanged


class ProviderError(Exception):
    """Provider failed, timed out or refused. The run stops as provider_failed."""


class ProviderNotConfigured(Exception):
    def __init__(self, message, missing):
        super().__init__(message)
        self.missing = missing


class Provider:
    """Swap point for the LLM. next_turn receives a provider-neutral transcript:
    {"role": "user", "content": str}
    {"role": "assistant", "turn": Turn}
    {"role": "tool", "results": [{"id", "content": str, "is_error": bool}]}
    """
    name = "provider"
    model = None
    # False for providers that play fixed turns instead of calling a model; their runs
    # are labelled mode "replay", never presented as a live model run.
    live = True

    def next_turn(self, system, transcript, tool_definitions):
        raise NotImplementedError


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model, api_key, timeout):
        # Optional dependency: only live runs with LLM_PROVIDER=anthropic need it.
        import anthropic

        self._sdk = anthropic
        self.model = model
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)

    def _messages(self, transcript):
        messages = []
        for entry in transcript:
            if entry["role"] == "user":
                messages.append({"role": "user", "content": entry["content"]})
            elif entry["role"] == "assistant":
                messages.append({"role": "assistant", "content": entry["turn"].raw})
            else:
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"],
                     "is_error": r["is_error"]}
                    for r in entry["results"]
                ]})
        return messages

    def next_turn(self, system, transcript, tool_definitions):
        sdk = self._sdk
        try:
            response = self.client.messages.create(
                model=self.model, max_tokens=16000, system=system,
                tools=tool_definitions, messages=self._messages(transcript),
            )
        except sdk.APITimeoutError as exc:
            raise ProviderError("timeout") from exc
        except sdk.APIStatusError as exc:
            raise ProviderError(f"HTTP {exc.status_code} from provider") from exc
        except sdk.APIConnectionError as exc:
            raise ProviderError("connection error") from exc
        if response.stop_reason in ("refusal", "max_tokens"):
            raise ProviderError(f"model stopped with stop_reason={response.stop_reason}")
        return Turn(
            text="\n".join(b.text for b in response.content if b.type == "text") or None,
            tool_calls=[ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"],
            raw=response.content,
        )


def provider_from_env():
    name = os.environ.get("LLM_PROVIDER", "")
    api_key = os.environ.get("LLM_API_KEY", "")
    missing = [var for var, value in (("LLM_PROVIDER", name), ("LLM_API_KEY", api_key)) if not value]
    if missing:
        raise ProviderNotConfigured("Live mode needs an LLM provider configured", missing)
    if name != "anthropic":
        raise ProviderNotConfigured(f"Unknown LLM_PROVIDER {name!r}; supported: anthropic", ["LLM_PROVIDER"])
    try:
        return AnthropicProvider(
            model=os.environ.get("LLM_MODEL") or DEFAULT_MODEL,
            api_key=api_key,
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
        )
    except ImportError as exc:
        raise ProviderNotConfigured("The anthropic package is not installed (pip install anthropic)",
                                    []) from exc


# --- tool execution (shared by live and replay) ------------------------------------------

def _summary(name, result):
    if name == "list_customers":
        return f"{len(result)} customers"
    if name == "get_customer":
        if result is None:
            return "not found"
        return (f"{result['account_id']}: status {result['status']}, version {result['version']}, "
                f"protected {result['protected']}")
    if name == "get_inactivity_report":
        hits = [c["account_id"] for c in result["customers"] if c["meets_threshold"]]
        return (f"{len(hits)} of {len(result['customers'])} customers inactive for "
                f"{result['min_inactive_days']}+ days: {', '.join(hits) or 'none'}")
    raise ValueError(name)


def _mutation_summary(status, body):
    data, error = body["data"], body["error"]
    if status == 200:
        return (f"executed: {data['before']['account_id']} {data['before']['status']} -> "
                f"{data['after']['status']} (version {data['after']['version']})")
    if data and data.get("status") in ("blocked", "pending_approval"):
        return f"{data['status']} by {data['rule_id']}: {data['reason']}"
    return f"{error['code']}: {error['message']}"


def execute_tool(index, name, raw_args, run_id, mode):
    """Run one tool call. Returns (step, feedback_for_model, invalid, stop)."""
    step = {"index": index, "tool": name, "args": raw_args, "mode": mode,
            "outcome": None, "result_summary": None, "action_id": None, "http_status": None}
    if name not in AGENT_TOOLS:
        step.update(outcome="rejected", result_summary=f"unknown tool {name!r}")
        return step, f"Unknown tool {name!r}. Available: {', '.join(AGENT_TOOLS)}", True, False
    model = AGENT_TOOLS[name][0]
    try:
        if not isinstance(raw_args, dict):
            raise ValueError("arguments must be a JSON object")
        args = model.model_validate(raw_args)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        message = str(exc)
        step.update(outcome="rejected", result_summary=f"invalid arguments: {message}")
        return step, f"Invalid arguments for {name}: {message}", True, False

    if name == "update_customer_status":
        status, body = tools.update_customer_status(
            args.account_id, args.new_status, args.reason, args.expected_version,
            run_id=run_id, mode=mode,
        )
        data, error = body["data"], body["error"]
        step.update(
            http_status=status,
            outcome=(data or {}).get("status") or error["code"],
            action_id=(data or {}).get("action_id") or ((error or {}).get("details") or {}).get("action_id"),
            result_summary=_mutation_summary(status, body),
        )
        # Anything but an executed change stops the run: blocked, pending approval,
        # conflict, not found, failed. Enforced here, not left to the model.
        return step, json.dumps(body), False, status != 200

    if name == "list_customers":
        result = tools.list_customers()
    elif name == "get_customer":
        result = tools.get_customer(args.account_id)
    else:
        result = tools.get_inactivity_report(min_inactive_days=args.min_inactive_days, as_of=args.as_of)
    step.update(outcome="ok", result_summary=_summary(name, result))
    return step, json.dumps(result, default=str), False, False


# --- summary from the ledger -----------------------------------------------------------

STOP_REASONS = {
    "completed": "the run finished",
    "stopped_blocked": "REWIND did not execute a requested change, so the run stopped",
    "stopped_invalid_tool_call": "repeated invalid tool calls, so the run stopped",
    "step_limit_reached": f"the {MAX_STEPS}-step limit was reached",
    "provider_failed": "the LLM provider failed, so the run stopped",
}


def _final_summary(run_id, status):
    with db.connect() as conn:
        rows = ledger.list_actions_for_run(conn, run_id)
    lines = []
    for row in rows:
        account = row["account_id"] or row["arguments"].get("account_id")
        if row["status"] == "executed":
            lines.append(f"executed {account} {row['before']['status']} -> {row['after']['status']}")
        else:
            lines.append(f"{row['status']} {account} ({row['rule_id']})")
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    tally = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no actions"
    detail = "; ".join(lines)
    text = f"Run {status}: {STOP_REASONS[status]}. Ledger for {run_id}: {tally}."
    return (f"{text} {detail}." if detail else text), [row["id"] for row in rows]


def _finish(run, status, error=None):
    run["status"] = status
    run["final_summary"], run["action_ids"] = _final_summary(run["run_id"], status)
    # Stored for GET /api/runs/{run_id}/evidence, with customer names/phones masked.
    with db.connect() as conn:
        names = [c["name"] for c in db.list_customers(conn)]
    steps = [{**s, "args": privacy.mask_args(s["args"], names) if isinstance(s["args"], dict)
              else s["args"]} for s in run["steps"]]
    ledger.save_run(run, steps, privacy.mask_text(run["instruction"], names))
    if status == "provider_failed":
        return 502, envelope(run, error)
    return 200, envelope(run)


# --- live --------------------------------------------------------------------------------

def run_live(instruction, provider):
    run_id = f"run-{uuid4()}"
    mode = "live" if provider.live else "replay"
    run = {"run_id": run_id, "mode": mode, "status": "running", "instruction": instruction,
           "scenario": None, "provider": {"name": provider.name, "model": provider.model},
           "steps": [], "model_message": None, "final_summary": None, "action_ids": []}
    transcript = [{"role": "user", "content": instruction}]
    definitions = tool_specs()
    invalid_calls = 0

    while True:
        if len(run["steps"]) >= MAX_STEPS:
            return _finish(run, "step_limit_reached")
        try:
            turn = provider.next_turn(SYSTEM_PROMPT, transcript, definitions)
        except Exception as exc:  # any provider failure: stop; the ledger already holds what ran
            reason = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            return _finish(run, "provider_failed",
                           error_obj("provider_failed", "The LLM provider failed", {"reason": reason}))
        transcript.append({"role": "assistant", "turn": turn})
        if turn.text:
            run["model_message"] = turn.text  # shown, never used for final_summary
        if not turn.tool_calls:
            return _finish(run, "completed")

        results = []
        for call in turn.tool_calls:
            if len(run["steps"]) >= MAX_STEPS:
                return _finish(run, "step_limit_reached")
            step, feedback, invalid, stop = execute_tool(
                len(run["steps"]) + 1, call.name, call.input, run_id, mode)
            run["steps"].append(step)
            if stop:
                return _finish(run, "stopped_blocked")
            if invalid:
                invalid_calls += 1
                if invalid_calls > 1:  # the error was fed back once already
                    return _finish(run, "stopped_invalid_tool_call")
            results.append({"id": call.id, "content": feedback, "is_error": invalid})
        transcript.append({"role": "tool", "results": results})


# --- replay -------------------------------------------------------------------------------

class ReplayStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: StrictStr
    args: dict


class ReplayScript(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    version: StrictInt = Field(ge=1)
    source: StrictStr
    instruction: StrictStr
    steps: list[ReplayStep] = Field(min_length=1)


class ReplayError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def load_replay(name):
    path = REPLAYS_DIR / f"{name}.json"  # name is restricted to [a-z0-9_] by the request schema
    if not path.is_file():
        available = sorted(p.stem for p in REPLAYS_DIR.glob("*.json"))
        raise ReplayError(404, "replay_not_found", f"No replay script {name!r}; available: {available}")
    try:
        script = ReplayScript.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, ValidationError) as exc:
        raise ReplayError(500, "replay_invalid", f"Replay script {name!r} is invalid: {exc}") from exc
    if script.name != name:
        raise ReplayError(500, "replay_invalid", f"Replay script {name!r} declares name {script.name!r}")
    return script


def run_replay(instruction, scenario):
    """Plays a recorded script through the real gateway. Never a live model run."""
    script = load_replay(scenario)
    run_id = f"run-{uuid4()}"
    run = {"run_id": run_id, "mode": "replay", "status": "running", "instruction": instruction,
           "scenario": script.name, "script_version": script.version, "script_source": script.source,
           "recorded_instruction": script.instruction, "steps": [], "final_summary": None,
           "action_ids": []}
    for scripted in script.steps:
        if len(run["steps"]) >= MAX_STEPS:
            return _finish(run, "step_limit_reached")
        step, _, invalid, stop = execute_tool(
            len(run["steps"]) + 1, scripted.tool, scripted.args, run_id, "replay")
        run["steps"].append(step)
        if invalid:
            return _finish(run, "stopped_invalid_tool_call")
        if stop:
            return _finish(run, "stopped_blocked")
    return _finish(run, "completed")


# --- evidence for GET /api/runs/{run_id}/evidence -------------------------------------------

def run_evidence(run_id):
    """Everything recorded about one run, read back from SQLite. Returns (status, envelope)."""
    with db.connect() as conn:
        run = ledger.get_run(conn, run_id)
        actions = ledger.list_actions_for_run(conn, run_id)
        if run is None and not actions:
            return 404, error_body("run_not_found", "No run or ledger rows with this run_id",
                                   {"run_id": run_id})
        items, final_state = [], {}
        for action in actions:
            attempts = ledger.list_undo_attempts(conn, action["id"])
            items.append({
                "action_id": action["id"],
                "tool": action["tool_name"],
                "args": action["arguments"],
                "actor": action["actor"],
                "mode": action["mode"],
                "scenario": action["scenario"],
                "status": action["status"],
                "policy": {key: action[key] for key in ("decision", "rule_id", "reason", "risk")},
                "approval": action["approval"],
                "before": action["before"],
                "after": action["after"],
                "pre_commit_guard": action["verification"],
                "post_action_check": action["post_action_check"],
                "flag": action["flag"],
                "recovery": {
                    "state": action["recovery_state"],
                    "recovered_at": action["recovered_at"],
                    "attempts": [{
                        "undo_action_id": a["id"], "created_at": a["created_at"], "actor": a["actor"],
                        "status": a["status"], "reason": a["reason"], "verification": a["verification"],
                    } for a in attempts],
                },
            })
            adapter = adapters.for_tool(action["tool_name"])
            if adapter and action["account_id"]:
                final_state[f"{adapter.table}/{action['account_id']}"] = adapter.snapshot(
                    conn, action["account_id"])
    return 200, envelope({
        "run_id": run_id,
        "recorded_run": run is not None,  # False for ledger rows written outside an agent run
        "instruction": run["instruction"] if run else None,
        "mode": run["mode"] if run else (actions[0]["mode"] if actions else None),
        "status": run["status"] if run else None,
        "scenario": run["scenario"] if run else None,
        "provider": run["provider"] if run else None,
        "steps": run["steps"] if run else [],
        "final_summary": run["final_summary"] if run else None,
        "actions": items,
        "final_state": final_state,
    })


# --- entry point for POST /api/agent/runs --------------------------------------------------

def start_run(request):
    """Returns (http_status, {data, error})."""
    if request.mode == "replay":
        try:
            return run_replay(request.instruction, request.scenario or DEFAULT_REPLAY)
        except ReplayError as exc:
            return exc.status, error_body(exc.code, exc.message)
    try:
        provider = provider_from_env()
    except ProviderNotConfigured as exc:
        return 503, error_body("provider_not_configured", str(exc), {
            "missing_env": exc.missing,
            "required_env": ["LLM_PROVIDER=anthropic", "LLM_API_KEY"],
            "optional_env": [f"LLM_MODEL (default {DEFAULT_MODEL})", "LLM_TIMEOUT_SECONDS (default 60)"],
        })
    return run_live(request.instruction, provider)
