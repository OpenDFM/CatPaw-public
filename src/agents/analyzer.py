from pathlib import Path

from openhands.sdk import Agent, Tool
from openhands.sdk.context import Skill
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

from src.agents.factory import create_phase_subagent
from src.tools.analyzer_tools import get_analyzer_tools_config


def create_vasp_analyzer_agent(parent_llm=None) -> Agent:
    """Create the node-level calculation analysis agent."""
    skills_root = Path(__file__).resolve().parent.parent / "skills"
    result_analysis_skill = Skill.load(skills_root / "result-analysis" / "SKILL.md")

    return create_phase_subagent(
        role="analyzer",
        skills=[
            result_analysis_skill,
        ],
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
        ]
        + get_analyzer_tools_config(),
    )
