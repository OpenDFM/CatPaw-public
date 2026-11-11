from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any

from src.utils.project_env import sanitize_name
from src.utils.results import write_json

DAG_BLUEPRINT_FILENAME = ".dag_blueprint.json"
DAG_STATE_FILENAME = ".dag_state.json"

NODE_PENDING = "pending"
NODE_READY = "ready"
NODE_RUNNING = "running"
NODE_DONE = "done"
NODE_FAILED = "failed"

DAG_ACTIVE = "active"
DAG_DONE = "done"
DAG_FAILED = "failed"

VALID_NODE_STATUSES = {
    NODE_PENDING,
    NODE_READY,
    NODE_RUNNING,
    NODE_DONE,
    NODE_FAILED,
}


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class DAGNode:
    node_id: str
    title: str
    goal: str
    workspace_subdir: str
    task_md_path: str
    depends_on: list[str] = field(default_factory=list)
    status: str = NODE_PENDING
    artifacts: dict[str, str] = field(default_factory=dict)
    last_error: str | None = None
    retry_count: int = 0
    restart_count: int = 0
    inputs: dict[str, str] = field(default_factory=dict)

    @property
    def dependencies(self) -> list[str]:
        """Artifact references imply edges; depends_on adds ordering-only edges."""
        return list(dict.fromkeys([*self.depends_on, *(ref.split("/", 1)[0] for ref in self.inputs.values())]))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DAGNode:
        return cls(
            node_id=str(payload.get("node_id", "")).strip(),
            title=str(payload.get("title", "")).strip(),
            goal=str(payload.get("goal", "")).strip(),
            workspace_subdir=str(payload.get("workspace_subdir", "")).strip(),
            task_md_path=str(payload.get("task_md_path", "")).strip(),
            depends_on=[str(item).strip() for item in payload.get("depends_on", []) if str(item).strip()],
            status=str(payload.get("status", NODE_PENDING)).strip() or NODE_PENDING,
            artifacts={
                str(key): str(value)
                for key, value in (payload.get("artifacts") or {}).items()
                if str(key).strip() and str(value).strip()
            },
            last_error=str(payload.get("last_error")).strip() if payload.get("last_error") else None,
            retry_count=int(payload.get("retry_count") or 0),
            restart_count=int(payload.get("restart_count") or 0),
            inputs=dict(payload.get("inputs", {})),
        )


class DAGState:
    def __init__(
        self,
        *,
        workspace_path: str,
        user_goal: str,
        nodes: list[DAGNode],
        status: str = DAG_ACTIVE,
        scheduler_wave: int = 0,
        expansion_rounds: int = 0,
        created_at: str | None = None,
        updated_at: str | None = None,
    ):
        self.workspace_path = str(Path(workspace_path).resolve())
        self.user_goal = user_goal
        self.nodes = nodes
        self.status = status
        self.scheduler_wave = max(int(scheduler_wave), 0)
        self.expansion_rounds = max(int(expansion_rounds), 0)
        self.created_at = created_at or utcnow()
        self.updated_at = updated_at or self.created_at
        self.index = {node.node_id: node for node in nodes}
        self.validate_state()
        self.refresh_ready_states()

    @property
    def path(self) -> Path:
        return Path(self.workspace_path) / DAG_STATE_FILENAME

    @property
    def blueprint_path(self) -> Path:
        return Path(self.workspace_path) / DAG_BLUEPRINT_FILENAME

    def validate_state(self) -> None:
        if not self.nodes:
            raise ValueError("DAG requires at least one node.")

        seen: set[str] = set()
        for node in self.nodes:
            if not node.node_id:
                raise ValueError("DAG node is missing node_id.")
            if node.node_id in seen:
                raise ValueError(f"Duplicate DAG node: {node.node_id}")
            seen.add(node.node_id)
            for name, reference in node.inputs.items():
                if not name or "/" not in reference or not all(reference.split("/", 1)):
                    raise ValueError(f"Invalid input binding: {node.node_id}.{name}={reference!r}")
            if node.status not in VALID_NODE_STATUSES:
                raise ValueError(f"Invalid DAG node status: {node.node_id} -> {node.status}")

        for node in self.nodes:
            for dep in node.dependencies:
                if dep not in seen:
                    raise ValueError(f"DAG node dependency does not exist: {node.node_id} -> {dep}")
            if node.node_id in node.dependencies:
                raise ValueError(f"DAG node cannot depend on itself: {node.node_id}")

        TopologicalSorter({node.node_id: node.dependencies for node in self.nodes}).prepare()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_path": self.workspace_path,
            "user_goal": self.user_goal,
            "status": self.status,
            "scheduler_wave": self.scheduler_wave,
            "expansion_rounds": self.expansion_rounds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": [node.to_dict() for node in self.nodes],
        }

    def save(self) -> Path:
        self.updated_at = utcnow()
        write_json(self.path, self.to_dict())
        return self.path

    def save_blueprint(self) -> Path:
        payload = {
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
                for node in self.nodes
            ]
        }
        self.blueprint_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return self.blueprint_path

    def add_nodes(self, new_nodes: list[DAGNode]) -> list[DAGNode]:
        existing_ids = {node.node_id for node in self.nodes}
        added = [node for node in new_nodes if node.node_id not in existing_ids]
        if not added:
            return []

        for node in added:
            node.status = NODE_PENDING
            node.artifacts = {}
            node.last_error = None

        candidate_nodes = [*self.nodes, *added]
        candidate = DAGState(
            workspace_path=self.workspace_path,
            user_goal=self.user_goal,
            nodes=candidate_nodes,
            status=DAG_ACTIVE,
            scheduler_wave=self.scheduler_wave,
            expansion_rounds=self.expansion_rounds,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )
        self.nodes = candidate.nodes
        self.status = candidate.status
        self.created_at = candidate.created_at
        self.updated_at = candidate.updated_at
        self.index = candidate.index
        self.save()
        self.save_blueprint()
        return added

    @classmethod
    def load(cls, workspace_path: str) -> DAGState:
        path = Path(workspace_path) / DAG_STATE_FILENAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        nodes = [DAGNode.from_dict(item) for item in payload["nodes"]]
        return cls(
            workspace_path=str(payload.get("workspace_path") or workspace_path),
            user_goal=str(payload.get("user_goal", "")),
            nodes=nodes,
            status=str(payload.get("status", DAG_ACTIVE)),
            scheduler_wave=int(payload.get("scheduler_wave") or 0),
            expansion_rounds=int(payload.get("expansion_rounds") or 0),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )

    @classmethod
    def exists(cls, workspace_path: str) -> bool:
        return (Path(workspace_path) / DAG_STATE_FILENAME).is_file()

    @classmethod
    def create_from_blueprint(cls, workspace_path: str, user_goal: str) -> DAGState:
        blueprint_path = Path(workspace_path) / DAG_BLUEPRINT_FILENAME
        if not blueprint_path.is_file():
            raise FileNotFoundError(f"DAG blueprint not found: {blueprint_path}")

        payload = json.loads(blueprint_path.read_text(encoding="utf-8"))
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValueError("DAG blueprint is missing nodes.")

        nodes: list[DAGNode] = []
        for raw in raw_nodes:
            if not isinstance(raw, dict):
                continue
            node_id = sanitize_name(str(raw.get("node_id", "")).strip())
            if not node_id:
                continue
            depends_on = [sanitize_name(str(item).strip()) for item in raw.get("depends_on", []) if str(item).strip()]
            workspace_subdir = sanitize_name(str(raw.get("workspace_subdir") or node_id).strip())
            task_md_path = f"{workspace_subdir}/task.md"
            node = DAGNode(
                node_id=node_id,
                title=str(raw.get("title") or node_id).strip(),
                goal=str(raw.get("goal") or raw.get("description") or user_goal).strip(),
                workspace_subdir=workspace_subdir,
                task_md_path=task_md_path,
                depends_on=depends_on,
                inputs=dict(raw.get("inputs", {})),
            )
            nodes.append(node)

        state = cls(
            workspace_path=workspace_path,
            user_goal=user_goal,
            nodes=nodes,
        )
        state.save()
        state.save_blueprint()
        return state

    def refresh_ready_states(self) -> None:
        for node in self.nodes:
            if node.status in {NODE_DONE, NODE_FAILED, NODE_RUNNING}:
                continue
            if all(self.index[dep].status == NODE_DONE for dep in node.dependencies):
                node.status = NODE_READY
            else:
                node.status = NODE_PENDING
        # A failed node no longer forces the whole DAG to FAILED on its own:
        # independent ready/running nodes must still get a chance to run, and the
        # scheduler decides on recovery/reporting. The DAG is only FAILED when it
        # is terminal (nothing left to run) yet not every node finished.
        if all(node.status == NODE_DONE for node in self.nodes):
            self.status = DAG_DONE
        elif any(node.status in {NODE_READY, NODE_RUNNING} for node in self.nodes):
            self.status = DAG_ACTIVE
        elif any(node.status == NODE_FAILED for node in self.nodes):
            self.status = DAG_FAILED
        else:
            self.status = DAG_ACTIVE

    def get_ready_nodes(self) -> list[DAGNode]:
        self.refresh_ready_states()
        return [node for node in self.nodes if node.status == NODE_READY]

    def get_node(self, node_id: str) -> DAGNode:
        return self.index[node_id]

    def mark_running(self, node_id: str) -> None:
        node = self.get_node(node_id)
        if node.status not in {NODE_READY, NODE_RUNNING}:
            raise ValueError(f"Node cannot transition to running: {node_id} ({node.status})")
        node.status = NODE_RUNNING
        node.last_error = None
        self.save()

    def mark_done(self, node_id: str, artifacts: dict[str, str] | None = None) -> None:
        node = self.get_node(node_id)
        node.status = NODE_DONE
        node.last_error = None
        if artifacts:
            node.artifacts.update({k: v for k, v in artifacts.items() if k and v})
        self.refresh_ready_states()
        self.save()

    def mark_failed(self, node_id: str, error: str) -> None:
        node = self.get_node(node_id)
        node.status = NODE_FAILED
        node.last_error = error
        self.refresh_ready_states()
        self.save()

    def reset_for_retry(self, node_id: str) -> DAGNode:
        """Reset a failed node to pending for one full re-run and bump retry_count."""
        node = self.get_node(node_id)
        if node.status != NODE_FAILED:
            raise ValueError(f"Only failed nodes can be reset for retry: {node_id} ({node.status})")
        node.status = NODE_PENDING
        node.last_error = None
        node.artifacts = {}
        node.retry_count += 1
        self.refresh_ready_states()
        self.save()
        return node

    def transitive_descendants(self, node_ids: list[str]) -> list[DAGNode]:
        """Return requested nodes and every downstream dependent in DAG order."""
        requested = {str(node_id).strip() for node_id in node_ids if str(node_id).strip()}
        unknown = sorted(requested.difference(self.index))
        if unknown:
            raise ValueError(f"Unknown DAG restart node(s): {', '.join(unknown)}")

        affected = set(requested)
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if node.node_id in affected:
                    continue
                if any(dependency in affected for dependency in node.dependencies):
                    affected.add(node.node_id)
                    changed = True
        return [node for node in self.nodes if node.node_id in affected]

    def reset_for_restart(self, node_ids: list[str]) -> list[DAGNode]:
        """Invalidate an explicit scientific subgraph restart without spending retry budget."""
        affected = self.transitive_descendants(node_ids)
        for node in affected:
            node.status = NODE_PENDING
            node.artifacts = {}
            node.last_error = None
            node.restart_count += 1
        self.status = DAG_ACTIVE
        self.refresh_ready_states()
        self.save()
        return affected

    def requeue_for_resume(self, node_id: str) -> DAGNode:
        """Resume interrupted orchestration without spending a calculation retry."""
        node = self.get_node(node_id)
        if node.status not in {NODE_RUNNING, NODE_FAILED}:
            raise ValueError(f"Only interrupted running/failed nodes can be resumed: {node_id} ({node.status})")
        node.status = NODE_PENDING
        self.refresh_ready_states()
        self.save()
        return node

    def unresolved_nodes(self) -> list[DAGNode]:
        return [node for node in self.nodes if node.status not in {NODE_DONE, NODE_FAILED}]
