from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import (
    DeclaredResources,
    Tool,
    ToolAnnotations,
    ToolExecutor,
    register_tool,
)
from openhands.tools.file_editor.definition import (
    FileEditorAction,
    FileEditorObservation,
)
from openhands.tools.file_editor.editor import FileEditor
from pydantic import Field

from src.graph.state import (
    NODE_DONE,
    NODE_FAILED,
    DAGNode,
    DAGState,
)
from src.utils.results import (
    MODELING_SUMMARY_FILENAME,
    MODELING_VALIDATION_FILENAME,
    NODE_RESULT_FILENAME,
    PARAMETER_SUMMARY_FILENAME,
    PARAMETER_VALIDATION_FILENAME,
    RUNNER_RESULT_FILENAME,
    read_completion_policy,
    read_node_result_metadata,
    write_pipeline_context,
)
from src.utils.task_types import CANONICAL_TASK_TYPES

RECOMMENDED_TASK_TYPE_FORMAT = "task_type: <bulk_opt|slab_opt|mol_opt|scf|dos_calc>"


def node_retry_limit() -> int:
    """Per-node automatic retry cap shared across intra-node and DAG-level recovery."""
    raw = os.getenv("CATPAW_NODE_RETRY_LIMIT", "1").strip()
    try:
        return max(int(raw), 0)
    except ValueError:
        return 1


class DAGControlValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        repair_hints: list[str],
        expected_task_types: list[str] | None = None,
        accepted_task_type_formats: list[str] | None = None,
        retry_instruction: str = "Correct the nodes payload and retry DAGControl with the same command once.",
    ):
        super().__init__(message)
        self.error_code = error_code
        self.expected_task_types = expected_task_types or []
        self.accepted_task_type_formats = accepted_task_type_formats or []
        self.repair_hints = repair_hints
        self.retry_instruction = retry_instruction


class DAGControlAction(Action):
    command: str = Field(description=("DAG operation: initialize, extend, status, ready, start, update, or retry."))
    nodes: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Nodes for initialize or extend. Each node needs node_id, title, goal, depends_on, "
            "workspace_subdir, and task_md_content. Optional inputs maps roles to node_id/artifact_key references; these imply dependencies. task_md_path is normalized to "
            "<workspace_subdir>/task.md when present."
        ),
    )
    node_ids: list[str] = Field(default_factory=list, description="Target node ids for start.")
    node_id: str = Field(default="", description="Target node id for update.")
    node_status: str = Field(default="", description="Node final status for update: success or failed.")
    error: str = Field(default="", description="Failure summary for update.")
    artifacts: dict[str, Any] = Field(default_factory=dict, description="Artifact paths from node_result.json.")
    user_goal: str = Field(default="", description="Original user goal. Used when creating a new DAG.")
    overwrite: bool = Field(default=False, description="Allow initialize to replace an existing DAG.")
    review_mode: str = Field(default="disabled", description="Node pipeline review mode: enabled or disabled.")
    reason: str = Field(default="", description="Reason for extending the DAG.")


class DAGControlObservation(Observation):
    message: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


def json_block(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def normalize_node(raw: dict[str, Any], user_goal: str) -> DAGNode:
    node_id = str(raw.get("node_id", "")).strip()
    if not node_id:
        raise ValueError("DAG node is missing node_id.")
    depends_on = [str(item).strip() for item in raw.get("depends_on", []) if str(item).strip()]
    workspace_subdir = str(raw.get("workspace_subdir") or node_id).strip()
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
    return node


def validate_relative_workspace_path(value: str, field_name: str) -> None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a workspace-relative path: {value}")


def task_md_content(raw: dict[str, Any], node_id: str) -> str:
    content = str(raw.get("task_md_content") or "").strip()
    if not content:
        raise ValueError(f"DAG node {node_id} is missing task_md_content.")
    return content


def extract_task_type(content: str) -> str | None:
    inline_pattern = re.compile(
        r"^\s*(?:[-*]\s*)?(?:\*\*)?[`\"]?task[\s_-]*type[`\"]?(?:\*\*)?\s*[:：]\s*[`\"]?([A-Za-z0-9_+-]+)",
        re.IGNORECASE,
    )
    heading_pattern = re.compile(r"^\s*#+\s*task[\s_-]*type\s*$", re.IGNORECASE)
    lines = content.splitlines()
    for index, line in enumerate(lines):
        match = inline_pattern.match(line)
        if match:
            return match.group(1).strip().lower()
        if heading_pattern.match(line):
            for next_line in lines[index + 1 :]:
                task_type = next_line.strip().strip("-*`\"' ")
                if task_type:
                    return task_type.lower()
    return None


def validate_task_md_content(raw: dict[str, Any], node_id: str) -> str:
    content = task_md_content(raw, node_id)
    task_type = extract_task_type(content)
    if not task_type:
        expected = sorted(CANONICAL_TASK_TYPES)
        raise DAGControlValidationError(
            f"DAG node {node_id} task_md_content must include a canonical task_type line, "
            f"for example 'task_type: bulk_opt'. Expected one of: {', '.join(expected)}.",
            error_code="missing_task_type",
            expected_task_types=expected,
            accepted_task_type_formats=[RECOMMENDED_TASK_TYPE_FORMAT],
            repair_hints=[
                "Add one parseable task_type line near the top of task_md_content.",
                "Use only the canonical task types listed in expected_task_types.",
            ],
        )
    if task_type not in CANONICAL_TASK_TYPES:
        expected = sorted(CANONICAL_TASK_TYPES)
        repair_hints = (
            [
                "Do not create standalone bader, charge, static, or analysis DAG nodes.",
                "For Bader, charge, or spin-localization objectives, create the relevant optimization node followed by a dependent scf node, e.g. bulk_opt -> scf.",
                "Put charge/Bader intent in the scf node objective and task.md; Analyzer handles charge extraction inside the scf node.",
            ]
            if task_type in {"bader", "charge", "static", "analysis"}
            else [
                "Replace the unsupported task_type with one canonical value from expected_task_types.",
                "Phase-only work such as structure retrieval/preparation, Bader, or charge extraction belongs inside a canonical calculation node.",
            ]
        )
        raise DAGControlValidationError(
            f"DAG node {node_id} uses unsupported task_type={task_type!r}; expected one of: {', '.join(expected)}.",
            error_code="unsupported_task_type",
            expected_task_types=expected,
            accepted_task_type_formats=[RECOMMENDED_TASK_TYPE_FORMAT],
            repair_hints=repair_hints,
        )
    return content


class DAGControlExecutor(ToolExecutor[DAGControlAction, DAGControlObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = str(Path(workspace_dir).resolve())

    @property
    def workspace(self) -> Path:
        return Path(self.workspace_dir)

    def __call__(self, action: DAGControlAction, conversation=None) -> DAGControlObservation:
        command = action.command.strip().lower()
        try:
            if command == "initialize":
                payload = self.initialize(action)
            elif command == "extend":
                payload = self.extend(action)
            elif command == "status":
                payload = self.status()
            elif command == "ready":
                payload = self.ready()
            elif command == "start":
                payload = self.start(action)
            elif command == "update":
                payload = self.update(action)
            elif command == "retry":
                payload = self.retry(action)
            else:
                payload = self.error_payload(f"Unknown DAGControl command: {action.command}")
        except DAGControlValidationError as exc:
            payload = self.error_payload(
                str(exc),
                error_code=exc.error_code,
                expected_task_types=exc.expected_task_types,
                accepted_task_type_formats=exc.accepted_task_type_formats,
                repair_hints=exc.repair_hints,
                retry_instruction=exc.retry_instruction,
            )
        except Exception as exc:
            payload = self.error_payload(str(exc))
        return DAGControlObservation(message=json_block(payload))

    def error_payload(self, message: str, **details: Any) -> dict[str, Any]:
        payload = {
            "status": "error",
            "message": message,
            "dag_status": self.load_status_or_none(),
            "ready_nodes": [],
            "node_summaries": [],
            "errors": [message],
        }
        for key, value in details.items():
            if value:
                payload[key] = value
        return payload

    def load_status_or_none(self) -> str | None:
        if not DAGState.exists(self.workspace_dir):
            return None
        try:
            return DAGState.load(self.workspace_dir).status
        except Exception:
            return None

    def load_state(self) -> DAGState:
        if not DAGState.exists(self.workspace_dir):
            raise FileNotFoundError("DAG is not initialized. Call DAGControl(command='initialize') first.")
        return DAGState.load(self.workspace_dir)

    def initialize(self, action: DAGControlAction) -> dict[str, Any]:
        if DAGState.exists(self.workspace_dir) and not action.overwrite:
            return self.error_payload("DAG already exists. Set overwrite=true to replace it.")
        if not action.nodes:
            raise ValueError("initialize requires nodes.")

        user_goal = action.user_goal.strip()
        nodes: list[DAGNode] = []
        task_contents: dict[str, str] = {}
        for raw in action.nodes:
            if not isinstance(raw, dict):
                continue
            node = normalize_node(raw, user_goal)
            validate_relative_workspace_path(node.workspace_subdir, "workspace_subdir")
            validate_relative_workspace_path(node.task_md_path, "task_md_path")
            task_contents[node.node_id] = validate_task_md_content(raw, node.node_id)
            nodes.append(node)
        if not nodes:
            raise ValueError("initialize received no valid nodes.")

        state = DAGState(workspace_path=self.workspace_dir, user_goal=user_goal, nodes=nodes)
        state.save_blueprint()
        state.save()
        for node in nodes:
            self.node_workspace(node).mkdir(parents=True, exist_ok=True)
            self.write_task_file(node, task_contents[node.node_id])

        return self.payload(
            state,
            status="success",
            message=f"DAG initialized successfully with {len(nodes)} nodes.",
        )

    def extend(self, action: DAGControlAction) -> dict[str, Any]:
        state = self.load_state()
        if not action.nodes:
            return self.payload(
                state,
                status="success",
                message="No extension nodes provided.",
                ready_nodes=[self.delegate_payload(state, node) for node in state.get_ready_nodes()],
            )

        existing_ids = {node.node_id for node in state.nodes}
        skipped_duplicate_nodes: list[str] = []
        new_nodes: list[DAGNode] = []
        task_contents: dict[str, str] = {}
        seen_in_request: set[str] = set()
        for raw in action.nodes:
            if not isinstance(raw, dict):
                continue
            node = normalize_node(raw, state.user_goal)
            validate_relative_workspace_path(node.workspace_subdir, "workspace_subdir")
            validate_relative_workspace_path(node.task_md_path, "task_md_path")
            if node.node_id in existing_ids or node.node_id in seen_in_request:
                skipped_duplicate_nodes.append(node.node_id)
                continue
            seen_in_request.add(node.node_id)
            task_contents[node.node_id] = validate_task_md_content(raw, node.node_id)
            new_nodes.append(node)

        added_nodes = state.add_nodes(new_nodes)
        for node in added_nodes:
            self.node_workspace(node).mkdir(parents=True, exist_ok=True)
            self.write_task_file(node, task_contents[node.node_id])

        state = DAGState.load(self.workspace_dir)
        payload = self.payload(
            state,
            status="success",
            message=f"Extended DAG with {len(added_nodes)} new node(s).",
            ready_nodes=[self.delegate_payload(state, node) for node in state.get_ready_nodes()],
        )
        payload["added_nodes"] = [self.node_summary(node) for node in added_nodes]
        payload["skipped_duplicate_nodes"] = skipped_duplicate_nodes
        payload["reason"] = action.reason.strip()
        return payload

    def status(self) -> dict[str, Any]:
        if not DAGState.exists(self.workspace_dir):
            return {
                "status": "not_initialized",
                "message": "DAG is not initialized.",
                "dag_status": None,
                "ready_nodes": [],
                "node_summaries": [],
                "errors": [],
            }
        state = self.load_state()
        return self.payload(state, status="success", message=f"DAG status: {state.status}")

    def ready(self) -> dict[str, Any]:
        state = self.load_state()
        ready_nodes = state.get_ready_nodes()
        state.save()
        return self.payload(
            state,
            status="success",
            message=f"Current number of ready nodes: {len(ready_nodes)}",
            ready_nodes=[self.delegate_payload(state, node) for node in ready_nodes],
        )

    def start(self, action: DAGControlAction) -> dict[str, Any]:
        state = self.load_state()
        nodes = self.target_nodes(state, action.node_ids)
        delegate_payloads: list[dict[str, Any]] = []
        for node in nodes:
            self.seed_root_inputs(node)
            self.link_upstream_files(state, node)
            task_path = self.ensure_task_file(node)
            state.mark_running(node.node_id)
            write_pipeline_context(
                str(self.node_workspace(node)),
                user_command=node.goal,
                input_path=None,
                review_mode=action.review_mode if action.review_mode in {"enabled", "disabled"} else "disabled",
                original_user_goal=state.user_goal,
                node_goal=node.goal,
                node_id=node.node_id,
                node_title=node.title,
                completion_policy=read_completion_policy(self.workspace_dir),
            )
            delegate_payload = self.delegate_payload(state, node)
            delegate_payload["message"] += (
                f"\nREVIEW_MODE={action.review_mode if action.review_mode in {'enabled', 'disabled'} else 'disabled'}"
            )
            delegate_payload["task_md_path"] = str(task_path)
            delegate_payloads.append(delegate_payload)
        state = DAGState.load(self.workspace_dir)
        return self.payload(
            state,
            status="success",
            message=f"Started {len(nodes)} ready node(s).",
            ready_nodes=delegate_payloads,
        )

    def update(self, action: DAGControlAction) -> dict[str, Any]:
        state = self.load_state()
        node_id = action.node_id.strip()
        if not node_id:
            raise ValueError("update requires node_id.")
        node = state.get_node(node_id)
        node_status = action.node_status.strip().lower()
        if node_status not in {"success", "failed"}:
            raise ValueError("update node_status must be success or failed.")

        artifacts = {
            str(key): str(value) for key, value in action.artifacts.items() if str(key).strip() and str(value).strip()
        }
        if "node_result" not in artifacts:
            result_path = self.node_workspace(node) / NODE_RESULT_FILENAME
            if result_path.is_file():
                artifacts["node_result"] = str(result_path)

        errors: list[str] = []
        if node_status == "success":
            state.mark_done(
                node.node_id, artifacts={**self.collect_node_artifacts(self.node_workspace(node)), **artifacts}
            )
        else:
            error = action.error.strip() or f"{node.node_id}: Coordinator execution failed."
            state.mark_failed(node.node_id, error)
            errors.append(error)
        state = DAGState.load(self.workspace_dir)
        return self.payload(
            state,
            status="failed" if errors else "success",
            message="Node status updated." if not errors else "Node marked as failed.",
            errors=errors,
        )

    def retry(self, action: DAGControlAction) -> dict[str, Any]:
        state = self.load_state()
        ids = [str(node_id).strip() for node_id in action.node_ids if str(node_id).strip()]
        if not ids:
            raise ValueError("retry requires node_ids.")

        limit = node_retry_limit()
        retried: list[str] = []
        skipped: list[dict[str, str]] = []
        for node_id in ids:
            node = state.get_node(node_id)
            reason = self.retry_block_reason(node, limit)
            if reason:
                skipped.append({"node_id": node_id, "reason": reason})
                continue
            self.reset_node_files(node)
            state.reset_for_retry(node_id)
            retried.append(node_id)

        state = DAGState.load(self.workspace_dir)
        payload = self.payload(
            state,
            status="success",
            message=f"Reset {len(retried)} node(s) for retry.",
            ready_nodes=[self.delegate_payload(state, n) for n in state.get_ready_nodes()],
        )
        payload["retried_nodes"] = retried
        payload["skipped_nodes"] = skipped
        payload["reason"] = action.reason.strip()
        return payload

    def retry_block_reason(self, node: DAGNode, limit: int) -> str | None:
        if node.status != NODE_FAILED:
            return f"node is {node.status}, not failed; only failed nodes can be retried."
        if node.retry_count >= limit:
            return f"per-node retry budget exhausted (retry_count={node.retry_count}, limit={limit})."
        node_dir = self.node_workspace(node)
        if any(path.is_file() for path in node_dir.rglob(".job_id")):
            return "submitted calculation evidence exists; use resume/reconciliation instead of full retry."
        meta = read_node_result_metadata(str(node_dir))
        if meta and meta.get("recovery_attempted") is True:
            return "node already performed an in-node convergence recovery; shared retry budget is spent."
        return None

    def reset_node_files(self, node: DAGNode) -> None:
        """Archive pre-submission handoffs before an explicit full retry."""
        node_dir = self.node_workspace(node)
        archive_dir = node_dir / ".attempts" / f"retry_{node.retry_count + 1}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for filename in (
            NODE_RESULT_FILENAME,
            RUNNER_RESULT_FILENAME,
            MODELING_SUMMARY_FILENAME,
            MODELING_VALIDATION_FILENAME,
            PARAMETER_SUMMARY_FILENAME,
            PARAMETER_VALIDATION_FILENAME,
        ):
            path = node_dir / filename
            if path.is_file():
                shutil.move(str(path), str(archive_dir / filename))

    def payload(
        self,
        state: DAGState,
        *,
        status: str,
        message: str,
        ready_nodes: list[dict[str, Any]] | None = None,
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "message": message,
            "dag_status": state.status,
            "ready_nodes": ready_nodes if ready_nodes is not None else [],
            "node_summaries": [self.node_summary(node) for node in state.nodes],
            "errors": errors or [],
        }

    def target_nodes(self, state: DAGState, node_ids: list[str], *, require_ready: bool = True) -> list[DAGNode]:
        ids = [str(node_id).strip() for node_id in node_ids if str(node_id).strip()]
        if not ids:
            raise ValueError("node_ids are required.")
        nodes = [state.get_node(node_id) for node_id in ids]
        if require_ready:
            state.refresh_ready_states()
            invalid = [node.node_id for node in nodes if node.status != "ready"]
            if invalid:
                raise ValueError(f"Nodes are not in ready state and cannot be started: {', '.join(invalid)}")
        return nodes

    def node_workspace(self, node: DAGNode) -> Path:
        return self.workspace / node.workspace_subdir

    def write_task_file(self, node: DAGNode, content: str) -> Path:
        node_dir = self.node_workspace(node)
        node_dir.mkdir(parents=True, exist_ok=True)
        expected_task = node_dir / "task.md"
        text = content.rstrip() + "\n"
        expected_task.write_text(text, encoding="utf-8")
        return expected_task

    def ensure_task_file(self, node: DAGNode) -> Path:
        node_dir = self.node_workspace(node)
        node_dir.mkdir(parents=True, exist_ok=True)
        expected_task = node_dir / "task.md"
        if expected_task.is_file():
            return expected_task
        raise FileNotFoundError(f"Node {node.node_id} is missing task.md at {expected_task}.")

    def seed_root_inputs(self, node: DAGNode) -> None:
        if node.dependencies:
            return
        node_dir = self.node_workspace(node)
        node_dir.mkdir(parents=True, exist_ok=True)
        for pattern in ("*.cif", "*.xsd", "POSCAR"):
            for source in self.workspace.glob(pattern):
                if not source.is_file():
                    continue
                target = node_dir / source.name
                if not target.exists():
                    shutil.copy2(source, target)

    def link_upstream_files(self, state: DAGState, node: DAGNode) -> None:
        """Resolve named inputs once; never select a structure by directory order."""
        node_dir = self.node_workspace(node)
        resolved = {}
        for role, reference in node.inputs.items():
            source_id, key = reference.split("/", 1)
            source = state.get_node(source_id)
            if source.status != NODE_DONE:
                raise ValueError(f"Input source is not complete: {reference}")
            path = Path(source.artifacts[key])
            if not path.is_file():
                raise FileNotFoundError(f"Missing input {reference}: {path}")
            resolved[role] = str(path.resolve())
        (node_dir / "inputs.json").write_text(json_block(resolved), encoding="utf-8")
        if node.inputs:
            return
        # Unbound dependencies expose every artifact by its unique key.
        for dep_id in node.dependencies:
            dep = state.get_node(dep_id)
            for key, path in dep.artifacts.items():
                src = Path(path)
                if key.endswith(("_structure", "_wavecar", "_chgcar", "_outcar")) and src.is_file():
                    target = node_dir / f"{key}_from_{dep_id}"
                    if not target.exists():
                        shutil.copy2(src, target)

    def collect_node_artifacts(self, node_dir: Path) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        candidates = {
            "task_md": node_dir / "task.md",
            "modeling_summary": node_dir / MODELING_SUMMARY_FILENAME,
            "modeling_validation": node_dir / MODELING_VALIDATION_FILENAME,
            "parameter_summary": node_dir / PARAMETER_SUMMARY_FILENAME,
            "parameter_validation": node_dir / PARAMETER_VALIDATION_FILENAME,
            "runner_result": node_dir / RUNNER_RESULT_FILENAME,
            "node_result": node_dir / NODE_RESULT_FILENAME,
        }
        for key, path in candidates.items():
            if path.is_file():
                artifacts[key] = str(path)
        return artifacts

    def node_summary(self, node: DAGNode) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "title": node.title,
            "goal": node.goal,
            "depends_on": node.dependencies,
            "inputs": node.inputs,
            "status": node.status,
            "workspace": str(self.node_workspace(node)),
            "task_md_path": str(self.workspace / node.task_md_path),
            "artifacts": node.artifacts,
            "last_error": node.last_error,
        }

    def delegate_payload(self, state: DAGState, node: DAGNode) -> dict[str, Any]:
        node_dir = self.node_workspace(node)
        payload: dict[str, Any] = {
            "agent_id": f"coordinator_{node.node_id}",
            "agent_type": "coordinator_agent",
            "node_id": node.node_id,
            "title": node.title,
            "workspace": str(node_dir),
            "task_md_path": str(node_dir / "task.md"),
            "message": (
                f"PROJECT_ROOT={Path(__file__).resolve().parents[2]}\n"
                f"ACTIVE_NODE_DIR={node_dir}\n"
                f"TASK_MD_PATH={node_dir / 'task.md'}\n"
                f"INPUTS_PATH={node_dir / 'inputs.json'}\n"
                f"ORIGINAL_USER_GOAL={state.user_goal}\n"
                f"NODE_GOAL={node.goal}\n"
                "Use ORIGINAL_USER_GOAL only as read-only intent context. "
                "Do not expand scope beyond this DAG node's task.md and NODE_GOAL."
            ),
        }
        param_context = self.upstream_param_context(state, node)
        if param_context:
            payload["upstream_param_context"] = param_context
            payload["message"] += f"\n{param_context}"
        return payload

    def upstream_param_context(self, state: DAGState, node: DAGNode) -> str:
        if not node.dependencies:
            return ""
        ref_lines: list[str] = []
        for dep_id in node.dependencies:
            dep = state.get_node(dep_id)
            if dep.status != NODE_DONE or not dep.artifacts:
                continue
            dep_dir = self.node_workspace(dep)
            cp = dep.artifacts.get("parameter_summary")
            nr = dep.artifacts.get("node_result")
            if cp or nr:
                ref_lines.append(f"  Upstream node {dep.node_id} ({dep_dir}):")
                if cp:
                    ref_lines.append(f"    parameter_summary: {cp}")
                if nr:
                    ref_lines.append(f"    node_result: {nr}")
        if not ref_lines:
            return ""
        return (
            "Upstream reference files (for downstream ParamWorker alignment of critical accuracy parameters):\n"
            + "\n".join(ref_lines)
        )


class DAGControlTool(ToolDefinition[DAGControlAction, DAGControlObservation]):
    name: ClassVar[str] = "DAGControl"
    DESCRIPTION: ClassVar[str] = "Create, extend, inspect, start, and update the CatPaw DAG state."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=DAGControlAction,
                observation_type=DAGControlObservation,
                executor=DAGControlExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


register_tool("DAGControl", DAGControlTool)


class DAGSkillFileEditorExecutor(ToolExecutor[FileEditorAction, FileEditorObservation]):
    def __init__(self, skill_root: str | None = None):
        self.skill_root = Path(
            skill_root or Path(__file__).resolve().parents[1] / "skills" / "dag-orchestration"
        ).resolve()
        self.editor = FileEditor(workspace_root=str(self.skill_root))

    def __call__(self, action: FileEditorAction, conversation=None) -> FileEditorObservation:
        if action.command != "view":
            return self.blocked(
                action,
                "DAGSkillFileEditor is read-only for Supervisor. Use command='view' only.",
            )
        try:
            path = self.resolve_allowed_path(action.path)
            return self.editor(
                command="view",
                path=str(path),
                view_range=action.view_range,
            )
        except Exception as exc:
            return self.blocked(action, str(exc))

    def blocked(self, action: FileEditorAction, message: str) -> FileEditorObservation:
        return FileEditorObservation.from_text(
            text=(
                f"{message}\n"
                f"Allowed dag-orchestration paths are under: {self.skill_root}\n"
                "Allowed files/directories: SKILL.md, references/, and assets/."
            ),
            command=action.command,
            is_error=True,
            path=action.path,
            prev_exist=True,
        )

    def resolve_allowed_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if not path.is_absolute():
            raise ValueError("path must be an absolute path under the dag-orchestration skill root.")
        resolved = path.resolve()
        try:
            rel = resolved.relative_to(self.skill_root)
        except ValueError as exc:
            raise ValueError("path is outside the dag-orchestration skill root.") from exc
        if ".." in rel.parts:
            raise ValueError("path must not contain '..'.")

        is_allowed_dir = resolved.is_dir() and (
            rel.parts == ()
            or rel.parts == ("references",)
            or rel.parts == ("assets",)
            or (len(rel.parts) >= 1 and rel.parts[0] in {"references", "assets"})
        )
        is_allowed_file = rel.parts == ("SKILL.md",) or (
            len(rel.parts) >= 2 and rel.parts[0] in {"references", "assets"}
        )
        if not (is_allowed_dir or is_allowed_file):
            raise ValueError("path must be SKILL.md or under references/ or assets/.")
        if not resolved.exists():
            raise FileNotFoundError(f"Skill resource not found: {rel.as_posix()}")
        return resolved


class DAGSkillFileEditorTool(ToolDefinition[FileEditorAction, FileEditorObservation]):
    name: ClassVar[str] = "DAGSkillFileEditor"
    DESCRIPTION: ClassVar[str] = (
        "Read-only FileEditor-style viewer for dag-orchestration skill resources. "
        "Use command='view' with absolute paths under the dag-orchestration skill root only. "
        "Allowed resources are SKILL.md, references/, and assets/. Editing commands are blocked."
    )

    def declared_resources(self, action: Action) -> DeclaredResources:
        assert isinstance(action, FileEditorAction)
        normalized_path = Path(action.path).resolve()
        return DeclaredResources(keys=(f"file:{normalized_path}",), declared=True)

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        skill_root = (Path(__file__).resolve().parents[1] / "skills" / "dag-orchestration").resolve()
        description = (
            f"{cls.DESCRIPTION}\n"
            f"dag-orchestration skill root: {skill_root}\n"
            f"Common files to view:\n"
            f"- {skill_root / 'SKILL.md'}\n"
            f"- {skill_root / 'references' / 'dag-few-shot-examples.md'}\n"
            f"- {skill_root / 'assets' / 'manifest.yaml'}"
        )
        return [
            cls(
                description=description,
                action_type=FileEditorAction,
                observation_type=FileEditorObservation,
                annotations=ToolAnnotations(
                    title="dag_skill_file_editor",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=DAGSkillFileEditorExecutor(skill_root=str(skill_root)),
            )
        ]


register_tool("DAGSkillFileEditor", DAGSkillFileEditorTool)


def get_dag_tools_config() -> list[Tool]:
    return [Tool(name="DAGControl"), Tool(name="DAGSkillFileEditor")]
