"""Project configuration helpers.

`config.yaml` contains local paths and per-agent model credentials; keep it untracked. Relative paths are resolved
from the repository root so the project can be moved or published without
embedding a developer-specific absolute path in source files.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_ENV_VAR = "CATPAW_CONFIG"
DEFAULT_CONFIG_FILENAME = "config.yaml"
EXAMPLE_CONFIG_FILENAME = "config.example.yaml"

PATH_ENV_OVERRIDES = {
    "database_dir": "CATPAW_DATABASE_DIR",
    "model_library_dir": "CATPAW_MODEL_LIBRARY_DIR",
    "workspace_root": "CATPAW_WORKSPACE_ROOT",
}

PATH_DEFAULTS = {
    "database_dir": "database",
    "model_library_dir": "database/model_library",
    "potcar_dir": "database/potcars",
    "memory_dir": "memory",
    "workspace_root": "workspace",
}


@dataclass(frozen=True)
class HpcResourceProfile:
    name: str
    max_atoms: int | None
    total_cpus: int
    kpar: int
    ncore: int
    omp_threads: int


@dataclass(frozen=True)
class HpcResourceConfig:
    cores_per_node: int
    nodes_per_job: int
    profiles: tuple[HpcResourceProfile, ...]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    raw = os.getenv(CONFIG_ENV_VAR, "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else project_root() / path
    return project_root() / DEFAULT_CONFIG_FILENAME


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping.")
    overrides = json.loads(os.getenv("CATPAW_AGENT_CONNECTIONS", "{}"))
    for role, agent in data.get("agents", {}).items():
        agent.update(overrides.get("all", {}))
        agent.update(overrides.get(role, {}))
    return data


def positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer; found {value!r}.") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer; found {value!r}.")
    return parsed


def hpc_resource_config() -> HpcResourceConfig:
    section = load_config().get("hpc")
    if not isinstance(section, dict):
        raise ValueError("config.yaml must define an hpc mapping.")

    cores_per_node = positive_int(section.get("cores_per_node"), "hpc.cores_per_node")
    nodes_per_job = positive_int(section.get("nodes_per_job", 1), "hpc.nodes_per_job")
    if nodes_per_job != 1:
        raise ValueError("The current VASP sbatch template supports only hpc.nodes_per_job=1.")

    raw_profiles = section.get("resource_profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("hpc.resource_profiles must be a non-empty mapping.")

    profiles: list[HpcResourceProfile] = []
    previous_max = 0
    for index, (name, raw) in enumerate(raw_profiles.items()):
        label = f"hpc.resource_profiles.{name}"
        if not isinstance(raw, dict):
            raise ValueError(f"{label} must be a mapping.")

        raw_max = raw.get("max_atoms")
        if raw_max is None:
            if index != len(raw_profiles) - 1:
                raise ValueError(f"{label}.max_atoms=null is allowed only for the final catch-all profile.")
            max_atoms = None
        else:
            max_atoms = positive_int(raw_max, f"{label}.max_atoms")
            if max_atoms <= previous_max:
                raise ValueError("HPC resource profile max_atoms values must be strictly increasing.")
            previous_max = max_atoms

        total_cpus = positive_int(raw.get("total_cpus"), f"{label}.total_cpus")
        kpar = positive_int(raw.get("kpar"), f"{label}.kpar")
        ncore = positive_int(raw.get("ncore"), f"{label}.ncore")
        omp_threads = positive_int(raw.get("omp_threads", 1), f"{label}.omp_threads")
        if omp_threads != 1:
            raise ValueError(f"{label}.omp_threads must be 1 until the sbatch template supports cpus-per-task.")
        if total_cpus * omp_threads > cores_per_node * nodes_per_job:
            raise ValueError(
                f"{label} requests {total_cpus * omp_threads} CPU cores, exceeding "
                f"the configured capacity {cores_per_node * nodes_per_job}."
            )
        if total_cpus % kpar != 0:
            raise ValueError(f"{label}.kpar={kpar} must divide total_cpus={total_cpus}.")
        ranks_per_kgroup = total_cpus // kpar
        if ranks_per_kgroup % ncore != 0:
            raise ValueError(
                f"{label}.ncore={ncore} must divide ranks per k-point group ({total_cpus}/{kpar}={ranks_per_kgroup})."
            )

        profiles.append(
            HpcResourceProfile(
                name=name,
                max_atoms=max_atoms,
                total_cpus=total_cpus,
                kpar=kpar,
                ncore=ncore,
                omp_threads=omp_threads,
            )
        )

    if profiles[-1].max_atoms is not None:
        raise ValueError("The final HPC resource profile must use max_atoms=null as a catch-all.")

    return HpcResourceConfig(
        cores_per_node=cores_per_node,
        nodes_per_job=nodes_per_job,
        profiles=tuple(profiles),
    )


def select_hpc_resource_profile(num_atoms: int) -> HpcResourceProfile:
    atom_count = positive_int(num_atoms, "num_atoms")
    config = hpc_resource_config()
    for profile in config.profiles:
        if profile.max_atoms is None or atom_count <= profile.max_atoms:
            return profile
    raise RuntimeError("No HPC resource profile matched the structure.")


def validate_submission_resources(total_cpus: int, omp_threads: int) -> None:
    cpus = positive_int(total_cpus, "total_cpus")
    threads = positive_int(omp_threads, "omp_threads")
    config = hpc_resource_config()
    if threads != 1:
        raise ValueError("omp_threads must be 1 until the sbatch template supports cpus-per-task.")
    capacity = config.cores_per_node * config.nodes_per_job
    if cpus * threads > capacity:
        raise ValueError(f"Requested {cpus * threads} CPU cores, exceeding configured job capacity {capacity}.")


def resolve_path(raw: str | os.PathLike[str]) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_root() / path
    return path.resolve()


def configured_path(key: str) -> Path:
    env_name = PATH_ENV_OVERRIDES.get(key)
    if env_name:
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return resolve_path(env_value)

    paths = load_config().get("paths", {})
    if isinstance(paths, dict):
        value = paths.get(key)
        if value:
            return resolve_path(str(value))

    return resolve_path(PATH_DEFAULTS[key])


def database_dir() -> Path:
    return configured_path("database_dir")


def model_library_dir() -> Path:
    return configured_path("model_library_dir")


def potcar_dir() -> Path:
    return configured_path("potcar_dir")


def memory_dir() -> Path:
    return configured_path("memory_dir")


def max_node_parallel() -> int:
    return positive_int(load_config().get("workflow", {}).get("max_node_parallel", 2), "workflow.max_node_parallel")


def workspace_root() -> Path:
    return configured_path("workspace_root")


def init_config(*, force: bool = False) -> Path:
    target = config_path()
    source = project_root() / EXAMPLE_CONFIG_FILENAME

    if target.exists() and not force:
        raise FileExistsError(f"Config already exists: {target}")
    if not source.is_file():
        raise FileNotFoundError(f"Example config not found: {source}")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {"init", "path"}:
        raise ValueError("Usage: python -m src.utils.config init [--force] | path")

    command = args[0]
    if command == "path":
        print(config_path())
    else:
        path = init_config(force="--force" in args[1:])
        print(f"Created {path}")


if __name__ == "__main__":
    main()
