from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FINAL_REPORT_MD_FILENAME = "final_report.md"
BENCHMARK_RESULT_FILENAME = "benchmark_result.json"
MODELING_SUMMARY_FILENAME = "modeling_summary.json"
MODELING_VALIDATION_FILENAME = "modeling_validation.json"
PARAMETER_SUMMARY_FILENAME = "parameter_summary.json"
PARAMETER_VALIDATION_FILENAME = "parameter_validation.json"
RUNNER_RESULT_FILENAME = "runner_result.json"
NODE_RESULT_FILENAME = "node_result.json"
PIPELINE_CONTEXT_FILENAME = ".pipeline_context.json"

STRICT_COMPLETION_POLICY = {
    "profile": "strict",
    "allow_partial_candidate_success": False,
}

DEMONSTRATION_COMPLETION_POLICY = {
    "profile": "demonstration",
    "allow_partial_candidate_success": True,
    "partial_candidate_node_patterns": ["*_h1_rep", "*_h2_rep", "*_h3_rep"],
    "min_usable_calculations": 1,
    "max_force_eVA": 0.10,
    "strict_force_eVA": 0.05,
    "min_bond_length_A": 0.60,
    "require_scf_convergence": True,
    "require_structure_health": True,
    "report_quality_tier": "demonstration",
}


def read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    """Publish a complete handoff; readers never observe a half-written JSON file."""
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_runner_result(workspace_path: str | Path) -> dict[str, Any] | None:
    return read_json_file(Path(workspace_path) / RUNNER_RESULT_FILENAME)


def read_authoritative_calculations(workspace_path: str | Path) -> dict[str, str]:
    """Return logical calculation subdirs mapped to their selected physical attempts."""
    payload = read_runner_result(workspace_path) or {}
    raw = payload.get("authoritative_calculations")
    if not isinstance(raw, dict):
        return {}
    return {
        logical.strip().rstrip("/"): physical.strip().rstrip("/")
        for logical, physical in raw.items()
        if isinstance(logical, str) and logical.strip() and isinstance(physical, str) and physical.strip()
    }


def normalize_completion_policy(value: str | dict[str, Any] | None) -> dict[str, Any]:
    """Return one supported, closed completion policy payload."""
    if isinstance(value, dict):
        profile = str(value.get("profile") or "strict").strip().lower()
    else:
        profile = str(value or "strict").strip().lower()
    if profile == "demonstration":
        return dict(DEMONSTRATION_COMPLETION_POLICY)
    if profile != "strict":
        raise ValueError("completion profile must be strict or demonstration.")
    return dict(STRICT_COMPLETION_POLICY)


def update_runner_result(workspace_path: str | Path, **sections: Any) -> Path:
    path = Path(workspace_path) / RUNNER_RESULT_FILENAME
    payload = read_json_file(path) or {}
    payload.setdefault("workspace", str(Path(workspace_path).resolve()))
    for key, value in sections.items():
        payload[key] = value
    write_json(path, payload)
    return path


def read_node_result_metadata(workspace_path: str) -> dict[str, Any] | None:
    """Read node_result.json as the node completion contract."""
    payload = read_json_file(Path(workspace_path) / NODE_RESULT_FILENAME)
    if payload is None:
        return None

    status = str(payload.get("node_status") or "failed").strip().lower()
    if status not in {"success", "failed", "blocked"}:
        status = "failed"
    payload["node_status"] = status
    payload.setdefault("error", None)
    if not isinstance(payload.get("artifacts"), dict):
        payload["artifacts"] = {}
    payload["artifacts"] = {
        str(key): str(value) for key, value in payload["artifacts"].items() if str(key).strip() and str(value).strip()
    }
    node_result_path = Path(workspace_path) / NODE_RESULT_FILENAME
    payload["artifacts"].setdefault("node_result", str(node_result_path))
    return payload


def read_analysis_plan(workspace_path: str) -> dict[str, Any] | None:
    return read_json_file(Path(workspace_path) / ".autocat" / "analysis_plan.json")


def read_reaction_profiles(workspace_path: str) -> dict[str, Any] | None:
    return read_json_file(Path(workspace_path) / ".autocat" / "reaction_profiles.json")


# Pipeline context (migrated from src/validator/context.py)


def write_pipeline_context(
    workspace_path: str,
    *,
    user_command: str,
    input_path: str | None = None,
    review_mode: str | None = None,
    original_user_goal: str | None = None,
    node_goal: str | None = None,
    node_id: str | None = None,
    node_title: str | None = None,
    completion_policy: str | dict[str, Any] | None = None,
) -> Path:
    payload = {
        "workspace": workspace_path,
        "user_command": user_command,
        "original_user_goal": original_user_goal,
        "node_goal": node_goal,
        "node_id": node_id,
        "node_title": node_title,
        "input_path": input_path,
        "review_mode": review_mode,
        "completion_policy": normalize_completion_policy(completion_policy),
        "run_id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    target = Path(workspace_path) / PIPELINE_CONTEXT_FILENAME
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return target


def read_pipeline_context(workspace_path: str) -> dict[str, Any] | None:
    return read_json_file(Path(workspace_path) / PIPELINE_CONTEXT_FILENAME)


def read_completion_policy(workspace_path: str | Path) -> dict[str, Any]:
    path = Path(workspace_path).resolve()
    workspace_root = next(
        (candidate for candidate in (path, *path.parents) if (candidate / ".dag_state.json").is_file()),
        None,
    )
    context_path = (workspace_root or path) / PIPELINE_CONTEXT_FILENAME
    context = read_json_file(context_path) or {}
    return normalize_completion_policy(context.get("completion_policy"))


def set_pipeline_completion_profile(
    workspace_path: str | Path,
    profile: str,
    *,
    user_command: str = "",
) -> Path:
    target = Path(workspace_path) / PIPELINE_CONTEXT_FILENAME
    payload = read_json_file(target) or {
        "workspace": str(Path(workspace_path).resolve()),
        "user_command": user_command,
        "run_id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    payload["completion_policy"] = normalize_completion_policy(profile)
    payload["completion_policy_updated_at"] = datetime.now(UTC).isoformat()
    write_json(target, payload)
    return target


def set_pipeline_review_mode(
    workspace_path: str,
    review_mode: str,
    *,
    user_command: str = "",
) -> Path:
    if review_mode not in {"enabled", "disabled"}:
        raise ValueError("review_mode must be enabled or disabled.")

    target = Path(workspace_path) / PIPELINE_CONTEXT_FILENAME
    payload = read_json_file(target) or {
        "workspace": str(Path(workspace_path).resolve()),
        "user_command": user_command,
        "run_id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    payload["review_mode"] = review_mode
    payload["review_updated_at"] = datetime.now(UTC).isoformat()
    write_json(target, payload)
    return target
