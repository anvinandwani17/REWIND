"""Agent runs without network: a scripted fake provider for live mode, and replay scripts."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent
import db
import main

OPERATOR = {"X-Role": "operator", "X-Role-Key": "demo-operator-key"}
RUNS = "/api/agent/runs"


class FakeProvider(agent.Provider):
    """Returns scripted turns; an Exception in the script is raised instead."""
    name, model = "fake", "fake-model"

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def next_turn(self, system, transcript, tool_definitions):
        self.calls.append({"system": system, "transcript": list(transcript), "tools": tool_definitions})
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def call(name, args, call_id="c1"):
    return agent.Turn(tool_calls=[agent.ToolCall(call_id, name, args)])


def done(text="done"):
    return agent.Turn(text=text)


UPDATE_102 = {"account_id": "ACC-102", "new_status": "Archived", "reason": "Inactive 591 days",
              "expected_version": 1}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("REWIND_DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.setenv("REWIND_DEMO_MODE", "1")
    for name in ("REWIND_RECENT_ACTIVITY_DAYS", "REWIND_BULK_APPROVAL_THRESHOLD",
                 "REWIND_FAULT_DISABLE_RECENT_ACTIVITY", "REWIND_ROLE_KEYS", "REWIND_POLICIES_PATH",
                 "LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(main.app, headers=OPERATOR) as c:
        yield c


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def install(turns):
        holder["provider"] = FakeProvider(turns)
        monkeypatch.setattr(agent, "provider_from_env", lambda: holder["provider"])
        return holder["provider"]
    return install


def live(client, instruction="Archive customers inactive for over a year"):
    return client.post(RUNS, json={"instruction": instruction, "mode": "live"})


def snapshot():
    with db.connect() as conn:
        return (
            [dict(r) for r in conn.execute("SELECT * FROM customers ORDER BY account_id")],
            [dict(r) for r in conn.execute("SELECT * FROM actions ORDER BY action_id")],
        )


def ledger_for(run_id):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT action_id, status, mode, actor FROM actions WHERE run_id = ? ORDER BY created_at",
            (run_id,))]


# --- tool surface ---------------------------------------------------------------------------

def test_only_read_tools_and_status_update_are_exposed_with_schemas_from_schemas_py():
    specs = {spec["name"]: spec for spec in agent.tool_specs()}
    assert set(specs) == {"list_customers", "get_customer", "get_inactivity_report", "update_customer_status"}
    for name, (model, _) in agent.AGENT_TOOLS.items():
        assert specs[name]["input_schema"] == model.model_json_schema()
    assert specs["update_customer_status"]["input_schema"]["additionalProperties"] is False


def test_system_prompt_states_the_three_rules():
    prompt = agent.SYSTEM_PROMPT
    assert "get_inactivity_report" in prompt and "meets_threshold" in prompt
    assert "Before every update_customer_status call, call get_customer" in prompt
    assert "If REWIND blocks a change" in prompt and "stop and report" in prompt


def test_agent_has_no_write_path_but_the_gateway():
    source = Path(agent.__file__).read_text(encoding="utf-8")
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "transaction(", "gateway.execute"):
        assert forbidden not in source  # mutations only via tools.update_customer_status -> gateway


# --- rejected tool calls ----------------------------------------------------------------------

def test_unknown_tool_is_rejected_and_fed_back_once(client, fake):
    provider = fake([call("drop_table", {"table": "customers"}), done("ok, stopping")])
    before = snapshot()
    response = live(client)
    assert response.status_code == 200
    run = response.json()["data"]
    assert run["status"] == "completed"
    assert run["steps"][0]["outcome"] == "rejected"
    assert "unknown tool" in run["steps"][0]["result_summary"]
    # The error went back to the model as an error tool result, exactly once.
    [feedback] = provider.calls[1]["transcript"][-1]["results"]
    assert feedback["is_error"] is True and "Unknown tool" in feedback["content"]
    assert snapshot() == before


def test_second_invalid_call_stops_the_run(client, fake):
    provider = fake([call("drop_table", {}), call("drop_table", {}), done()])
    run = live(client).json()["data"]
    assert run["status"] == "stopped_invalid_tool_call"
    assert len(run["steps"]) == 2
    assert len(provider.calls) == 2  # never asked the model again


@pytest.mark.parametrize("bad_args", [
    {"account_id": "ACC-102", "new_status": "Archived"},                     # no reason
    {"account_id": "ACC-102", "new_status": "Archived", "reason": 5},        # wrong type
    {"account_id": "ACC-102", "new_status": "Archived", "reason": "x", "sql": "DROP"},
    "not an object",
])
def test_malformed_args_are_rejected_and_nothing_is_written(client, fake, bad_args):
    fake([call("update_customer_status", bad_args), call("update_customer_status", bad_args)])
    before = snapshot()
    run = live(client).json()["data"]
    assert run["status"] == "stopped_invalid_tool_call"
    assert {s["outcome"] for s in run["steps"]} == {"rejected"}
    assert run["action_ids"] == []
    assert snapshot() == before


# --- gateway decides; summary comes from the ledger ----------------------------------------------

def test_executed_change_goes_through_the_gateway_and_is_labelled_live(client, fake):
    fake([call("get_customer", {"account_id": "ACC-102"}), call("update_customer_status", UPDATE_102),
          done("Archived 5 customers successfully!")])
    run = live(client).json()["data"]
    assert run["status"] == "completed"
    [row] = ledger_for(run["run_id"])
    assert row == {"action_id": run["action_ids"][0], "status": "executed", "mode": "live", "actor": "agent"}
    # The model's claim is kept visible but the summary is built from the ledger.
    assert run["model_message"] == "Archived 5 customers successfully!"
    assert "1 executed" in run["final_summary"]
    assert "executed ACC-102 Active -> Archived" in run["final_summary"]
    assert "5 customers" not in run["final_summary"]


def test_gateway_block_stops_the_run(client, fake):
    provider = fake([call("update_customer_status", {"account_id": "ACC-103", "new_status": "Archived",
                                                       "reason": "Inactive"}), done()])
    run = live(client).json()["data"]
    assert run["status"] == "stopped_blocked"
    assert run["steps"][0]["outcome"] == "blocked"
    assert "protected_customer" in run["steps"][0]["result_summary"]
    assert len(provider.calls) == 1
    assert "blocked ACC-103 (protected_customer)" in run["final_summary"]


def test_pending_approval_also_stops_the_run(client, fake, monkeypatch):
    monkeypatch.setenv("REWIND_BULK_APPROVAL_THRESHOLD", "0")
    fake([call("update_customer_status", UPDATE_102), done()])
    run = live(client).json()["data"]
    assert run["status"] == "stopped_blocked"
    assert run["steps"][0]["outcome"] == "pending_approval"


def test_step_limit_is_eight(client, fake):
    fake([call("list_customers", {}, call_id=f"c{i}") for i in range(20)])
    run = live(client).json()["data"]
    assert run["status"] == "step_limit_reached"
    assert len(run["steps"]) == agent.MAX_STEPS == 8


def test_agent_context_is_pii_masked(client, fake):
    provider = fake([call("list_customers", {}), call("get_customer", {"account_id": "ACC-102"}, "c2"),
                     call("get_inactivity_report", {"min_inactive_days": 365}, "c3"), done()])
    live(client)
    seen = json.dumps([c["transcript"] for c in provider.calls], default=str)
    for customer in client.get("/api/customers").json()["data"]:
        assert customer["name"] not in seen and customer["phone"] not in seen


# --- provider failure ----------------------------------------------------------------------------

@pytest.mark.parametrize("failure", [agent.ProviderError("timeout"), RuntimeError("socket closed")])
def test_provider_failure_mid_run_reports_only_what_executed(client, fake, failure):
    fake([call("update_customer_status", UPDATE_102), failure])
    response = live(client)
    assert response.status_code == 502
    body = response.json()
    assert body["error"]["code"] == "provider_failed"
    run = body["data"]
    assert run["status"] == "provider_failed"
    rows = ledger_for(run["run_id"])
    assert [r["action_id"] for r in rows] == run["action_ids"]
    assert [r["status"] for r in rows] == ["executed"]
    # The only customer change is the one in the ledger.
    customers = {c["account_id"]: c for c in client.get("/api/customers").json()["data"]}
    assert [a for a, c in customers.items() if c["version"] != 1] == ["ACC-102"]


def test_live_without_provider_config_says_which_env_vars(client):
    response = live(client)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "provider_not_configured"
    assert set(error["details"]["missing_env"]) == {"LLM_PROVIDER", "LLM_API_KEY"}


def test_unknown_provider_name_is_rejected(client, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mystery")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    response = live(client)
    assert response.status_code == 503
    assert response.json()["error"]["details"]["missing_env"] == ["LLM_PROVIDER"]


# --- the Anthropic provider, without network ----------------------------------------------------

class StubMessages:
    def __init__(self, outcome):
        self.outcome, self.requests = outcome, []

    def create(self, **request):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def anthropic_provider(outcome):
    pytest.importorskip("anthropic")
    provider = agent.AnthropicProvider(model="claude-opus-5-5", api_key="test-key", timeout=5)
    provider.client = SimpleNamespace(messages=StubMessages(outcome))
    return provider


def test_anthropic_provider_maps_requests_and_tool_use():
    content = [SimpleNamespace(type="text", text="Checking."),
               SimpleNamespace(type="tool_use", id="tu_1", name="get_customer", input={"account_id": "ACC-102"})]
    provider = anthropic_provider(SimpleNamespace(stop_reason="tool_use", content=content))
    previous = agent.Turn(tool_calls=[agent.ToolCall("tu_0", "list_customers", {})], raw=["raw-blocks"])
    turn = provider.next_turn("SYS", [
        {"role": "user", "content": "go"},
        {"role": "assistant", "turn": previous},
        {"role": "tool", "results": [{"id": "tu_0", "content": "[]", "is_error": False}]},
    ], agent.tool_specs())

    assert turn.text == "Checking."
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [("tu_1", "get_customer", {"account_id": "ACC-102"})]
    [request] = provider.client.messages.requests
    assert request["model"] == "claude-opus-5-5" and request["system"] == "SYS"
    assert request["tools"] == agent.tool_specs()
    assert request["messages"] == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": ["raw-blocks"]},   # passed back unchanged
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_0", "content": "[]",
                                       "is_error": False}]},
    ]


def test_anthropic_provider_timeout_and_refusal_become_provider_errors():
    import anthropic
    import httpx

    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    with pytest.raises(agent.ProviderError, match="timeout"):
        anthropic_provider(timeout).next_turn("S", [{"role": "user", "content": "go"}], [])
    with pytest.raises(agent.ProviderError, match="refusal"):
        anthropic_provider(SimpleNamespace(stop_reason="refusal", content=[])).next_turn(
            "S", [{"role": "user", "content": "go"}], [])


# --- replay ------------------------------------------------------------------------------------

def replay(client, scenario=None, instruction="Archive customers inactive for over a year"):
    body = {"instruction": instruction, "mode": "replay"}
    if scenario:
        body["scenario"] = scenario
    return client.post(RUNS, json=body)


def test_replay_runs_through_the_real_gateway_and_is_labelled_replay(client):
    response = replay(client, "archive_inactive")
    assert response.status_code == 200
    run = response.json()["data"]
    assert (run["mode"], run["status"], run["scenario"], run["script_version"]) == (
        "replay", "completed", "archive_inactive", 1)
    assert {s["mode"] for s in run["steps"]} == {"replay"}
    rows = ledger_for(run["run_id"])
    assert len(rows) == 2 and {r["mode"] for r in rows} == {"replay"}
    assert [r["action_id"] for r in rows] == run["action_ids"]
    # Policy and verification ran for real: the actions are normal, undoable ledger rows.
    action = client.get(f"/api/actions/{run['action_ids'][0]}").json()["data"]
    assert action["mode"] == "replay"
    assert action["verification"]["verdict"] == "verified"
    assert client.post(f"/api/actions/{run['action_ids'][0]}/undo").status_code == 200


def test_replay_stops_at_a_block_and_later_steps_never_run(client):
    before_102 = [c for c in client.get("/api/customers").json()["data"] if c["account_id"] == "ACC-102"]
    run = replay(client, "blocked_protected").json()["data"]
    assert run["status"] == "stopped_blocked"
    assert len(run["steps"]) == 3          # script has 5 steps; it stopped at the block
    assert [r["status"] for r in ledger_for(run["run_id"])] == ["blocked"]
    assert [c for c in client.get("/api/customers").json()["data"] if c["account_id"] == "ACC-102"] == before_102


def test_replay_defaults_to_archive_inactive(client):
    assert replay(client).json()["data"]["scenario"] == "archive_inactive"


@pytest.mark.parametrize("scenario, status, code", [
    ("no_such_script", 404, "replay_not_found"),
    ("../main", 422, "invalid_request"),
])
def test_bad_replay_names(client, scenario, status, code):
    response = replay(client, scenario)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_every_shipped_replay_script_is_valid():
    names = sorted(p.stem for p in agent.REPLAYS_DIR.glob("*.json"))
    assert names == ["archive_inactive", "blocked_protected", "stale_report"]
    for name in names:
        script = agent.load_replay(name)
        assert script.version >= 1 and "not captured from a model run" in script.source


def test_live_mode_rejects_scenario(client):
    response = client.post(RUNS, json={"instruction": "x", "mode": "live", "scenario": "archive_inactive"})
    assert response.status_code == 422


def test_only_operators_start_runs(client):
    response = client.post(RUNS, headers={"X-Role": "approver", "X-Role-Key": "demo-approver-key"},
                           json={"instruction": "x", "mode": "replay"})
    assert response.status_code == 403
