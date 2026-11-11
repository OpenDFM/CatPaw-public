"""Prepare one evidence-backed, node-local repair without overwriting failed jobs."""

import fcntl
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import ToolExecutor, register_tool
from pydantic import BaseModel, Field

from src.graph.state import DAGState
from src.monitor.job_monitor import JobMonitor, JobState
from src.tools.dag_tools import node_retry_limit
from src.utils.results import read_json_file, read_runner_result, update_runner_result


class RepairDiagnosis(BaseModel):
    worker: Literal["modeling_worker_agent", "param_worker_agent"]
    evidence: list[str] = Field(min_length=1, description="Observed facts with file paths or tool evidence.")
    hypotheses: list[str] = Field(default_factory=list, description="Uncertain explanations, not established causes.")
    repair_goal: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)


class PrepareNodeRepairAction(Action):
    node_dir: str
    calculations: list[str] = Field(min_length=1, description="Failed logical calculation subdirectories.")
    diagnosis: RepairDiagnosis


class PrepareNodeRepairObservation(Observation):
    prepared: bool = False
    plan: dict[str, Any] = Field(default_factory=dict)
    message: str = ""

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=json.dumps(self.model_dump(), ensure_ascii=False))]


def confirm_jobs_terminal(node_dir: Path, sources: list[str]) -> None:
    monitor = JobMonitor(str(node_dir))
    runner = read_runner_result(node_dir) or {}
    jobs = runner.get("job_status", {}).get("jobs", [])
    for subdir in sources:
        job_id = (node_dir / subdir / ".job_id").read_text().strip()
        if not job_id.isdigit():
            raise ValueError(f"Missing numeric job ID in {subdir}.")
        state, _, _, _ = monitor.check_slurm_status(job_id)
        stalled_cancel = any(job.get("job_id") == job_id and job.get("recovery_cancel_reason") for job in jobs)
        if state not in {JobState.COMPLETED, JobState.FAILED} and not (state == JobState.CANCELLED and stalled_cancel):
            raise ValueError(f"Job {job_id} is active, unknown or externally cancelled; repair is blocked.")


def prepare_node_repair(action: PrepareNodeRepairAction, workspace_dir: str) -> dict[str, Any]:
    node_dir = (Path(workspace_dir) / action.node_dir).resolve()
    if not (node_dir / "task.md").is_file():
        raise ValueError("Repair requires an existing calculation node.")
    with (node_dir / ".repair.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = read_runner_result(node_dir) or {}
        if (node_dir / "node_result.json").exists():
            raise ValueError("Node is already finalized; use explicit reconciliation/restart.")
        attempts = runner.get("recovery_attempts", [])
        sources = list(dict.fromkeys(action.calculations))
        if attempts:
            plan = attempts[-1]
            if plan.get("sources") != sources or plan.get("diagnosis") != action.diagnosis.model_dump():
                raise ValueError("The node repair budget is already reserved; continue the recorded attempt.")
            if plan["status"] == "prepared":
                return plan
        else:
            retry_count = 0
            for parent in node_dir.parents:
                if (parent / ".dag_state.json").is_file():
                    state = DAGState.load(str(parent))
                    node = next((n for n in state.nodes if (parent / n.workspace_subdir).resolve() == node_dir), None)
                    if node is None:
                        raise ValueError("Repair directory is not a node of the enclosing DAG.")
                    retry_count = node.retry_count
                    break
            if retry_count >= min(node_retry_limit(), 1):
                raise ValueError("The shared node retry budget is exhausted or disabled.")
            results = runner.get("job_status", {}).get("results", {})
            mapping = {}
            for subdir in sources:
                source = (node_dir / subdir).resolve()
                if not source.is_relative_to(node_dir) or source.relative_to(node_dir).as_posix() != subdir:
                    raise ValueError("Use canonical calculation paths inside the active node.")
                if results.get(subdir) not in {"FAILED", "NOT_CONVERGED", "STALLED", "CONVERGED"}:
                    raise ValueError(f"No terminal calculation evidence for {subdir}.")
                target = (
                    source.with_name(source.name + "_recovery1")
                    if subdir != "."
                    else node_dir / "calculation_recovery1"
                )
                if target.exists():
                    raise ValueError(f"Repair target already exists without a reservation: {target}")
                mapping[subdir] = target.relative_to(node_dir).as_posix()
            confirm_jobs_terminal(node_dir, sources)
            plan = {
                "attempt": 1,
                "status": "preparing",
                "sources": sources,
                "calculations": mapping,
                "diagnosis": action.diagnosis.model_dump(),
            }
            # Reserve the budget before creating anything; a repeated call finishes this same preparation.
            update_runner_result(node_dir, recovery_attempts=[plan])
        archive = node_dir / ".repairs" / "attempt_1"
        archive.mkdir(parents=True, exist_ok=True)
        for name in (
            "modeling_summary.json",
            "modeling_validation.json",
            "parameter_summary.json",
            "parameter_validation.json",
            "runner_result.json",
        ):
            source = node_dir / name
            if source.is_file() and not (archive / name).exists():
                shutil.copy2(source, archive / name)
        for source_dir, target_dir in plan["calculations"].items():
            source, target = node_dir / source_dir, node_dir / target_dir
            target.mkdir(parents=True, exist_ok=True)
            for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR"):
                if (source / name).is_file():
                    shutil.copy2(source / name, target / name)
        plan["status"] = "prepared"
        update_runner_result(
            node_dir,
            recovery_attempts=[plan],
            authoritative_calculations={**runner.get("authoritative_calculations", {}), **plan["calculations"]},
        )
        return plan


def validate_repair_submission(working_dir: Path) -> None:
    """Require fresh validation and terminal source jobs for a reserved repair."""
    for node_dir in (working_dir, *working_dir.parents):
        runner = read_runner_result(node_dir)
        if not runner:
            continue
        for plan in runner.get("recovery_attempts", []):
            mapping = plan.get("calculations", {})
            if working_dir.relative_to(node_dir).as_posix() not in mapping.values():
                continue
            if plan["status"] != "prepared":
                raise ValueError("Repair preparation is incomplete.")
            summary = read_json_file(node_dir / "parameter_summary.json") or {}
            if working_dir.relative_to(node_dir).as_posix() not in {
                calculation["subdir"] for calculation in summary.get("calculations", [])
            }:
                raise ValueError("Repair target is not in the validated parameter summary.")
            for name in ("modeling_validation.json", "parameter_validation.json"):
                validation = read_json_file(node_dir / name) or {}
                if validation.get("status") != "PASS" or validation.get("repair_attempt") != plan["attempt"]:
                    raise ValueError(f"Repair requires fresh PASS with repair_attempt={plan['attempt']}: {name}")
            confirm_jobs_terminal(node_dir, plan["sources"])
            return


class PrepareNodeRepairExecutor(ToolExecutor[PrepareNodeRepairAction, PrepareNodeRepairObservation]):
    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir

    def __call__(self, action: PrepareNodeRepairAction, conversation: Any = None) -> PrepareNodeRepairObservation:
        try:
            plan = prepare_node_repair(action, self.workspace_dir)
            return PrepareNodeRepairObservation(prepared=True, plan=plan)
        except (OSError, ValueError) as exc:
            return PrepareNodeRepairObservation(message=f"Repair not prepared: {exc}")


class PrepareNodeRepairTool(ToolDefinition[PrepareNodeRepairAction, PrepareNodeRepairObservation]):
    name: ClassVar[str] = "PrepareNodeRepair"

    @classmethod
    def create(cls, conv_state: Any) -> Sequence[ToolDefinition]:
        return [
            cls(
                description="Reserve the node's single repair, preserve old inputs/handoffs and prepare new calculation paths.",
                action_type=PrepareNodeRepairAction,
                observation_type=PrepareNodeRepairObservation,
                executor=PrepareNodeRepairExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


register_tool("PrepareNodeRepair", PrepareNodeRepairTool)
