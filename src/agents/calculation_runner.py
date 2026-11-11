from pathlib import Path

from openhands.sdk import Agent, Tool
from openhands.sdk.context import Skill
from openhands.tools.terminal import TerminalTool

from src.agents.factory import create_phase_subagent
from src.tools.runner_tools import get_calculation_runner_tools_config


def create_vasp_calculation_runner_agent(parent_llm=None) -> Agent:
    """Create the job submission, monitoring, and result extraction agent."""
    skills_root = Path(__file__).resolve().parent.parent / "skills"
    job_submission_skill = Skill.load(skills_root / "job-submission" / "SKILL.md")

    return create_phase_subagent(
        role="calculation_runner",
        skills=[
            job_submission_skill,
        ],
        tools=[
            Tool(name=TerminalTool.name),
        ]
        + get_calculation_runner_tools_config(),
    )
