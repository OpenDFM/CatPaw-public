from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.graph.state import DAG_ACTIVE, NODE_DONE, NODE_PENDING, DAGNode, DAGState
from src.utils.results import (
    NODE_RESULT_FILENAME,
    read_completion_policy,
    read_node_result_metadata,
)

FORK_MANIFEST_FILENAME = ".fork_manifest.json"
PIPELINE_CONTEXT_FILENAME = ".pipeline_context.json"


def path_within(root: Path, relative_path: str) -> Path:
    path = (root / relative_path).resolve()
    if path == root or root not in path.parents:
        raise ValueError(f"Workspace-relative path escapes its workspace: {relative_path}")
    return path


def rebase_string(value: str, source: Path, target: Path) -> str:
    source_text = str(source)
    return value.replace(source_text, str(target))


def rebase_json_value(value: Any, source: Path, target: Path) -> Any:
    if isinstance(value, dict):
        return {key: rebase_json_value(item, source, target) for key, item in value.items()}
    if isinstance(value, list):
        return [rebase_json_value(item, source, target) for item in value]
    if isinstance(value, str):
        return rebase_string(value, source, target)
    return value


@dataclass(frozen=True)
class WorkspaceForkResult:
    source_workspace: str
    target_workspace: str
    restart_nodes: tuple[str, ...]
    preserved_nodes: tuple[str, ...]
    reset_nodes: tuple[str, ...]
    ready_nodes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_workspace": self.source_workspace,
            "target_workspace": self.target_workspace,
            "restart_nodes": list(self.restart_nodes),
            "preserved_nodes": list(self.preserved_nodes),
            "reset_nodes": list(self.reset_nodes),
            "ready_nodes": list(self.ready_nodes),
        }


def fork_workspace_from_completed_upstream(
    source_workspace: str | Path,
    target_workspace: str | Path,
    *,
    restart_nodes: list[str] | tuple[str, ...],
    review_mode: str = "disabled",
) -> WorkspaceForkResult:
    """Fork a DAG while copying only completed upstream evidence and reset-node tasks."""
    if review_mode not in {"enabled", "disabled"}:
        raise ValueError("review_mode must be enabled or disabled.")

    source = Path(source_workspace).expanduser().resolve()
    target = Path(target_workspace).expanduser().resolve()
    requested = tuple(dict.fromkeys(str(node_id).strip() for node_id in restart_nodes if str(node_id).strip()))
    if not requested:
        raise ValueError("Workspace fork requires at least one restart node.")
    if source == target:
        raise ValueError("Source and target workspaces must be different.")
    if source in target.parents:
        raise ValueError("Target workspace cannot be inside the source workspace.")
    if target.exists():
        raise FileExistsError(f"Target workspace already exists: {target}")

    state = DAGState.load(str(source))
    if Path(state.workspace_path).resolve() != source:
        raise ValueError(f"Source DAG state points to a different workspace: {state.workspace_path}")

    reset = state.transitive_descendants(list(requested))
    reset_ids = {node.node_id for node in reset}
    preserved = [node for node in state.nodes if node.node_id not in reset_ids]
    for node in preserved:
        if node.status != NODE_DONE:
            raise ValueError(f"Fork would preserve incomplete node {node.node_id}: {node.status}")
        node_dir = path_within(source, node.workspace_subdir)
        metadata = read_node_result_metadata(str(node_dir))
        if not metadata or metadata.get("node_status") != "success":
            raise ValueError(f"Fork requires a successful {NODE_RESULT_FILENAME}: {node.node_id}")

    temp = target.parent / f".{target.name}.forking-{uuid.uuid4().hex[:8]}"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp.mkdir()
    created_at = datetime.now(UTC).isoformat()
    try:
        for node in preserved:
            source_dir = path_within(source, node.workspace_subdir)
            destination = path_within(temp, node.workspace_subdir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_dir, destination)
            for path in sorted(destination.rglob("*.json")):
                payload = json.loads(path.read_text(encoding="utf-8"))
                rebased = rebase_json_value(payload, source, target)
                path.write_text(
                    json.dumps(rebased, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

        for node in reset:
            source_task = path_within(source, node.task_md_path)
            if not source_task.is_file():
                raise FileNotFoundError(f"Reset node is missing task.md: {node.node_id} ({source_task})")
            destination = path_within(temp, node.task_md_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_task, destination)

        fork_nodes: list[DAGNode] = []
        for node in state.nodes:
            payload = node.to_dict()
            if node.node_id in reset_ids:
                payload.update(status=NODE_PENDING, artifacts={}, last_error=None)
            else:
                payload["artifacts"] = {
                    key: rebase_string(value, source, target) for key, value in node.artifacts.items()
                }
            fork_nodes.append(DAGNode.from_dict(payload))

        fork_state = DAGState(
            workspace_path=str(target),
            user_goal=state.user_goal,
            nodes=fork_nodes,
            status=DAG_ACTIVE,
            scheduler_wave=0,
            expansion_rounds=state.expansion_rounds,
            created_at=created_at,
            updated_at=created_at,
        )
        state_path = temp / ".dag_state.json"
        state_path.write_text(
            json.dumps(fork_state.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        blueprint_payload = {
            "nodes": [
                {
                    "node_id": node.node_id,
                    "title": node.title,
                    "goal": node.goal,
                    "depends_on": node.depends_on,
                    "inputs": node.inputs,
                    "workspace_subdir": node.workspace_subdir,
                    "task_md_path": node.task_md_path,
                }
                for node in fork_state.nodes
            ]
        }
        (temp / ".dag_blueprint.json").write_text(
            json.dumps(blueprint_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        pipeline_context = {
            "workspace": str(target),
            "user_command": state.user_goal,
            "original_user_goal": None,
            "node_goal": None,
            "node_id": None,
            "node_title": None,
            "input_path": None,
            "review_mode": review_mode,
            "completion_policy": read_completion_policy(source),
            "run_id": uuid.uuid4().hex,
            "timestamp": created_at,
            "forked_from": str(source),
        }
        (temp / PIPELINE_CONTEXT_FILENAME).write_text(
            json.dumps(pipeline_context, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        ready_nodes = tuple(node.node_id for node in fork_state.get_ready_nodes())
        result = WorkspaceForkResult(
            source_workspace=str(source),
            target_workspace=str(target),
            restart_nodes=requested,
            preserved_nodes=tuple(node.node_id for node in preserved),
            reset_nodes=tuple(node.node_id for node in reset),
            ready_nodes=ready_nodes,
        )
        manifest = {
            "created_at": created_at,
            "review_mode": review_mode,
            "source_state_created_at": state.created_at,
            "source_state_updated_at": state.updated_at,
            **result.to_dict(),
        }
        (temp / FORK_MANIFEST_FILENAME).write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, target)
        return result
    except Exception:
        if temp.exists():
            shutil.rmtree(temp)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fork an CatPaw DAG from completed upstream nodes without copying reset jobs."
    )
    parser.add_argument("source_workspace")
    parser.add_argument("target_workspace")
    parser.add_argument("--restart-nodes", nargs="+", required=True)
    parser.add_argument(
        "--review-mode",
        choices=("enabled", "disabled"),
        default="disabled",
    )
    args = parser.parse_args()
    result = fork_workspace_from_completed_upstream(
        args.source_workspace,
        args.target_workspace,
        restart_nodes=args.restart_nodes,
        review_mode=args.review_mode,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
