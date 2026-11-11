from openhands.sdk import Agent, Tool
from openhands.tools.task import TaskToolSet
from openhands.tools.terminal import TerminalTool

from src.agents.factory import create_phase_subagent
from src.tools.repair_tools import PrepareNodeRepairTool


def create_vasp_coordinator_agent(parent_llm=None) -> Agent:
    return create_phase_subagent(
        role="coordinator",
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=PrepareNodeRepairTool.name),
            Tool(name=TaskToolSet.name),
        ],
    )
