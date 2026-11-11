from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from typing import Any

import src.utils.tracing as tracing
from src.graph.restart import RestartPlan, apply_restart_plan, build_restart_plan
from src.graph.resume import (
    ACCEPT_RESULT,
    ANALYZE,
    CONTINUE,
    EXTRACT_ANALYZE,
    FAIL,
    KEEP,
    MONITOR,
    SKIP,
    ResumeDecision,
    append_recovery_log,
    build_resume_plan,
    runner_result_converged,
)
from src.graph.state import (
    DAG_DONE,
    DAG_FAILED,
    NODE_DONE,
    NODE_FAILED,
    NODE_READY,
    NODE_RUNNING,
    DAGNode,
    DAGState,
)
from src.memory.store import MEMORY_CATEGORIES, MemoryStore, memory_enabled
from src.utils.config import max_node_parallel
from src.utils.results import (
    BENCHMARK_RESULT_FILENAME,
    FINAL_REPORT_MD_FILENAME,
    MODELING_SUMMARY_FILENAME,
    MODELING_VALIDATION_FILENAME,
    NODE_RESULT_FILENAME,
    PARAMETER_SUMMARY_FILENAME,
    PARAMETER_VALIDATION_FILENAME,
    RUNNER_RESULT_FILENAME,
    read_completion_policy,
    read_node_result_metadata,
)
from src.utils.ui import (
    print_banner,
    print_error,
    print_info,
    print_step,
    print_success,
    print_warning,
)

RUN_METRICS_FILENAME = ".run_metrics.json"
REVIEW_LOG_FILENAME = ".review_log.jsonl"

SUBAGENTS_REGISTERED = False


def run_coordinator_child(
    *,
    workspace_path: str,
    user_command: str,
    node_payload: dict[str, Any],
    message: str,
    wave_index: int,
    resume_mode: bool,
    trace_parent: str | None = None,
) -> dict[str, Any]:
    node = DAGNode.from_dict(node_payload)
    runner = SupervisorRunner(
        workspace_path=workspace_path,
        user_command=user_command,
        human_review=False,
        input_path=None,
        resume=resume_mode,
    )
    with tracing.span(f"node.{node.node_id}", workspace_path, parent=trace_parent):
        metrics = runner.run_coordinator_agent(
            node=node,
            node_workspace=Path(workspace_path) / node.workspace_subdir,
            message=message,
            conversation_tag=f"COORDINATOR_{node.node_id}_RESTART_{node.restart_count}_ATTEMPT_{node.retry_count}",
        )
    # Batch export runs asynchronously; do not delay scientific results for a flush.
    return {"node_id": node.node_id, "metrics": metrics}


class SupervisorRunner:
    def __init__(
        self,
        workspace_path: str,
        user_command: str = "",
        human_review: bool = False,
        input_path: str | None = None,
        *,
        resume: bool = False,
        reconcile_only: bool = False,
        restart_nodes: list[str] | None = None,
        run_node: str | None = None,
        review_handler=None,
    ):
        self.workspace_path = str(Path(workspace_path).resolve())
        self.user_command = user_command
        self.human_review = human_review
        self.review_handler = review_handler
        self.input_path = input_path
        self.resume = resume
        self.reconcile_only = reconcile_only
        self.restart_nodes = list(restart_nodes or [])
        self.run_node = str(run_node).strip() if run_node else None
        self.metrics_waves: list[dict[str, Any]] = []
        self.max_expansion_rounds = max(int(os.getenv("CATPAW_MAX_EXPANSION_ROUNDS", "5").strip()), 0)
        self.max_node_parallel = max_node_parallel()
        self.review_dag_after_init = self.human_review and self.env_enabled("CATPAW_REVIEW_DAG", default=True)
        self.review_node_result = self.human_review and self.env_enabled("CATPAW_REVIEW_NODE_RESULT", default=True)
        self.register_subagents()

    def register_subagents(self) -> None:
        global SUBAGENTS_REGISTERED
        if SUBAGENTS_REGISTERED:
            return

        from openhands.sdk.subagent import register_agent

        from src.agents.analyzer import create_vasp_analyzer_agent
        from src.agents.calculation_runner import create_vasp_calculation_runner_agent
        from src.agents.coordinator import create_vasp_coordinator_agent
        from src.agents.modeling_validator import create_vasp_modeling_validator_agent
        from src.agents.modeling_worker import create_vasp_modeling_worker_agent
        from src.agents.param_validator import create_vasp_param_validator_agent
        from src.agents.param_worker import create_vasp_param_worker_agent

        registrations = [
            (
                "coordinator_agent",
                create_vasp_coordinator_agent,
                "Node-level coordinator: runs ModelingWorker, validators, ParamWorker, CalculationRunner, and final analysis",
            ),
            (
                "modeling_worker_agent",
                create_vasp_modeling_worker_agent,
                "Combined modeling planner/executor: prepares structures and writes modeling_summary.json",
            ),
            (
                "modeling_validator_agent",
                create_vasp_modeling_validator_agent,
                "Modeling validator: checks POSCAR artifacts and modeling_summary.json and returns PASS/FAIL JSON",
            ),
            (
                "param_worker_agent",
                create_vasp_param_worker_agent,
                "Combined parameter planner/input generator: writes parameter_summary.json and VASP input files",
            ),
            (
                "param_validator_agent",
                create_vasp_param_validator_agent,
                "Parameter validator: checks parameter_summary.json and VASP input files and returns PASS/FAIL JSON",
            ),
            (
                "calculation_runner_agent",
                create_vasp_calculation_runner_agent,
                "Calculation runner: submits prepared VASP jobs, monitors them, and extracts lightweight results",
            ),
            (
                "analyzer_agent",
                create_vasp_analyzer_agent,
                "Node-level analyzer: reads calculation status and lightweight results, then writes node_result.json",
            ),
        ]
        for name, factory, description in registrations:
            try:
                register_agent(name=name, factory_func=factory, description=description)
            except Exception as exc:
                message = str(exc).lower()
                if "already" not in message and "exists" not in message and "duplicate" not in message:
                    raise
        SUBAGENTS_REGISTERED = True

    def build_supervisor_prompt(self) -> str:
        if self.input_path:
            structure_ctx = (
                f"The user provided an initial structure file ({Path(self.input_path).name}). "
                "The Runner copied it to the workspace root and completed one-time POSCAR normalization. "
                "In task.md, state only that the initial structure has been provided by the user and is located in the node directory. "
                "Do not add inferred geometry details, literature constants, or recommended tolerances."
            )
        else:
            structure_ctx = (
                "The user did not provide an initial structure file. "
                "In task.md, state that ModelingWorker needs to retrieve and prepare the initial structure based on the task objective. "
                "Do not search for structures yourself. "
                "Keep this to source provenance only, without adding guessed structural parameters or convergence thresholds."
            )
        review_context = "- Human review: enabled\n" if self.human_review else ""
        prompt = (
            "Run context:\n"
            f"- Workspace: {self.workspace_path}\n"
            f"- Research objective: {self.user_command}\n"
            f"{review_context}\n"
            "Initial structure:\n"
            f"{structure_ctx}"
        )
        memory_context = self.memory_file_context()
        if memory_context:
            prompt += f"\n\n{memory_context}"
        return prompt

    @staticmethod
    def env_enabled(name: str, *, default: bool = True) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    def memory_file_context(self) -> str:
        categories = [category for category in MEMORY_CATEGORIES if memory_enabled(category)]
        if not categories:
            return ""
        store = MemoryStore()
        return (
            "CatPaw memory files:\n"
            + "".join(f"- {category}_memory: {store.path_for_category(category)}\n" for category in categories)
            + "Before planning, search relevant memory with search_memory when that tool is available. "
            "Use compact keyword queries separated by |, such as IrO2|slab_opt|LDAU; "
            "plain whitespace-separated queries are treated as OR terms by the tool. "
            "Use a memory only when scope matches the current task; treat confidence=weak as soft guidance "
            "and confidence=needs_confirmation as something to verify before applying."
        )

    def run_agent(
        self,
        *,
        agent,
        prompt: str,
        conversation_tag: str,
        step_title: str,
        workspace: str | None = None,
        visualizer_name: str = "VASP-AGENT",
        human_review_loop: bool = False,
        max_responses: int = 10,
    ) -> dict[str, Any]:
        from openhands.sdk import Conversation
        from openhands.sdk.conversation.state import ConversationExecutionStatus
        from openhands.tools.delegate import DelegationVisualizer

        conversation_workspace = workspace or self.workspace_path
        print_step(step_title)
        os.environ["VASP_WORKSPACE"] = self.workspace_path
        base_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"{self.workspace_path}:{conversation_workspace}")
        conversation_id = uuid.uuid5(base_uuid, conversation_tag)
        conv = Conversation(
            agent=agent,
            workspace=conversation_workspace,
            persistence_dir=Path(self.workspace_path) / ".openhands" / "conversations",
            conversation_id=conversation_id,
            visualizer=DelegationVisualizer(name=visualizer_name),
        )
        try:
            has_events = len(conv.state.events) > 0
            pending_human_review = bool(
                has_events
                and human_review_loop
                and conv.state.execution_status == ConversationExecutionStatus.FINISHED
                and not self.agent_used_finish(conv)
            )
            if not has_events or (
                conv.state.execution_status == ConversationExecutionStatus.FINISHED and not pending_human_review
            ):
                conv.send_message(prompt)
            if human_review_loop:
                self.run_conversation_until_human_review_done(conv, max_responses=max_responses)
            else:
                conv.run()
            metrics = self.extract_metrics(conv, conversation_tag)
        finally:
            conv.close()

        return metrics

    @staticmethod
    def events_after_last_user_message(conversation) -> list[Any]:
        events = list(getattr(conversation.state, "events", []) or [])
        last_user_message_id = getattr(conversation.state, "last_user_message_id", None)
        if last_user_message_id is None:
            return events

        for index, event in enumerate(events):
            if getattr(event, "id", None) == last_user_message_id:
                return events[index + 1 :]
        return events

    @staticmethod
    def agent_used_finish(conversation) -> bool:
        from openhands.sdk.event import ActionEvent
        from openhands.sdk.tool.builtins.finish import FinishAction

        return any(
            isinstance(event, ActionEvent) and isinstance(event.action, FinishAction)
            for event in reversed(SupervisorRunner.events_after_last_user_message(conversation))
        )

    def run_conversation_until_human_review_done(self, conversation, *, max_responses: int) -> None:
        from openhands.sdk.conversation.state import ConversationExecutionStatus
        from openhands.sdk.event import MessageEvent

        has_asked_human = False
        for _ in range(max(max_responses, 1)):
            conversation.run()

            if conversation.state.execution_status in {
                ConversationExecutionStatus.ERROR,
                ConversationExecutionStatus.STUCK,
            }:
                raise RuntimeError(f"Conversation stopped with status: {conversation.state.execution_status.value}")

            if conversation.state.execution_status != ConversationExecutionStatus.FINISHED:
                print_info(f"Conversation paused with status: {conversation.state.execution_status.value}")
                return

            if self.agent_used_finish(conversation):
                if has_asked_human:
                    return
                conversation.send_message(
                    "Before using the finish tool, ask the human to review the validator result. "
                    "Only use finish after receiving the human response."
                )
                continue

            last_message = None
            for event in reversed(self.events_after_last_user_message(conversation)):
                if isinstance(event, MessageEvent) and event.source == "agent":
                    last_message = str(event.llm_message.content)
                    break
            if not last_message:
                conversation.send_message(
                    "Please continue. Use the finish tool only after asking the human "
                    "for review when a configured review gate requires it and receiving the human response."
                )
                continue

            has_asked_human = True
            print("\nAgent asks human:")
            print(last_message)
            human_response = self.read_human_review_response(
                "conversation", last_message, conversation_id=str(conversation.id)
            )
            self.append_review_log("conversation", human_response, conversation_id=str(conversation.id))
            conversation.send_message(human_response)

    def read_human_review_response(self, kind: str, prompt: str, **context) -> str:
        if self.review_handler is not None:
            return self.review_handler(kind, prompt, **context)
        while True:
            try:
                response = input("\nHuman response (APPROVE/REVISE; waits indefinitely): ").strip()
            except EOFError as exc:
                raise RuntimeError(
                    "Human review requires an interactive stdin; review was not approved. "
                    "Resume this workspace with --review in a persistent interactive terminal."
                ) from exc

            normalized = response.upper()
            if normalized.startswith("APPROVE") or normalized.startswith("REVISE"):
                return response
            print_warning("Review is still pending; reply with APPROVE or REVISE followed by optional details.")

    def append_review_log(
        self,
        kind: str,
        response: str,
        *,
        node_id: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            "node_id": node_id,
            "conversation_id": conversation_id,
            "response": response,
            "automatic": False,
        }
        path = Path(self.workspace_path) / REVIEW_LOG_FILENAME
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def review_requests_revision(response: str) -> bool:
        return response.lstrip().upper().startswith("REVISE")

    def review_initial_dag_if_enabled(self) -> None:
        if not self.review_dag_after_init:
            return

        state = DAGState.load(self.workspace_path)
        print_banner("HUMAN REVIEW: DAG PLAN")
        print(f"Workspace: {self.workspace_path}")
        print(f"DAG status: {state.status}")
        print("Planned nodes:")
        for node in state.nodes:
            deps = ", ".join(node.dependencies) if node.dependencies else "none"
            print(f"- {node.node_id}: {node.title} | depends_on={deps} | task={node.task_md_path}")
        print("Reply APPROVE to start the DAG, or REVISE with requested DAG changes to stop before execution.")

        response = self.read_human_review_response(
            "dag_plan", "请检查 DAG 节点、依赖和任务说明。批准后开始执行；要求修改会停止本次运行。"
        )
        self.append_review_log("dag_plan", response)
        if self.review_requests_revision(response):
            raise RuntimeError(f"DAG plan review requested revision: {response}")
        print_success("DAG plan review approved; starting scheduler.")

    def review_node_result_if_enabled(
        self,
        node: DAGNode,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.review_node_result:
            return metadata

        revision_round = 0
        while True:
            print_banner(f"HUMAN REVIEW: NODE RESULT {node.node_id}")
            node_status = str(metadata.get("node_status") or "unknown")
            error = str(metadata.get("error") or "")
            artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
            node_dir = Path(self.workspace_path) / node.workspace_subdir
            print(f"Node: {node.node_id} ({node.title})")
            print(f"Workspace: {node_dir}")
            print(f"node_status: {node_status}")
            if error:
                print(f"error: {error}")
            node_result_path = artifacts.get("node_result") or str(node_dir / NODE_RESULT_FILENAME)
            print(f"node_result: {node_result_path}")
            if artifacts:
                print("Artifacts:")
                for key, value in sorted(artifacts.items()):
                    print(f"- {key}: {value}")
            print(
                "Reply APPROVE to accept this node result for downstream use, or REVISE with "
                "requested analysis changes. REVISE reruns Analyzer only and preserves all VASP work."
            )

            response = self.read_human_review_response(
                "node_result",
                "请检查节点结果。要求修改只重跑 Analyzer，保留 VASP 计算产物。",
                node_id=node.node_id,
                result=metadata,
            )
            self.append_review_log("node_result", response, node_id=node.node_id)
            if not self.review_requests_revision(response):
                print_success(f"Node result review approved: {node.node_id}")
                return metadata

            revision_round += 1
            metadata = self.rerun_analyzer_for_node_review(
                node,
                feedback=response,
                revision_round=revision_round,
            )

    def rerun_analyzer_for_node_review(
        self,
        node: DAGNode,
        *,
        feedback: str,
        revision_round: int,
    ) -> dict[str, Any]:
        from src.agents.analyzer import create_vasp_analyzer_agent

        node_dir = Path(self.workspace_path) / node.workspace_subdir
        result_path = node_dir / NODE_RESULT_FILENAME
        previous_result = result_path.read_bytes() if result_path.is_file() else None

        print_warning(
            f"Node result revision requested for {node.node_id}; rerunning Analyzer only. "
            "Existing VASP calculations and runner handoffs will be preserved."
        )
        metrics = self.run_agent(
            agent=create_vasp_analyzer_agent(),
            prompt=(
                f"PROJECT_ROOT={Path(__file__).resolve().parents[2]}\n"
                f"ACTIVE_NODE_DIR={node_dir}\n"
                f"TASK_MD_PATH={node_dir / 'task.md'}\n"
                f"NODE_ID={node.node_id}\n"
                f"NODE_TITLE={node.title}\n"
                f"ORIGINAL_USER_GOAL={self.user_command}\n"
                f"NODE_GOAL={node.goal}\n"
                f"COMPLETION_POLICY={json.dumps(read_completion_policy(self.workspace_path), ensure_ascii=False, sort_keys=True)}\n"
                f"HUMAN_REVIEW_REVISION_ROUND={revision_round}\n"
                f"HUMAN_REVIEW_FEEDBACK={feedback}\n\n"
                "The current node_result.json is a rejected analysis draft. Reread task.md and all "
                "existing modeling, parameter, runner, and calculation evidence, address only the "
                "human feedback that is supported by that evidence, and call WriteNodeResult to "
                "rewrite node_result.json. Do not submit, monitor, restart, or modify any VASP "
                "calculation, input artifact, runner_result.json, .job_id, or DAG state."
            ),
            conversation_tag=f"ANALYZER_REVIEW_{node.node_id}_RESTART_{node.restart_count}_ROUND_{revision_round}",
            step_title=f"ANALYZER REVIEW: Revise node result {node.node_id}",
            workspace=str(node_dir),
            visualizer_name=f"VASP-ANALYZER-REVIEW-{node.node_id}",
        )
        self.metrics_waves.append(metrics)

        if not result_path.is_file():
            raise RuntimeError(f"Analyzer review revision did not write {NODE_RESULT_FILENAME} for {node.node_id}.")
        revised_result = result_path.read_bytes()
        if previous_result is not None and revised_result == previous_result:
            raise RuntimeError(f"Analyzer review revision did not change {NODE_RESULT_FILENAME} for {node.node_id}.")

        metadata = read_node_result_metadata(str(node_dir))
        if metadata is None:
            raise RuntimeError(f"Analyzer review revision wrote an invalid {NODE_RESULT_FILENAME} for {node.node_id}.")
        return metadata

    def run_supervisor_agent(self, prompt: str, *, conversation_tag: str, step_title: str) -> dict[str, Any]:
        from src.agents.supervisor import create_vasp_supervisor_agent

        return self.run_agent(
            agent=create_vasp_supervisor_agent(),
            prompt=prompt,
            conversation_tag=conversation_tag,
            step_title=step_title,
            visualizer_name="VASP-SUPERVISOR",
        )

    def run_coordinator_agent(
        self,
        *,
        node: DAGNode,
        node_workspace: Path,
        message: str,
        conversation_tag: str,
    ) -> dict[str, Any]:
        from src.agents.coordinator import create_vasp_coordinator_agent

        memory_context = self.memory_file_context()
        memory_context = f"\n{memory_context}\n" if memory_context else ""
        completion_policy = read_completion_policy(self.workspace_path)
        resume_context = (
            "RESUME_MODE=enabled\n"
            "Inspect existing handoffs before acting. Preserve valid PASS artifacts; resume from the first incomplete phase. "
            "If any .job_id exists, never submit or resubmit that calculation; only monitor and extract it.\n"
            if self.resume
            else "RESUME_MODE=disabled\n"
        )
        review_context = "REVIEW_MODE=enabled\n" if self.human_review else ""
        prompt = (
            f"PROJECT_ROOT={Path(__file__).resolve().parents[2]}\n"
            f"ACTIVE_NODE_DIR={node_workspace}\n"
            f"TASK_MD_PATH={node_workspace / 'task.md'}\n"
            f"NODE_ID={node.node_id}\n"
            f"NODE_TITLE={node.title}\n\n"
            f"ORIGINAL_USER_GOAL={self.user_command}\n"
            f"NODE_GOAL={node.goal}\n"
            f"{review_context}"
            f"COMPLETION_POLICY={json.dumps(completion_policy, ensure_ascii=False, sort_keys=True)}\n"
            f"{resume_context}"
            "Use ORIGINAL_USER_GOAL only as read-only intent context. "
            "Do not expand scope beyond this DAG node's task.md and NODE_GOAL.\n\n"
            f"{memory_context}"
            f"{message}\n\n"
            "Run the complete Coordinator loop for this single DAG node. "
            "Use the ModelingWorker, ModelingValidator, ParamWorker, ParamValidator, CalculationRunner, and Analyzer task flow, "
            "then ensure Analyzer writes node_result.json as the DAG completion contract."
        )
        return self.run_agent(
            agent=create_vasp_coordinator_agent(),
            prompt=prompt,
            conversation_tag=conversation_tag,
            step_title=f"COORDINATOR: Execute DAG node {node.node_id}",
            workspace=str(node_workspace),
            visualizer_name=f"VASP-COORDINATOR-{node.node_id}",
            human_review_loop=self.human_review,
        )

    @staticmethod
    def extract_metrics(conv, tag: str) -> dict[str, Any]:
        try:
            combined = conv.conversation_stats.get_combined_metrics()
            usage = combined.accumulated_token_usage
            return {
                "tag": tag,
                "cost": combined.accumulated_cost,
                "api_calls": len(combined.token_usages),
                "tokens": {
                    "prompt": usage.prompt_tokens,
                    "completion": usage.completion_tokens,
                    "cache_read": usage.cache_read_tokens,
                    "cache_write": usage.cache_write_tokens,
                },
            }
        except Exception:
            return {
                "tag": tag,
                "cost": 0,
                "api_calls": 0,
                "tokens": {"prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0},
            }

    def find_initial_structure(self, node_dir: Path) -> Path | None:
        poscar_path = node_dir / "POSCAR"
        if poscar_path.is_file():
            return poscar_path

        candidates = sorted(
            [
                *node_dir.glob("*.cif"),
                *node_dir.glob("*.xsd"),
            ]
        )

        if not candidates:
            return None
        if len(candidates) > 1:
            names = ", ".join(path.name for path in candidates)
            raise RuntimeError(f"Multiple initial structure files detected; keep only one: {names}")
        return candidates[0]

    def convert_structure_to_poscar(self, structure_path: Path, node_dir: Path) -> Path:
        from pymatgen.core import Structure

        poscar_path = node_dir / "POSCAR"
        print_step(f"Detected initial structure file {structure_path.name}; converting to POSCAR...")

        element_list = [
            "H",
            "He",
            "Li",
            "Be",
            "B",
            "C",
            "N",
            "O",
            "F",
            "Ne",
            "Na",
            "Mg",
            "Al",
            "Si",
            "P",
            "S",
            "Cl",
            "Ar",
            "K",
            "Ca",
            "Sc",
            "Ti",
            "V",
            "Cr",
            "Mn",
            "Fe",
            "Co",
            "Ni",
            "Cu",
            "Zn",
            "Ga",
            "Ge",
            "As",
            "Se",
            "Br",
            "Kr",
            "Rb",
            "Sr",
            "Y",
            "Zr",
            "Nb",
            "Mo",
            "Tc",
            "Ru",
            "Rh",
            "Pd",
            "Ag",
            "Cd",
            "In",
            "Sn",
            "Sb",
            "Te",
            "I",
            "Xe",
            "Cs",
            "Ba",
            "La",
            "Ce",
            "Pr",
            "Nd",
            "Pm",
            "Sm",
            "Eu",
            "Gd",
            "Tb",
            "Dy",
            "Ho",
            "Er",
            "Tm",
            "Yb",
            "Lu",
            "Hf",
            "Ta",
            "W",
            "Re",
            "Os",
            "Ir",
            "Pt",
            "Au",
            "Hg",
            "Tl",
            "Pb",
            "Bi",
            "Po",
            "At",
            "Rn",
            "Fr",
            "Ra",
            "Ac",
            "Th",
            "Pa",
            "U",
            "Np",
            "Pu",
            "Am",
            "Cm",
            "Bk",
            "Cf",
            "Es",
            "Fm",
            "Md",
            "No",
            "Lr",
        ]

        structure = Structure.from_file(str(structure_path))
        sorted_elements = sorted(
            list(set(structure.symbol_set)),
            key=lambda symbol: element_list.index(symbol) if symbol in element_list else 999,
        )
        structure.sort(key=lambda site: sorted_elements.index(site.specie.symbol))
        structure.to(fmt="poscar", filename=str(poscar_path))
        print_success(f"Standardized POSCAR generated: {poscar_path}")
        return poscar_path

    def print_restart_plan(self, plan: RestartPlan) -> None:
        print_banner("CATPAW SCIENTIFIC RESTART PLAN")
        print(f"Workspace: {plan.workspace_path}")
        print("Requested nodes: " + ", ".join(plan.requested_nodes))
        print("Affected nodes (requested + transitive descendants):")
        for node_id in plan.affected_nodes:
            evidence = plan.job_evidence.get(node_id)
            suffix = f" | jobs={json.dumps(evidence, ensure_ascii=False)}" if evidence else ""
            print(f"- {node_id}: {plan.prior_statuses[node_id]} -> restart{suffix}")
        print("Preserved nodes: " + (", ".join(plan.preserved_nodes) or "none"))
        print(f"Archive destination: {plan.archive_root}")
        if plan.blocked_reasons:
            print_warning("Restart is blocked:")
            for reason in plan.blocked_reasons:
                print_warning(f"- {reason}")

    def review_restart_plan_if_enabled(self, plan: RestartPlan) -> None:
        if not self.human_review:
            return
        print(
            "Reply APPROVE to archive the affected subgraph and restart it, or "
            "REVISE to stop without changing the workspace."
        )
        response = self.read_human_review_response("restart_plan", "批准将归档受影响节点并重新运行；要求修改将停止。")
        self.append_review_log("restart_plan", response)
        if self.review_requests_revision(response):
            raise RuntimeError(f"Scientific restart review requested revision: {response}")

    def run(self) -> str:
        if self.reconcile_only:
            return self.run_workflow()
        with tracing.workflow(self.workspace_path):
            return self.run_workflow()

    def run_workflow(self) -> str:
        print_banner("RESUMING CATPAW WORKSPACE" if self.resume else "INITIALIZING CATPAW SUPERVISOR")
        if self.input_path and not self.resume:
            node_dir = Path(self.workspace_path)
            initial_structure = self.find_initial_structure(node_dir)
            if initial_structure is not None:
                self.convert_structure_to_poscar(initial_structure, node_dir)

        try:
            resume_changed = False
            if self.resume:
                if not DAGState.exists(self.workspace_path):
                    raise RuntimeError("Resume workspace has no .dag_state.json.")
                if self.run_node:
                    return self.run_single_node(self.run_node)
                if self.restart_nodes:
                    restart_plan = build_restart_plan(
                        DAGState.load(self.workspace_path),
                        self.restart_nodes,
                    )
                    self.print_restart_plan(restart_plan)
                    if self.reconcile_only:
                        return "RECONCILE_ONLY"
                    if restart_plan.blocked_reasons:
                        raise RuntimeError("Scientific restart is blocked: " + "; ".join(restart_plan.blocked_reasons))
                    self.review_restart_plan_if_enabled(restart_plan)
                    archive_root = apply_restart_plan(restart_plan)
                    print_success(f"Scientific restart archived old evidence at: {archive_root}")
                    resume_changed = True
                resume_changed = self.reconcile_resume_workspace() or resume_changed
                if self.reconcile_only:
                    return "RECONCILE_ONLY"
            else:
                metrics = self.run_supervisor_agent(
                    self.build_supervisor_prompt(),
                    conversation_tag="SUPERVISOR_MAIN",
                    step_title="SUPERVISOR: Build DAG and task files",
                )
                self.metrics_waves.append(metrics)
                if not DAGState.exists(self.workspace_path):
                    raise RuntimeError("Supervisor did not initialize the DAG.")
                self.review_initial_dag_if_enabled()

            self.run_dag_scheduler_loop()
            final_state = DAGState.load(self.workspace_path)
            if final_state.status != DAG_DONE:
                failed = [node.node_id for node in final_state.nodes if node.status == NODE_FAILED]
                unresolved = [node.node_id for node in final_state.unresolved_nodes()]
                raise RuntimeError(
                    "DAG is incomplete; "
                    f"failed={failed or 'none'}, unresolved={unresolved or 'none'}. "
                    "No successful final report was generated."
                )

            print_success("Supervisor DAG completed successfully.")
            final_report = Path(self.workspace_path) / FINAL_REPORT_MD_FILENAME
            if resume_changed or not final_report.is_file():
                metrics = self.run_reporter_agent()
                self.metrics_waves.append(metrics)
            else:
                print_info(f"Existing completed report preserved: {final_report}")
            return self.detect_latest_job_id()
        finally:
            if not self.reconcile_only:
                self.write_metrics_summary()

    def run_single_node(self, node_id: str) -> str:
        """Run one ready node or resume one running node without entering the scheduler."""
        from src.tools.dag_tools import DAGControlExecutor

        state = DAGState.load(self.workspace_path)
        try:
            node = state.get_node(node_id)
        except KeyError as exc:
            raise ValueError(f"Unknown DAG node: {node_id}") from exc

        if node.status == NODE_DONE:
            print_info(f"DAG node is already done: {node_id}")
            return self.detect_latest_job_id()
        if node.status == NODE_FAILED:
            decision = next(
                (item for item in build_resume_plan(state, persist_monitor_state=True) if item.node_id == node_id),
                None,
            )
            if decision is None or decision.action not in {MONITOR, CONTINUE}:
                action = decision.action if decision is not None else "missing"
                raise RuntimeError(f"DAG node {node_id} cannot be resumed in place: reconciliation action is {action}.")
            print_info(f"Resuming DAG node {node_id} from existing jobs or its reserved repair.")
            state.requeue_for_resume(node_id)
            state = DAGState.load(self.workspace_path)
            state.mark_running(node_id)
            append_recovery_log(self.workspace_path, decision, outcome=NODE_RUNNING)
            state = DAGState.load(self.workspace_path)
            node = state.get_node(node_id)
        if node.status not in {NODE_READY, NODE_RUNNING}:
            raise RuntimeError(f"DAG node {node_id} must be ready or running, found {node.status}.")

        if node.status == NODE_READY:
            self.run_ready_node(
                DAGControlExecutor(workspace_dir=self.workspace_path),
                node,
                review_mode="enabled" if self.human_review else "disabled",
                wave_index=state.scheduler_wave,
            )
        else:
            node_workspace = Path(self.workspace_path) / node.workspace_subdir
            tag = f"COORDINATOR_{node.node_id}_RESTART_{node.restart_count}_ATTEMPT_{node.retry_count}_RUN_NODE"
            message = (
                "Resume this already-running DAG node in place. Existing .job_id markers "
                "are authoritative; do not resubmit them. Continue monitoring, extraction, "
                "and analysis from the first incomplete phase."
            )
            try:
                metrics = self.run_coordinator_agent(
                    node=node,
                    node_workspace=node_workspace,
                    message=message,
                    conversation_tag=tag,
                )
                self.metrics_waves.append(metrics)
                self.update_node_from_node_result(node)
            except Exception as exc:
                if not self.recover_completed_node_after_coordinator_error(node, exc):
                    state = DAGState.load(self.workspace_path)
                    state.mark_failed(node.node_id, str(exc))
                    raise

        final_node = DAGState.load(self.workspace_path).get_node(node_id)
        if final_node.status != NODE_DONE:
            raise RuntimeError(f"Single-node worker stopped without completing {node_id}: {final_node.status}.")
        return self.detect_latest_job_id()

    def reconcile_resume_workspace(self) -> bool:
        state = DAGState.load(self.workspace_path)
        decisions = build_resume_plan(
            state,
            persist_monitor_state=not self.reconcile_only,
        )
        print_banner("CATPAW RESUME RECONCILIATION")
        for decision in decisions:
            print(
                f"- {decision.node_id}: {decision.action} | {decision.reason} | "
                f"evidence={json.dumps(decision.evidence, ensure_ascii=False)}"
            )
        if self.reconcile_only:
            return False

        changed = False
        for decision in decisions:
            outcome = self.apply_resume_decision(decision)
            append_recovery_log(self.workspace_path, decision, outcome=outcome)
            changed = changed or decision.action not in {SKIP, KEEP}
        return changed

    def apply_resume_decision(self, decision: ResumeDecision) -> str:
        state = DAGState.load(self.workspace_path)
        node = state.get_node(decision.node_id)

        if decision.action in {SKIP, KEEP}:
            return "unchanged"
        if decision.action == ACCEPT_RESULT:
            self.update_node_from_node_result(node)
            return DAGState.load(self.workspace_path).get_node(node.node_id).status
        if decision.action == ANALYZE:
            recovered = self.finalize_converged_node(node, reason=decision.reason)
            return "done" if recovered else "analyzer_recovery_failed"
        if decision.action == EXTRACT_ANALYZE:
            recovered = self.extract_and_finalize_existing_node(node)
            return "done" if recovered else "extraction_or_analysis_failed"
        if decision.action in {CONTINUE, MONITOR}:
            state.requeue_for_resume(node.node_id)
            return DAGState.load(self.workspace_path).get_node(node.node_id).status
        if decision.action == FAIL:
            state.mark_failed(node.node_id, decision.reason)
            return "failed"
        raise ValueError(f"Unknown resume action: {decision.action}")

    def extract_and_finalize_existing_node(self, node: DAGNode) -> bool:
        from src.tools.runner_tools import (
            ExtractCalcResultsAction,
            ExtractCalcResultsExecutor,
        )

        node_dir = Path(self.workspace_path) / node.workspace_subdir
        observation = ExtractCalcResultsExecutor(workspace_dir=str(node_dir))(
            ExtractCalcResultsAction(workspace_path=str(node_dir))
        )
        if not runner_result_converged(node_dir):
            state = DAGState.load(self.workspace_path)
            state.mark_failed(node.node_id, observation.summary_text)
            return False
        return self.finalize_converged_node(node, reason="existing Slurm job converged")

    def run_dag_scheduler_loop(self) -> None:
        """The parent owns DAG writes; completed work immediately releases dependents."""
        from src.tools.dag_tools import DAGControlExecutor

        dag_control = DAGControlExecutor(workspace_dir=self.workspace_path)
        state = DAGState.load(self.workspace_path)
        self.expansion_rounds = state.expansion_rounds
        parallelism = 1 if self.human_review else self.max_node_parallel
        review_mode = "enabled" if self.human_review else "disabled"
        # SDK/tracing threads already exist: do not inherit their locks through fork.
        with ProcessPoolExecutor(max_workers=parallelism, mp_context=get_context("spawn")) as executor:
            active = {}
            while True:
                state = DAGState.load(self.workspace_path)
                ready = state.get_ready_nodes()[: parallelism - len(active)]
                if ready:
                    if self.human_review:
                        self.run_ready_node(
                            dag_control, ready[0], review_mode=review_mode, wave_index=state.scheduler_wave
                        )
                        continue
                    started = self.start_ready_node_batch(dag_control, ready, review_mode=review_mode)
                    for node, message in started.values():
                        future = executor.submit(
                            run_coordinator_child,
                            workspace_path=self.workspace_path,
                            user_command=self.user_command,
                            node_payload=node.to_dict(),
                            message=message,
                            wave_index=state.scheduler_wave,
                            resume_mode=self.resume,
                            trace_parent=tracing.parent_context(),
                        )
                        active[future] = node.node_id
                if active:
                    finished, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in finished:
                        node_id = active.pop(future)
                        node = DAGState.load(self.workspace_path).get_node(node_id)
                        try:
                            self.metrics_waves.append(future.result()["metrics"])
                            self.update_node_from_node_result(node)
                        except Exception as exc:
                            self.handle_node_error(node, exc)
                    continue
                state = DAGState.load(self.workspace_path)
                if state.get_ready_nodes():
                    continue
                # Replanning runs only at quiescence, never blocks runnable work.
                state.scheduler_wave += 1
                state.save()
                self.post_wave_supervise(wave_index=state.scheduler_wave)
                state = DAGState.load(self.workspace_path)
                if not state.get_ready_nodes():
                    if state.status != DAG_DONE:
                        print_warning("DAG stopped: no runnable work remains; inspect failed nodes.")
                    return

    def handle_node_error(self, node: DAGNode, error: Exception) -> None:
        if not self.recover_completed_node_after_coordinator_error(node, error):
            DAGState.load(self.workspace_path).mark_failed(node.node_id, str(error))
            print_error(f"Coordinator failed for node {node.node_id}: {error}")

    def start_ready_node_batch(
        self,
        dag_control: Any,
        batch: list[DAGNode],
        *,
        review_mode: str,
    ) -> dict[str, tuple[DAGNode, str]]:
        from src.tools.dag_tools import DAGControlAction

        started: dict[str, tuple[DAGNode, str]] = {}
        for node in batch:
            response = dag_control(
                DAGControlAction(
                    command="start",
                    node_ids=[node.node_id],
                    review_mode=review_mode,
                )
            )
            payload = json.loads(response.message)
            state = DAGState.load(self.workspace_path)
            if payload["status"] != "success":
                error = payload["message"]
                state.mark_failed(node.node_id, error)
                print_error(f"Failed to start DAG node {node.node_id}: {error}")
                continue
            started[node.node_id] = (state.get_node(node.node_id), payload["ready_nodes"][0]["message"])
        return started

    def post_wave_supervise(self, *, wave_index: int) -> None:
        """At quiescence, append missing work or retry eligible failed nodes."""
        state = DAGState.load(self.workspace_path)
        retry_eligible = self.retry_eligible_nodes(state)
        expansion_open = self.expansion_rounds < self.max_expansion_rounds
        if not retry_eligible and not expansion_open:
            if state.status != DAG_FAILED:
                if self.max_expansion_rounds == 0:
                    print_info("Dynamic DAG expansion is disabled by CATPAW_MAX_EXPANSION_ROUNDS=0.")
                else:
                    print_info(
                        f"Dynamic DAG expansion limit reached ({self.max_expansion_rounds}); "
                        "continuing with the current DAG."
                    )
            return

        before_ids = {node.node_id for node in state.nodes}
        before_retry = {node.node_id: node.retry_count for node in state.nodes}
        metrics = self.run_supervisor_agent(
            self.build_replanner_prompt(wave_index, retry_eligible),
            conversation_tag=f"SUPERVISOR_REPLAN_WAVE_{wave_index}",
            step_title=f"SUPERVISOR: Dynamic DAG expansion/recovery after wave {wave_index}",
        )
        self.metrics_waves.append(metrics)

        after_state = DAGState.load(self.workspace_path)
        added_ids = [node.node_id for node in after_state.nodes if node.node_id not in before_ids]
        if added_ids:
            self.expansion_rounds += 1
            after_state.expansion_rounds = self.expansion_rounds
            after_state.save()
            self.validate_extended_task_files(after_state, added_ids)
            print_success(
                f"DAG expanded after wave {wave_index}: {', '.join(added_ids)} "
                f"({self.expansion_rounds}/{self.max_expansion_rounds})."
            )
        retried_ids = [
            node.node_id for node in after_state.nodes if node.retry_count > before_retry.get(node.node_id, 0)
        ]
        if retried_ids:
            print_success(f"DAG node(s) reset for retry after wave {wave_index}: {', '.join(retried_ids)}.")

    def retry_eligible_nodes(self, state: DAGState) -> list[str]:
        """Failed nodes the Supervisor may reset once, honoring the shared retry budget."""
        from src.tools.dag_tools import node_retry_limit

        limit = node_retry_limit()
        eligible: list[str] = []
        for node in state.nodes:
            if node.status != NODE_FAILED or node.retry_count >= limit:
                continue
            node_dir = Path(self.workspace_path) / node.workspace_subdir
            if any(path.is_file() for path in node_dir.rglob(".job_id")):
                continue
            meta = read_node_result_metadata(str(node_dir))
            if meta and meta.get("recovery_attempted") is True:
                continue
            eligible.append(node.node_id)
        return eligible

    def run_ready_node(
        self,
        dag_control: Any,
        node: DAGNode,
        *,
        review_mode: str,
        wave_index: int,
    ) -> None:
        started = self.start_ready_node_batch(dag_control, [node], review_mode=review_mode)
        if not started:
            return
        running_node, message = started[node.node_id]
        try:
            metrics = self.run_coordinator_agent(
                node=running_node,
                node_workspace=Path(self.workspace_path) / running_node.workspace_subdir,
                message=message,
                conversation_tag=f"COORDINATOR_{node.node_id}_RESTART_{running_node.restart_count}_ATTEMPT_{running_node.retry_count}",
            )
            self.metrics_waves.append(metrics)
            self.update_node_from_node_result(running_node)
        except Exception as exc:
            self.handle_node_error(running_node, exc)

    def finalize_converged_node(self, node: DAGNode, *, reason: str) -> bool:
        """Create only the missing Analyzer handoff for durable converged work."""
        state = DAGState.load(self.workspace_path)
        current = state.get_node(node.node_id)
        node_dir = Path(self.workspace_path) / current.workspace_subdir
        metadata = read_node_result_metadata(str(node_dir))

        try:
            if metadata and metadata.get("node_status") == "success":
                self.update_node_from_node_result(current)
                return DAGState.load(self.workspace_path).get_node(current.node_id).status == "done"
            if not runner_result_converged(node_dir):
                return False

            from src.agents.analyzer import create_vasp_analyzer_agent

            print_warning(
                f"Finalizing converged VASP work for {current.node_id} with Analyzer only; "
                "no calculation will be submitted."
            )
            metrics = self.run_agent(
                agent=create_vasp_analyzer_agent(),
                prompt=(
                    f"PROJECT_ROOT={Path(__file__).resolve().parents[2]}\n"
                    f"ACTIVE_NODE_DIR={node_dir}\n"
                    f"TASK_MD_PATH={node_dir / 'task.md'}\n"
                    f"NODE_ID={current.node_id}\n"
                    f"NODE_TITLE={current.title}\n"
                    f"RECOVERY_REASON={reason}\n\n"
                    "A converged runner_result.json already exists. Do not submit, monitor, "
                    "restart, or modify any VASP calculation or input artifact. Read the "
                    "existing task and handoffs, perform the normal Analyzer work, and write "
                    "node_result.json."
                ),
                conversation_tag=f"ANALYZER_RECOVERY_{current.node_id}_RESTART_{current.restart_count}_ATTEMPT_{current.retry_count}",
                step_title=f"ANALYZER RECOVERY: Finalize converged node {current.node_id}",
                workspace=str(node_dir),
                visualizer_name=f"VASP-ANALYZER-{current.node_id}",
            )
            self.metrics_waves.append(metrics)
            self.update_node_from_node_result(current)
            return DAGState.load(self.workspace_path).get_node(current.node_id).status == "done"
        except Exception as recovery_exc:
            print_warning(f"Could not finalize converged node {current.node_id} ({reason}): {recovery_exc}")
            return False

    def recover_completed_node_after_coordinator_error(self, node: DAGNode, error: Exception) -> bool:
        return self.finalize_converged_node(node, reason=f"Coordinator failed: {error}")

    def update_node_from_node_result(self, node: DAGNode) -> None:
        from src.tools.dag_tools import DAGControlAction, DAGControlExecutor

        state = DAGState.load(self.workspace_path)
        current = state.get_node(node.node_id)
        node_dir = Path(self.workspace_path) / current.workspace_subdir
        metadata = read_node_result_metadata(str(node_dir))
        if metadata is None:
            state.mark_failed(current.node_id, f"{current.node_id}: missing or invalid {NODE_RESULT_FILENAME}.")
            return

        metadata = self.review_node_result_if_enabled(current, metadata)
        artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
        node_status = str(metadata.get("node_status") or "failed")
        dag_status = "success" if node_status == "success" else "failed"
        update_obs = DAGControlExecutor(workspace_dir=self.workspace_path)(
            DAGControlAction(
                command="update",
                node_id=current.node_id,
                node_status=dag_status,
                error=str(
                    metadata.get("error") or (f"node_result status: {node_status}" if dag_status == "failed" else "")
                ),
                artifacts={str(k): str(v) for k, v in artifacts.items()},
            )
        )
        update_payload = json.loads(update_obs.message)
        if update_payload.get("errors") and dag_status == "success":
            raise RuntimeError("; ".join(update_payload["errors"]))
        if dag_status == "success":
            print_success(f"DAG node completed: {current.node_id}")

    def build_replanner_prompt(self, wave_index: int, retry_eligible: list[str]) -> str:
        state = DAGState.load(self.workspace_path)
        node_summaries: list[dict[str, Any]] = []
        inspect_results: list[str] = []
        for node in state.nodes:
            summary = {
                "node_id": node.node_id,
                "title": node.title,
                "goal": node.goal,
                "depends_on": node.dependencies,
                "status": node.status,
                "retry_count": node.retry_count,
                "restart_count": node.restart_count,
                "workspace_subdir": node.workspace_subdir,
                "task_md_path": node.task_md_path,
                "artifacts": node.artifacts,
                "last_error": node.last_error,
            }
            node_summaries.append(summary)
            node_result = node.artifacts.get("node_result") or str(
                Path(self.workspace_path) / node.workspace_subdir / NODE_RESULT_FILENAME
            )
            if Path(node_result).is_file():
                inspect_results.append(node_result)

        retry_line = ", ".join(retry_eligible) if retry_eligible else "none"
        prompt = (
            "Dynamic DAG expansion and failure-recovery check:\n"
            f"- Workspace: {self.workspace_path}\n"
            f"- Research objective: {self.user_command}\n"
            f"- Completed scheduler wave: {wave_index}\n"
            "- New-node policy: append-only; do not delete, rename, or restructure existing nodes.\n"
            f"- Retry-eligible failed nodes (within the Python-enforced per-node budget): {retry_line}\n"
            f"- Existing DAG node summaries:\n{json.dumps(node_summaries, indent=2, ensure_ascii=False)}\n\n"
            "Node results to inspect if needed:\n"
            + ("\n".join(f"- {path}" for path in inspect_results) if inspect_results else "- none")
            + "\n\n"
            "Two independent decisions:\n"
            "1. Expansion: if additional calculations are required for the objective, call "
            'DAGControl(command="extend", nodes=[...], reason="...") with only new append-only nodes, '
            "including task_md_content for each.\n"
            "2. Recovery: for a retry-eligible failed node whose failure looks fixable by a clean full "
            "re-run (transient or run-level failure, not an impossible/ill-posed task), call "
            'DAGControl(command="retry", node_ids=[...], reason="..."). This resets the node and its '
            "handoff artifacts so it re-runs once; downstream dependents are preserved. Leave genuinely "
            "unrecoverable nodes failed. The per-node retry budget is enforced in code, so a retry call may "
            "be skipped.\n"
            "If neither expansion nor retry is warranted, call neither command and state that no change is needed. "
            "Do not dispatch Coordinators and do not call DAGControl start/update."
        )
        memory_query_parts = [
            self.user_command,
            "dynamic expansion failure recovery",
            json.dumps(node_summaries, ensure_ascii=False),
        ]
        memory_context = self.memory_file_context()
        if memory_context:
            prompt += (
                f"\n\n{memory_context}\n"
                "Suggested search_memory query terms; use a compact | separated subset: "
                f"{' | '.join(memory_query_parts)}"
            )
        return prompt

    def validate_extended_task_files(self, state: DAGState, node_ids: list[str]) -> None:
        missing: list[str] = []
        for node_id in node_ids:
            node = state.get_node(node_id)
            task_path = Path(self.workspace_path) / node.task_md_path
            node_task_path = Path(self.workspace_path) / node.workspace_subdir / "task.md"
            if not task_path.is_file() and not node_task_path.is_file():
                missing.append(f"{node_id} ({task_path})")
        if missing:
            raise RuntimeError(
                "Supervisor extended the DAG but DAGControl did not produce task.md for new node(s): "
                + ", ".join(missing)
            )

    def build_reporter_prompt(self) -> str:
        state = DAGState.load(self.workspace_path)
        if state.status not in {DAG_DONE, DAG_FAILED}:
            raise RuntimeError(
                f"Reporter can only run after the DAG reaches a terminal state; current status is {state.status}."
            )

        node_evidence: list[dict[str, Any]] = []
        for node in state.nodes:
            node_dir = Path(self.workspace_path) / node.workspace_subdir
            node_result_path = Path(node.artifacts.get("node_result") or node_dir / NODE_RESULT_FILENAME)
            standard_files = {
                "node_result": str(node_result_path),
                "modeling_summary": str(node_dir / MODELING_SUMMARY_FILENAME),
                "modeling_validation": str(node_dir / MODELING_VALIDATION_FILENAME),
                "parameter_summary": str(node_dir / PARAMETER_SUMMARY_FILENAME),
                "parameter_validation": str(node_dir / PARAMETER_VALIDATION_FILENAME),
                "runner_result": str(node_dir / RUNNER_RESULT_FILENAME),
            }

            node_evidence.append(
                {
                    "node_id": node.node_id,
                    "title": node.title,
                    "depends_on": node.dependencies,
                    "status": node.status,
                    "workspace_subdir": node.workspace_subdir,
                    "evidence_files": {key: path for key, path in standard_files.items() if Path(path).is_file()},
                    "artifact_keys": sorted(node.artifacts.keys()),
                }
            )

        failed_nodes = [node.node_id for node in state.nodes if node.status == NODE_FAILED]
        partial_instruction = ""
        if failed_nodes:
            partial_instruction = (
                f"- This is a best-effort report: node(s) {', '.join(failed_nodes)} ended failed after the "
                "automatic retry budget was exhausted. Report only what the available evidence supports, "
                "explicitly state which calculations failed and what is therefore unanswered, and do not "
                "fill the gaps with assumed values.\n"
            )

        final_report_path = Path(self.workspace_path) / FINAL_REPORT_MD_FILENAME
        benchmark_result_path = Path(self.workspace_path) / BENCHMARK_RESULT_FILENAME
        benchmark_result_instruction = ""
        if benchmark_result_path.is_file():
            benchmark_result_instruction = (
                f"- Benchmark result path: {benchmark_result_path}\n"
                "Update this existing benchmark_result.json by filling only its existing keys with evidence-supported values. "
                "Use numbers for scalar metrics and strings only for requested categorical labels such as element symbols. "
                "Do not add keys, units, status fields, evidence fields, arrays, or nested objects. "
                "Leave unsupported metrics as null.\n"
            )
        return (
            "Final DAG report generation request:\n"
            f"- Workspace: {self.workspace_path}\n"
            f"- Original research objective: {self.user_command}\n"
            f"- DAG status: {state.status}\n"
            f"- Final report path: {final_report_path}\n"
            f"{partial_instruction}\n"
            f"{benchmark_result_instruction}\n"
            "Write final_report.md at the final report path. Use this completed DAG evidence index. "
            "Start from each node_result.json, then read only the listed optional small JSON files needed to support "
            "methods, system description, fallback calculation values, or failed/blocked explanations. "
            "Do not inspect raw VASP outputs:\n"
            f"{json.dumps(node_evidence, indent=2, ensure_ascii=False)}\n\n"
            "Answer the original research objective first using supported evidence, then provide "
            "manuscript-style analysis and supporting information. Report key numerical results "
            "with units, connect interpretations to available structural, energetic, electronic, "
            "magnetic, charge, or DOS evidence, and explicitly state evidence limits such as when "
            "oxidation state or mechanism assignment would require Bader charge, projected DOS, "
            "spin-density visualization, XPS, or other additional evidence. Do not invent unsupported "
            "calculation settings or conclusions."
        )

    def run_reporter_agent(self) -> dict[str, Any]:
        from src.agents.reporter import create_vasp_reporter_agent

        restart_generation = sum(node.restart_count for node in DAGState.load(self.workspace_path).nodes)
        metrics = self.run_agent(
            agent=create_vasp_reporter_agent(),
            prompt=self.build_reporter_prompt(),
            conversation_tag=f"REPORTER_FINAL_RESTART_{restart_generation}",
            step_title="REPORTER: Generate final computational report",
            workspace=self.workspace_path,
            visualizer_name="VASP-REPORTER",
        )
        final_report_path = Path(self.workspace_path) / FINAL_REPORT_MD_FILENAME
        if not final_report_path.is_file():
            raise RuntimeError(f"Reporter completed without writing {FINAL_REPORT_MD_FILENAME}.")
        print_success(f"Final computational report written: {final_report_path}")
        return metrics

    def detect_latest_job_id(self) -> str:
        markers = (path for path in Path(self.workspace_path).rglob(".job_id") if path.is_file())
        latest = max(markers, key=lambda path: path.stat().st_mtime, default=None)
        return latest.read_text(encoding="utf-8").strip() if latest else "UNKNOWN"

    def write_metrics_summary(self) -> None:
        total_cost = 0.0
        total_api_calls = 0
        total_tokens: dict[str, int] = {"prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0}

        for wave in self.metrics_waves:
            total_cost += wave.get("cost", 0)
            total_api_calls += wave.get("api_calls", 0)
            for key in total_tokens:
                total_tokens[key] += wave.get("tokens", {}).get(key, 0)

        payload = {
            "cost_unit": "USD",
            "total_cost": round(total_cost, 6),
            "total_api_calls": total_api_calls,
            "total_tokens": total_tokens,
            "waves": self.metrics_waves,
        }

        metrics_filename = f".run_metrics.{self.run_node}.json" if self.run_node else RUN_METRICS_FILENAME
        metrics_path = Path(self.workspace_path) / metrics_filename
        metrics_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        prompt_k = total_tokens["prompt"] / 1000
        completion_k = total_tokens["completion"] / 1000
        print_info(
            f"Run metrics: ${total_cost:.4f} | {total_api_calls} API calls | "
            f"{prompt_k:.1f}K prompt + {completion_k:.1f}K completion tokens"
        )
        print_info(f"Saved to {metrics_path}")
