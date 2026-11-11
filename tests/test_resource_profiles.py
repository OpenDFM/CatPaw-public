from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.utils.config as config_module


@pytest.fixture
def configured_hpc(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        """hpc:
  cores_per_node: 72
  nodes_per_job: 1
  resource_profiles:
    small:
      max_atoms: 20
      total_cpus: 24
      kpar: 1
      ncore: 4
    large:
      max_atoms: null
      total_cpus: 48
      kpar: 1
      ncore: 8
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CATPAW_CONFIG", str(path))
    config_module.load_config.cache_clear()
    yield path
    config_module.load_config.cache_clear()


def write_poscar(path: Path, atom_count: int, element: str = "H") -> None:
    coordinates = "\n".join("0 0 0" for _ in range(atom_count))
    path.write_text(
        f"resource test\n1.0\n20 0 0\n0 20 0\n0 0 20\n{element}\n{atom_count}\nDirect\n{coordinates}\n",
        encoding="utf-8",
    )


def test_param_validator_rejects_resource_values_not_matching_profile(
    tmp_path: Path,
    configured_hpc: Path,
) -> None:
    from src.tools.param_validation_tools import validate_parameter_policy

    calc_dir = tmp_path / "bulk_opt"
    calc_dir.mkdir()
    write_poscar(calc_dir / "POSCAR", 4, element="Ni")
    (calc_dir / "INCAR").write_text(
        "ISIF = 3\nNSW = 10\nIBRION = 1\nKPAR = 1\nNCORE = 4\n",
        encoding="utf-8",
    )
    (calc_dir / "KPOINTS").write_text(
        "mesh\n0\nGamma\n6 6 6\n0 0 0\n",
        encoding="utf-8",
    )
    (calc_dir / "POTCAR").write_text("placeholder\n", encoding="utf-8")
    (tmp_path / "parameter_summary.json").write_text(
        json.dumps(
            {
                "calculations": [
                    {
                        "subdir": "bulk_opt",
                        "resource_profile": "large",
                        "submit_params": {
                            "job_name": "bulk",
                            "total_cpus": 48,
                            "num_dcus": 1,
                            "omp_threads": 1,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = validate_parameter_policy(
        str(tmp_path),
        [{"subdir": "bulk_opt", "task_type": "bulk_opt"}],
    )

    assert result["passed"] is False
    assert any("resource_profile must be 'small'" in error for error in result["errors"])
    assert any("requires total_cpus=24" in error for error in result["errors"])

    summary = json.loads((tmp_path / "parameter_summary.json").read_text(encoding="utf-8"))
    summary["calculations"][0]["resource_profile"] = "small"
    summary["calculations"][0]["submit_params"]["total_cpus"] = 24
    (tmp_path / "parameter_summary.json").write_text(
        json.dumps(summary),
        encoding="utf-8",
    )
    repaired = validate_parameter_policy(
        str(tmp_path),
        [{"subdir": "bulk_opt", "task_type": "bulk_opt"}],
    )

    assert repaired["passed"] is True


def test_submit_guard_rejects_request_above_configured_capacity(
    tmp_path: Path,
    configured_hpc: Path,
) -> None:
    from src.tools.runner_tools import SubmitVaspJobAction, SubmitVaspJobExecutor

    calc_dir = tmp_path / "calc"
    calc_dir.mkdir()
    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR"):
        (calc_dir / name).write_text("placeholder\n", encoding="utf-8")

    observation = SubmitVaspJobExecutor(str(tmp_path))(
        SubmitVaspJobAction(
            working_dir=str(calc_dir),
            total_cpus=73,
            num_dcus=1,
            omp_threads=1,
        )
    )

    assert observation.submitted is False
    assert "exceeding configured job capacity 72" in observation.message
    assert not (calc_dir / "vasp.sbatch").exists()
    assert not (calc_dir / ".job_id").exists()


def test_yaml_owns_potcar_memory_and_parallelism(configured_hpc, tmp_path, monkeypatch):
    from src.graph.runner import SupervisorRunner
    from src.memory.store import MemoryStore
    from src.tools.param_input_plugins.create_POTCAR import generate_smart_potcar

    with configured_hpc.open("a") as stream:
        stream.write("\npaths:\n  potcar_dir: potentials\n  memory_dir: memories\nworkflow:\n  max_node_parallel: 3\n")
    monkeypatch.setattr(config_module, "project_root", lambda: tmp_path)
    for name in ("POTCAR_DIR", "AUTOCAT_MEMORY_DIR", "CATPAW_MEMORY_DIR"):
        monkeypatch.setenv(name, str(tmp_path / "stale-env-path"))
    for name in ("AUTOCAT_MAX_NODE_PARALLEL", "CATPAW_MAX_NODE_PARALLEL"):
        monkeypatch.setenv(name, "99")

    assert config_module.potcar_dir() == tmp_path / "potentials"
    assert MemoryStore().root == tmp_path / "memories"
    assert SupervisorRunner(str(tmp_path)).max_node_parallel == 3

    potential = tmp_path / "potentials" / "H" / "POTCAR"
    potential.parent.mkdir(parents=True)
    potential.write_bytes(b"configured H potential\r\n")
    write_poscar(tmp_path / "POSCAR", 1)
    result = generate_smart_potcar(str(tmp_path / "POSCAR"), output_path=str(tmp_path / "assembled.POTCAR"))
    assert "successfully" in result
    assert (tmp_path / "assembled.POTCAR").read_bytes() == potential.read_bytes()
