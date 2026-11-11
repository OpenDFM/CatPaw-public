from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.graph.state import NODE_DONE, NODE_FAILED, NODE_RUNNING, DAGNode, DAGState
from src.monitor.service import inspect_workspace_jobs
from src.utils.results import read_node_result_metadata, read_runner_result

RECOVERY_LOG_FILENAME = ".recovery_log.jsonl"

SKIP = "skip"
ACCEPT_RESULT = "accept_result"
ANALYZE = "analyze"
EXTRACT_ANALYZE = "extract_analyze"
MONITOR = "monitor"
CONTINUE = "continue"
KEEP = "keep"
FAIL = "fail"


@dataclass(frozen=True)
class ResumeDecision:
    node_id: str
    action: str
    reason: str
    evidence: dict[str, Any]


def runner_result_converged(node_dir: Path) -> bool:
    payload = read_runner_result(node_dir)
    status = payload.get("job_status") if isinstance(payload, dict) else None
    return bool(
        isinstance(status, dict)
        and status.get("all_terminal") is True
        and status.get("all_converged") is True
        and status.get("has_failure") is False
    )


def decision_for_node(
    node: DAGNode,
    workspace: Path,
    *,
    persist_monitor_state: bool,
    job_inspector: Callable[..., tuple[Any, dict[str, Any]]],
) -> ResumeDecision:
    node_dir = workspace / node.workspace_subdir
    metadata = read_node_result_metadata(str(node_dir))
    if metadata is not None:
        node_status = str(metadata.get("node_status") or "failed")
        if node_status == "success":
            action = SKIP if node.status == NODE_DONE else ACCEPT_RESULT
            return ResumeDecision(
                node.node_id,
                action,
                "valid successful node_result.json",
                {"node_status": node_status},
            )
        return ResumeDecision(
            node.node_id,
            FAIL,
            f"node_result.json reports {node_status}",
            {"node_status": node_status, "error": metadata.get("error")},
        )

    runner = read_runner_result(node_dir) or {}
    if runner.get("recovery_attempts"):
        return ResumeDecision(
            node.node_id,
            CONTINUE,
            "continue the reserved node repair without resetting its budget or resubmitting jobs",
            {"recovery_attempts": runner["recovery_attempts"]},
        )

    if runner_result_converged(node_dir):
        return ResumeDecision(
            node.node_id,
            ANALYZE,
            "VASP results are converged but node_result.json is missing",
            {"runner_result": "all_terminal/all_converged"},
        )

    job_id_paths = sorted(node_dir.rglob(".job_id"))
    if job_id_paths:
        _, snapshot = job_inspector(str(node_dir), persist=persist_monitor_state)
        evidence = {
            "job_ids": [path.read_text(encoding="utf-8").strip() for path in job_id_paths if path.is_file()],
            "results": snapshot.get("results", {}),
        }
        if snapshot.get("all_converged") is True:
            return ResumeDecision(
                node.node_id,
                EXTRACT_ANALYZE,
                "existing Slurm jobs converged; rebuild runner handoff and analyze",
                evidence,
            )
        if snapshot.get("all_terminal") is True and snapshot.get("has_failure") is True:
            return ResumeDecision(
                node.node_id,
                CONTINUE,
                "terminal jobs lack a final node result; continue Coordinator diagnosis without resubmission",
                evidence,
            )
        return ResumeDecision(
            node.node_id,
            MONITOR,
            "existing Slurm job is still active or awaiting scheduler reconciliation",
            evidence,
        )

    runner_result = read_runner_result(node_dir)
    job_status = runner_result.get("job_status") if isinstance(runner_result, dict) else None
    if isinstance(job_status, dict) and (
        job_status.get("all_terminal") is True or job_status.get("has_failure") is True
    ):
        return ResumeDecision(
            node.node_id,
            FAIL,
            "terminal runner_result.json is not converged and no submitted job can be resumed",
            {"job_status": job_status},
        )

    if node.status == NODE_DONE:
        return ResumeDecision(
            node.node_id,
            FAIL,
            "DAG marks node done but its successful node_result.json is missing",
            {},
        )
    if node.status in {NODE_RUNNING, NODE_FAILED}:
        return ResumeDecision(
            node.node_id,
            CONTINUE,
            "orchestration was interrupted before a durable calculation result",
            {"previous_status": node.status, "last_error": node.last_error},
        )
    return ResumeDecision(node.node_id, KEEP, "node has not started", {"status": node.status})


def build_resume_plan(
    state: DAGState,
    *,
    persist_monitor_state: bool = False,
    job_inspector: Callable[..., tuple[Any, dict[str, Any]]] = inspect_workspace_jobs,
) -> list[ResumeDecision]:
    workspace = Path(state.workspace_path)
    return [
        decision_for_node(
            node,
            workspace,
            persist_monitor_state=persist_monitor_state,
            job_inspector=job_inspector,
        )
        for node in state.nodes
    ]


def append_recovery_log(
    workspace_path: str,
    decision: ResumeDecision,
    *,
    outcome: str,
) -> None:
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        **asdict(decision),
        "outcome": outcome,
    }
    path = Path(workspace_path) / RECOVERY_LOG_FILENAME
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
