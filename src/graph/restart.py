from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.graph.resume import RECOVERY_LOG_FILENAME
from src.graph.state import DAGState
from src.monitor.service import inspect_workspace_jobs
from src.utils.results import FINAL_REPORT_MD_FILENAME

RESTART_ARCHIVE_DIRNAME = ".restart_attempts"
RUN_METRICS_FILENAME = ".run_metrics.json"


@dataclass(frozen=True)
class RestartPlan:
    workspace_path: str
    requested_nodes: tuple[str, ...]
    affected_nodes: tuple[str, ...]
    preserved_nodes: tuple[str, ...]
    prior_statuses: dict[str, str]
    job_evidence: dict[str, dict[str, Any]]
    archive_root: str
    blocked_reasons: tuple[str, ...]
    reason: str = "scientific_invalidation"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def node_path(workspace: Path, relative_path: str) -> Path:
    path = (workspace / relative_path).resolve()
    if path == workspace or workspace not in path.parents:
        raise ValueError(f"Restart path escapes workspace: {relative_path}")
    return path


def build_restart_plan(
    state: DAGState,
    requested_nodes: list[str] | tuple[str, ...],
    *,
    job_inspector: Callable[..., tuple[Any, dict[str, Any]]] = inspect_workspace_jobs,
) -> RestartPlan:
    requested = tuple(dict.fromkeys(str(node_id).strip() for node_id in requested_nodes if str(node_id).strip()))
    if not requested:
        raise ValueError("Restart requires at least one node ID.")

    affected = state.transitive_descendants(list(requested))
    affected_ids = tuple(node.node_id for node in affected)
    affected_set = set(affected_ids)
    workspace = Path(state.workspace_path).resolve()
    evidence: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    archive_timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")

    for node in affected:
        node_dir = node_path(workspace, node.workspace_subdir)
        task_path = node_path(workspace, node.task_md_path)
        if node_dir not in task_path.parents:
            raise ValueError(f"Restart task.md is outside its node directory: {node.node_id}")
        if not task_path.is_file():
            raise ValueError(f"Restart node is missing task.md: {node.node_id} ({task_path})")

        markers = sorted(path for path in node_dir.rglob(".job_id") if path.is_file())
        if not markers:
            continue
        _, snapshot = job_inspector(str(node_dir), persist=False)
        job_ids: list[str] = []
        for path in markers:
            job_id = path.read_text(encoding="utf-8").strip()
            if job_id:
                job_ids.append(job_id)
        node_evidence = {
            "job_ids": job_ids,
            "job_count": snapshot.get("job_count", 0),
            "all_terminal": snapshot.get("all_terminal"),
            "results": snapshot.get("results", {}),
        }
        evidence[node.node_id] = node_evidence
        if not snapshot.get("job_count") or snapshot.get("all_terminal") is not True:
            blockers.append(
                f"{node.node_id}: submitted job state is active or unknown; "
                f"job_ids={job_ids or ['unknown']}, results={snapshot.get('results', {})}"
            )

    return RestartPlan(
        workspace_path=str(workspace),
        requested_nodes=requested,
        affected_nodes=affected_ids,
        preserved_nodes=tuple(node.node_id for node in state.nodes if node.node_id not in affected_set),
        prior_statuses={node.node_id: node.status for node in affected},
        job_evidence=evidence,
        archive_root=str(workspace / RESTART_ARCHIVE_DIRNAME / f"{archive_timestamp}_{uuid.uuid4().hex[:8]}"),
        blocked_reasons=tuple(blockers),
    )


def apply_restart_plan(
    plan: RestartPlan,
    *,
    job_inspector: Callable[..., tuple[Any, dict[str, Any]]] = inspect_workspace_jobs,
) -> Path:
    if plan.blocked_reasons:
        raise RuntimeError("Restart is blocked: " + "; ".join(plan.blocked_reasons))

    workspace = Path(plan.workspace_path).resolve()
    state = DAGState.load(str(workspace))
    fresh_plan = build_restart_plan(
        state,
        list(plan.requested_nodes),
        job_inspector=job_inspector,
    )
    if fresh_plan.affected_nodes != plan.affected_nodes:
        raise RuntimeError("DAG changed after restart planning; run reconcile-only again.")
    if fresh_plan.blocked_reasons:
        raise RuntimeError("Restart is blocked: " + "; ".join(fresh_plan.blocked_reasons))

    archive_root = Path(plan.archive_root)
    if archive_root.exists():
        raise FileExistsError(f"Restart archive already exists: {archive_root}")

    original_state = state.path.read_bytes()
    moved_nodes: list[tuple[Path, Path]] = []
    moved_workspace_files: list[tuple[Path, Path]] = []
    archive_root.mkdir(parents=True)

    try:
        for node_id in plan.affected_nodes:
            node = state.get_node(node_id)
            node_dir = node_path(workspace, node.workspace_subdir)
            task_path = node_path(workspace, node.task_md_path)
            task_relative = task_path.relative_to(node_dir)
            destination = archive_root / node.workspace_subdir
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(node_dir, destination)
            moved_nodes.append((node_dir, destination))
            node_dir.mkdir(parents=True)
            restored_task = node_dir / task_relative
            restored_task.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination / task_relative, restored_task)

        workspace_archive = archive_root / "_workspace"
        for filename in (FINAL_REPORT_MD_FILENAME, RUN_METRICS_FILENAME):
            source_path = workspace / filename
            if not source_path.is_file():
                continue
            destination = workspace_archive / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source_path, destination)
            moved_workspace_files.append((source_path, destination))

        manifest = archive_root / "restart_manifest.json"
        manifest.write_text(
            json.dumps(plan.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        state.reset_for_restart(list(plan.requested_nodes))
        recovery = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": "restart_subgraph",
            "reason": plan.reason,
            "requested_nodes": list(plan.requested_nodes),
            "affected_nodes": list(plan.affected_nodes),
            "prior_statuses": plan.prior_statuses,
            "job_evidence": plan.job_evidence,
            "archive_root": str(archive_root),
            "outcome": "restarted",
        }
        log_path = Path(plan.workspace_path) / RECOVERY_LOG_FILENAME
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(recovery, ensure_ascii=False) + "\n")
        return archive_root
    except Exception:
        temp_state = state.path.with_name(f".{state.path.name}.restart-rollback.tmp")
        temp_state.write_bytes(original_state)
        os.replace(temp_state, state.path)

        for source_path, destination in reversed(moved_workspace_files):
            if destination.exists():
                source_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source_path)
        for source_path, destination in reversed(moved_nodes):
            if source_path.exists():
                shutil.rmtree(source_path)
            if destination.exists():
                source_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source_path)
        if archive_root.exists():
            shutil.rmtree(archive_root)
        archive_parent = archive_root.parent
        if archive_parent.is_dir() and not any(archive_parent.iterdir()):
            archive_parent.rmdir()
        raise
