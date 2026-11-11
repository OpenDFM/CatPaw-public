from pathlib import Path

from openhands.sdk import Agent, Tool
from openhands.sdk.context import Skill
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

from src.agents.factory import create_phase_subagent
from src.tools.memory_tools import get_memory_read_tools_config
from src.tools.modeling.advanced import get_modeler_tools_config
from src.tools.modeling.basis import get_modeling_basis_tools_config


def create_vasp_modeling_worker_agent(parent_llm=None) -> Agent:
    """Create the combined modeling planner/executor agent."""
    skills_root = Path(__file__).resolve().parent.parent / "skills"
    modeling_worker_skill = Skill.load(skills_root / "structure-modeling" / "modeling-worker" / "SKILL.md")

    return create_phase_subagent(
        role="modeling_worker",
        skills=[modeling_worker_skill],
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
        ]
        + get_memory_read_tools_config("modeling")
        + get_modeling_basis_tools_config()
        + get_modeler_tools_config(),
    )
