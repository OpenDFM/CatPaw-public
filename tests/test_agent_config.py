"""Each registered child uses its own connection, including after SDK copying."""

import json

import src.agents.factory as factory
from src.agents.coordinator import create_vasp_coordinator_agent
from src.agents.modeling_validator import create_vasp_modeling_validator_agent
from src.agents.modeling_worker import create_vasp_modeling_worker_agent
from src.agents.param_validator import create_vasp_param_validator_agent
from src.agents.param_worker import create_vasp_param_worker_agent
from src.agents.supervisor import create_vasp_supervisor_agent
from src.graph.runner import SupervisorRunner


def test_api_retry_wait_is_published_for_the_dashboard(monkeypatch, tmp_path):
    webui = tmp_path / ".webui"
    webui.mkdir()
    monkeypatch.setenv("VASP_WORKSPACE", str(tmp_path))
    llm = factory.AgentLLM(
        usage_id="supervisor",
        model="openai/gpt-4.1",
        api_key="test",
        base_url="https://example.test/v1",
        num_retries=2,
        retry_min_wait=5,
        retry_max_wait=5,
        retry_multiplier=1,
    )
    attempts = 0
    observed = []

    @llm.retry_decorator(
        num_retries=2,
        retry_exceptions=(RuntimeError,),
        retry_min_wait=5,
        retry_max_wait=5,
        retry_multiplier=1,
    )
    def request() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary failure")
        return "ok"

    def inspect_wait(_seconds: float) -> None:
        files = list((webui / "api_wait").glob("*.json"))
        observed.append(json.loads(files[0].read_text()))

    request.retry.sleep = inspect_wait
    assert request() == "ok"
    assert observed[0]["role"] == "supervisor"
    assert observed[0]["reason"] == "RuntimeError"
    assert observed[0]["attempt"] == 1
    assert observed[0]["max_attempts"] == 2
    assert observed[0]["wait_seconds"] == 5


def test_registered_agent_uses_its_own_api_and_model(monkeypatch, tmp_path):
    monkeypatch.setattr(
        factory,
        "load_config",
        lambda: {
            "llm": {"api": "responses", "num_retries": 3, "retry_min_wait": 2, "retry_max_wait": 10},
            "agents": {
                "coordinator": {"url": "https://parent.example/v1", "api_key": "parent-key", "model": "openai/gpt-5.5"},
                "analyzer": {
                    "url": "https://child.example/v1",
                    "api_key": "child-key",
                    "model": "openai/gpt-4.1",
                    "api": "chat_completions",
                    "extra_body": {"chat_template_kwargs": {"thinking": True}},
                },
            }
        },
    )
    parent = create_vasp_coordinator_agent()
    runner = SupervisorRunner.__new__(SupervisorRunner)
    runner.register_subagents()
    from openhands.sdk.subagent.registry import get_agent_factory

    child = get_agent_factory("analyzer_agent").factory_func(parent.llm)

    assert parent.llm.uses_responses_api() is True
    assert parent.llm.stream is False
    assert parent.llm.num_retries == 3
    assert parent.llm.retry_min_wait == 2
    assert parent.llm.retry_max_wait == 10
    assert child.llm.model == "openai/gpt-4.1"
    assert child.llm.base_url == "https://child.example/v1"
    assert child.llm.api_key.get_secret_value() == "child-key"
    assert child.llm.uses_responses_api() is False
    assert child.llm.model_copy().uses_responses_api() is False
    assert child.llm.stream is False
    from openhands.sdk.llm.options.chat_options import select_chat_options

    expected_body = {"chat_template_kwargs": {"thinking": True}}
    assert parent.llm.litellm_extra_body == {}
    assert child.llm.litellm_extra_body == expected_body
    assert select_chat_options(child.llm.model_copy(), {}, has_tools=True)["extra_body"] == expected_body

    # Config, attached tools and rendered instructions must agree.
    import src.memory.store as store
    from src.tools.memory_tools import SearchMemoryAction, SearchMemoryExecutor

    agent_factories = {
        "supervisor": create_vasp_supervisor_agent,
        "modeling_worker": create_vasp_modeling_worker_agent,
        "modeling_validator": create_vasp_modeling_validator_agent,
        "param_worker": create_vasp_param_worker_agent,
        "param_validator": create_vasp_param_validator_agent,
        "coordinator": create_vasp_coordinator_agent,
    }
    roles = {
        "supervisor": "dag",
        "modeling_worker": "modeling",
        "modeling_validator": "modeling",
        "param_worker": "parameter",
        "param_validator": "parameter",
        "coordinator": None,
    }
    config = {
        "agents": {
            role: {"url": "https://example.test/v1", "api_key": "test", "model": "openai/gpt-4.1"} for role in roles
        }
    }
    monkeypatch.setattr(factory, "load_config", lambda: config)
    monkeypatch.setattr(store, "load_config", lambda: config)
    monkeypatch.setenv("VASP_WORKSPACE", str(tmp_path))
    all_categories = dict.fromkeys(["dag", "modeling", "parameter"], True)
    for categories, flags, review in [
        ({}, {}, False),
        ({"dag": True}, {}, False),
        ({"modeling": True}, {}, False),
        ({"parameter": True}, {}, False),
        (all_categories, {"WRITE": "0"}, True),
        (all_categories, {"READ": "0"}, False),
        (all_categories, {"ENABLED": "0"}, True),
    ]:
        config["memory"] = categories
        for flag in ("ENABLED", "READ", "WRITE"):
            monkeypatch.delenv(f"CATPAW_MEMORY_{flag}", raising=False)
        for flag, value in flags.items():
            monkeypatch.setenv(f"CATPAW_MEMORY_{flag}", value)
        (tmp_path / ".pipeline_context.json").write_text(
            json.dumps({"review_mode": "enabled" if review else "disabled"})
        )
        context = runner.memory_file_context()
        for category in all_categories:
            expected = categories.get(category, False) and flags.get("ENABLED") != "0"
            assert store.memory_enabled(category) == (expected and flags.get("READ") != "0")
            assert store.memory_enabled(category, "write") == (expected and flags.get("WRITE") != "0")
            assert (f"- {category}_memory:" in context) == store.memory_enabled(category)
            if not store.memory_enabled(category):
                result = SearchMemoryExecutor()(SearchMemoryAction(category=category, query="Pt"))
                assert json.loads(result.message)["status"] == "disabled"
        for role, category in roles.items():
            agent = agent_factories[role]()
            tool_names = {tool.name for tool in agent.tools}
            prompt = agent.static_system_message
            assert ("search_memory" in tool_names) == (
                bool(category)
                and store.memory_enabled(category)
                and role in {"supervisor", "modeling_worker", "param_worker"}
            )
            assert ("UpdateMemory" in tool_names) == (
                bool(category)
                and store.memory_enabled(category, "write")
                and role in {"supervisor", "modeling_validator", "param_validator"}
            )
            for tool in ("search_memory", "UpdateMemory"):
                assert (tool in prompt) == (tool in tool_names)
            if not review:
                assert "human-review" not in prompt.lower()
                assert "REVIEW_MODE" not in prompt
            if role == "coordinator":
                assert ("Human-review gates:" in prompt) == review
                assert ("human replies APPROVE" in prompt) == review
