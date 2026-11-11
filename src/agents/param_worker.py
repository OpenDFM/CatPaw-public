from pathlib import Path

from openhands.sdk import Agent, Tool
from openhands.sdk.context import Skill
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool

from src.agents.factory import create_phase_subagent
from src.tools.memory_tools import get_memory_read_tools_config
from src.tools.param_eval_tools import get_param_eval_tools_config
from src.tools.param_input_tools import get_param_input_tools_config


def create_vasp_param_worker_agent(parent_llm=None) -> Agent:
    """Create the combined parameter-planning and input-generation agent."""
    skills_root = Path(__file__).resolve().parent.parent / "skills"
    param_worker_skill = Skill.load(skills_root / "parameter-planning" / "param-worker" / "SKILL.md")
    convergence_recovery_skill = Skill.load(skills_root / "convergence-recovery" / "SKILL.md")
    return create_phase_subagent(
        role="param_worker",
        skills=[
            param_worker_skill,
            convergence_recovery_skill,
        ],
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
        ]
        + get_memory_read_tools_config("parameter")
        + get_param_eval_tools_config()
        + get_param_input_tools_config(),
    )
