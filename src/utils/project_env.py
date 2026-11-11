"""Resolve project paths, initialize workspaces, and diagnose the runtime environment."""

import importlib.util
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from src.utils.config import (
    database_dir as configured_database_dir,
)
from src.utils.config import (
    load_config,
)
from src.utils.config import (
    potcar_dir as configured_potcar_dir,
)
from src.utils.config import (
    project_root as configured_project_root,
)
from src.utils.config import (
    workspace_root as configured_workspace_root,
)


def sanitize_name(name: str) -> str:
    """Replace shell-unsafe characters in workspace / node directory names.

    Why: paths flow through `find` / `bash -c` strings inside FileEditor and
    TerminalTool without shell quoting; characters like `()` break those calls.
    Keep only alphanumerics, dot, underscore, hyphen; collapse runs.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return cleaned or "unnamed"


# Project / workspace paths


def project_root() -> Path:
    return configured_project_root()


def workspace_root() -> Path:
    return configured_workspace_root()


def resolve_workspace_name(workspace_name: str) -> str:
    return str((workspace_root() / workspace_name).resolve())


def resolve_workspace_reference(workspace_ref: str) -> str:
    candidate = Path(workspace_ref).expanduser()
    if candidate.is_dir():
        return str(candidate.resolve())
    if candidate.is_absolute() or workspace_ref.startswith(".") or "/" in workspace_ref:
        raise FileNotFoundError(f"Workspace does not exist: {candidate}")

    workspace_path = Path(resolve_workspace_name(workspace_ref))
    if not workspace_path.is_dir():
        raise FileNotFoundError(f"Workspace does not exist: {workspace_path}")
    return str(workspace_path)


def setup_workspace(
    input_file: str | None = None,
    workspace_name: str | None = None,
    *,
    workspace_base: Path | None = None,
) -> tuple[str, str | None]:
    workspace_base = workspace_base or workspace_root()
    workspace_base.mkdir(parents=True, exist_ok=True)

    input_path = Path(input_file).expanduser().resolve() if input_file else None
    base_name = workspace_name or (f"{input_path.stem}_job" if input_path else "scratch_job")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    workspace_path = workspace_base / f"{sanitize_name(base_name)}_{timestamp}"
    workspace_path.mkdir(parents=True, exist_ok=False)
    if input_path:
        shutil.copy2(input_path, workspace_path / input_path.name)

    os.environ["VASP_WORKSPACE"] = str(workspace_path)
    return str(workspace_path), str(input_path) if input_path else None


# Environment diagnostics (doctor)


def run_doctor() -> None:
    """Validate required configuration, directories, and dependencies."""
    from src.utils.ui import (
        print_banner,
        print_error,
        print_info,
        print_success,
        print_warning,
    )

    print_banner("CATPAW DOCTOR")
    settings = load_config()
    missing_required = False
    if settings.get("materials_project", {}).get("api_key"):
        print_success("materials_project.api_key is configured for Materials Project structure retrieval")
    else:
        print_error("materials_project.api_key is missing; configure it in config.yaml")
        missing_required = True

    for role, config in settings["agents"].items():
        if all(config.get(key) for key in ("model", "url", "api_key")):
            print_info(f"{role}: {config['model']} ({config.get('api', settings.get('llm', {}).get('api', 'chat_completions'))})")
        else:
            print_error(f"agents.{role} requires model, url and api_key.")
            missing_required = True

    ws_root = workspace_root()
    db_root = configured_database_dir()
    pot_root = configured_potcar_dir()
    if ws_root.is_dir():
        print_success(f"Workspace root is available: {ws_root}")
    else:
        print_error(f"Workspace root is missing: {ws_root}")
        missing_required = True

    if db_root.is_dir():
        print_success(f"Database directory is available: {db_root}")
    else:
        print_error(f"Database directory is missing: {db_root}")
        missing_required = True

    if pot_root.is_dir():
        print_success(f"POTCAR directory is available: {pot_root}")
    else:
        print_error(f"POTCAR directory is missing: {pot_root}")
        missing_required = True

    if importlib.util.find_spec("pydantic") is not None:
        print_success("pydantic is installed and available for Agent tool parameter models.")
    else:
        print_error("pydantic is not installed; current Agent tool parameter models still depend on it.")
        missing_required = True

    if missing_required:
        print_warning("Doctor checks failed. Complete the required items above before starting the pipeline.")
        raise RuntimeError("Doctor checks failed")

    print_success("Doctor checks passed. The pipeline is ready to run.")
