"""Tool names and repeated operations through SDK-created child conversations."""

import json
from types import SimpleNamespace

import pytest
from litellm.types.utils import ModelResponse
from openhands.sdk import Conversation, TextContent, Tool
from openhands.sdk.conversation.state import ConversationExecutionStatus
from openhands.sdk.llm import LLMResponse, Message
from openhands.sdk.llm.message import MessageToolCall
from openhands.sdk.llm.utils.metrics import MetricsSnapshot
from openhands.sdk.tool.registry import get_tool_module_qualnames, resolve_tool
from openhands.tools.task.manager import TaskManager

import src.agents.factory as factory
import src.memory.store as store
from src.agents.coordinator import create_vasp_coordinator_agent
from src.graph.runner import SupervisorRunner


@pytest.fixture
def agent_config(monkeypatch, tmp_path):
    config = {
        "agents": {
            role: {
                "url": "https://example.test/v1",
                "api_key": "test",
                "model": "openai/gpt-4.1",
                "api": "chat_completions",
            }
            for role in ("coordinator", "modeling_worker")
        }
    }
    monkeypatch.setattr(factory, "load_config", lambda: config)
    monkeypatch.setattr(store, "load_config", lambda: config)
    monkeypatch.setattr(factory.tracing, "enabled", lambda: False)
    monkeypatch.setenv("VASP_WORKSPACE", str(tmp_path))


def test_custom_tool_names_match_exported_schemas(tmp_path):
    state = SimpleNamespace(workspace=SimpleNamespace(working_dir=str(tmp_path)))
    for name, module in get_tool_module_qualnames().items():
        if not module.startswith("src.tools."):
            continue
        definitions = resolve_tool(Tool(name=name), state)
        assert len(definitions) == 1
        tool = definitions[0]
        assert tool.name == name
        assert tool.to_openai_tool()["function"]["name"] == name
        assert tool.to_responses_tool()["name"] == name


@pytest.mark.parametrize("scenario", ["noop", "progress", "water"])
def test_child_dispatch_and_repetition(agent_config, monkeypatch, tmp_path, scenario):
    import openhands.sdk.agent.agent as agent_module

    calls = 0

    def complete(llm, messages, *, tools, on_token):
        nonlocal calls
        calls += 1
        assert calls <= 8, "Child did not stop its repeated operations"
        tool_names = {tool.to_openai_tool()["function"]["name"] for tool in tools}
        assert "BuildMoleculeInBox" in tool_names and "build_molecule_in_box" not in tool_names
        if (scenario == "water" and calls > 1) or (scenario != "water" and calls == 7):
            name, arguments = "finish", {"message": "done"}
        elif scenario == "water":
            name, arguments = (
                "BuildMoleculeInBox",
                {
                    "molecule_input": "H2O",
                    "box_size": 15,
                    "output_poscar": str(tmp_path / "POSCAR"),
                },
            )
        else:
            name = "terminal"
            arguments = {"command": "true" if scenario == "noop" else "echo tick >> progress.txt; wc -l < progress.txt"}
        return LLMResponse(
            message=Message(
                role="assistant",
                content=[TextContent(text=f"Step {calls}")],
                tool_calls=[
                    MessageToolCall(
                        id=f"call_{calls}",
                        name=name,
                        arguments=json.dumps({**arguments, "summary": f"Action {calls}"}),
                        origin="completion",
                    )
                ],
            ),
            metrics=MetricsSnapshot(),
            raw_response=ModelResponse(id=f"response_{calls}"),
        )

    monkeypatch.setattr(agent_module, "make_llm_completion", complete)
    SupervisorRunner.__new__(SupervisorRunner).register_subagents()
    parent = Conversation(
        agent=create_vasp_coordinator_agent(),
        workspace=str(tmp_path),
        persistence_dir=tmp_path / "conversations",
        visualizer=None,
    )
    try:
        task = TaskManager().start_task("Prepare water", subagent_type="modeling_worker_agent", conversation=parent)
        child = task.conversation
        assert isinstance(child.stuck_detector, factory.ActionStuckDetector)
        if scenario == "noop":
            assert child.state.execution_status == ConversationExecutionStatus.STUCK
            assert calls <= 5
        else:
            assert child.state.execution_status == ConversationExecutionStatus.FINISHED
            if scenario == "progress":
                assert (tmp_path / "progress.txt").read_text().splitlines() == ["tick"] * 6
            else:
                from pymatgen.io.vasp.inputs import Poscar

                structure = Poscar.from_file(tmp_path / "POSCAR", check_for_potcar=False).structure
                assert structure.composition.as_dict() == {"H": 2, "O": 1}
                assert structure.lattice.abc == pytest.approx((15, 15, 15))
    finally:
        parent.close()
