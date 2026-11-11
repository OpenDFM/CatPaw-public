from pathlib import Path

from openhands.sdk import Agent
from openhands.sdk.context import Skill

from src.agents.factory import create_phase_subagent
from src.tools.dag_tools import get_dag_tools_config
from src.tools.memory_tools import get_supervisor_memory_tools_config


def create_vasp_supervisor_agent(parent_llm=None) -> Agent:
    skills_root = Path(__file__).resolve().parent.parent / "skills"
    dag_orchestration_skill = Skill.load(skills_root / "dag-orchestration" / "SKILL.md")

    return create_phase_subagent(
        role="supervisor",
        skills=[
            dag_orchestration_skill,
        ],
        tools=get_supervisor_memory_tools_config() + get_dag_tools_config(),
    )
