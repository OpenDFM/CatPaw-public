"""Shared helpers for CatPaw agent construction."""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from litellm.exceptions import BadGatewayError
from openhands.sdk import LLM, Agent, AgentContext, Tool
from openhands.sdk.context import Skill
from openhands.sdk.conversation.stuck_detector import StuckDetector
from openhands.sdk.event import ActionEvent
from tenacity import RetryCallState, stop_never

import src.utils.tracing as tracing
from src.utils.config import load_config
from src.utils.results import read_pipeline_context, write_json

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


class ActionStuckDetector(StuckDetector):
    """Compare executed actions independently of the model's accompanying text."""

    def _event_eq(self, event1, event2):
        """Override the OpenHands comparison hook."""
        if isinstance(event1, ActionEvent) and isinstance(event2, ActionEvent):
            return (
                event1.source == event2.source
                and event1.tool_name == event2.tool_name
                and event1.action == event2.action
            )
        return super()._event_eq(event1, event2)


class TracedAgent(Agent):
    def step(self, conversation, on_event, on_token=None):
        detector = conversation.stuck_detector
        if detector is not None and not isinstance(detector, ActionStuckDetector):
            # SDK-created TaskToolSet children also enter here; replace once so
            # their normal run loop uses the same comparison and thresholds.
            conversation._stuck_detector = ActionStuckDetector(
                conversation.state,
                thresholds=detector.thresholds,
            )
        role = Path(self.system_prompt_filename).stem
        with tracing.span(f"agent.{role}", conversation.state.workspace.working_dir):
            return super().step(conversation, on_event, on_token)


class AgentLLM(LLM):
    api: Literal["chat_completions", "responses"] = "chat_completions"

    def retry_decorator(self, **kwargs):
        kwargs["retry_exceptions"] = (*kwargs["retry_exceptions"], BadGatewayError)
        decorate = super().retry_decorator(**kwargs)

        def wrap(function):
            wrapped = decorate(function)
            if kwargs["num_retries"] == 0:
                wrapped.retry.stop = stop_never
            return wrapped

        return wrap

    def log_retry_attempt(self, retry_state: RetryCallState) -> None:
        super().log_retry_attempt(retry_state)
        workspace = os.getenv("VASP_WORKSPACE")
        if workspace:
            webui = Path(workspace) / ".webui"
            if webui.is_dir():
                assert retry_state.outcome is not None
                error = retry_state.outcome.exception()
                assert error is not None
                assert retry_state.next_action is not None
                wait_seconds = retry_state.next_action.sleep
                now = datetime.now(UTC)
                directory = webui / "api_wait"
                directory.mkdir(exist_ok=True)
                write_json(
                    directory / f"{os.getpid()}-{self.usage_id}.json",
                    {
                        "role": self.usage_id,
                        "model": self.model,
                        "reason": type(error).__name__,
                        "attempt": retry_state.attempt_number,
                        "max_attempts": self.num_retries or None,
                        "wait_seconds": wait_seconds,
                        "retry_at": (now + timedelta(seconds=wait_seconds)).isoformat(),
                        "updated_at": now.isoformat(),
                    },
                )

    def uses_responses_api(self) -> bool:
        return self.api == "responses"


def create_agent_llm(role: str) -> AgentLLM:
    settings = load_config()
    config = settings["agents"][role]
    defaults = settings.get("llm", {})
    return AgentLLM(
        usage_id=role,
        api=config.get("api", defaults.get("api", "chat_completions")),
        model=config["model"],
        base_url=config["url"],
        api_key=config["api_key"],
        stream=False,
        litellm_extra_body=config.get("extra_body", {}),
        num_retries=int(defaults.get("num_retries", 0)),
        retry_min_wait=int(defaults.get("retry_min_wait", 60)),
        retry_max_wait=int(defaults.get("retry_max_wait", 1800)),
    )


def create_phase_subagent(
    *,
    role: str,
    tools: Sequence[Tool],
    skills: Sequence[Skill] | None = None,
) -> Agent:
    """Create a CatPaw agent with a capability-aware Jinja prompt."""
    template_path = PROMPTS_DIR / f"{role}.j2"
    if not template_path.exists():
        raise FileNotFoundError(f"Agent prompt template not found: {template_path}")

    workspace = os.getenv("VASP_WORKSPACE")
    pipeline_context = (read_pipeline_context(workspace) or {}) if workspace else {}
    context = AgentContext(skills=list(skills or []))
    return TracedAgent(
        llm=create_agent_llm(role),
        tools=list(tools),
        agent_context=context,
        system_prompt_filename=str(template_path),
        system_prompt_kwargs={
            "available_tools": [tool.name for tool in tools],
            "human_review": pipeline_context.get("review_mode") == "enabled",
        },
    )
