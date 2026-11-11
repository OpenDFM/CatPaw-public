from pathlib import Path

from openhands.sdk import Agent, Tool
from openhands.sdk.context import Skill
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

from src.agents.factory import create_phase_subagent
from src.tools.memory_tools import get_memory_update_tools_config
from src.tools.modeling.validation import get_modeling_validator_tools_config


def create_vasp_modeling_validator_agent(parent_llm=None) -> Agent:
    """Create the modeling validation agent."""
    skills_root = Path(__file__).resolve().parent.parent / "skills"
    modeling_validator_skill = Skill.load(skills_root / "structure-modeling" / "modeling-validator" / "SKILL.md")

    return create_phase_subagent(
        role="modeling_validator",
        skills=[modeling_validator_skill],
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
        ]
        + get_modeling_validator_tools_config()
        + get_memory_update_tools_config("modeling"),
    )
