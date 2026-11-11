from pathlib import Path

from openhands.sdk import Agent, Tool
from openhands.sdk.context import Skill
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

from src.agents.factory import create_phase_subagent


def create_vasp_reporter_agent(parent_llm=None) -> Agent:
    skills_root = Path(__file__).resolve().parent.parent / "skills"
    report_writing_skill = Skill.load(skills_root / "report-writing" / "SKILL.md")

    return create_phase_subagent(
        role="reporter",
        skills=[
            report_writing_skill,
        ],
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
        ],
    )
