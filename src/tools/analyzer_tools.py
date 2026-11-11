from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from pydantic import Field

from src.utils.results import (
    MODELING_SUMMARY_FILENAME,
    MODELING_VALIDATION_FILENAME,
    NODE_RESULT_FILENAME,
    PARAMETER_SUMMARY_FILENAME,
    PARAMETER_VALIDATION_FILENAME,
    RUNNER_RESULT_FILENAME,
    read_authoritative_calculations,
    read_runner_result,
    write_json,
)

# Shared helpers


def resolve_dir(workspace_dir: str, target_dir: str) -> Path:
    target = Path(target_dir)
    if not target.is_absolute():
        target = Path(workspace_dir) / target
    return target.resolve()


def json_block(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def abs_existing_or_expected(node_dir: Path, filename: str) -> str:
    return str((node_dir / filename).resolve())


class AnalyzerToolObservation(Observation):
    message: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


# Analysis helpers


def read_job_status_results(workspace_dir: str) -> dict[str, str]:
    payload = read_runner_result(workspace_dir) or {}
    job_status = payload.get("job_status")
    if not isinstance(job_status, dict):
        return {}
    results = job_status.get("results")
    if not isinstance(results, dict):
        return {}
    return {k: v.upper() for k, v in results.items() if isinstance(k, str) and isinstance(v, str)}


def read_analysis_contract_entries(workspace_dir: str) -> dict[str, dict[str, Any]]:
    summary_path = Path(workspace_dir) / PARAMETER_SUMMARY_FILENAME
    if not summary_path.is_file():
        return {}
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    calculations = payload.get("calculations")
    if not isinstance(calculations, list):
        return {}
    contracts: dict[str, dict[str, Any]] = {}
    for calc in calculations:
        if not isinstance(calc, dict):
            continue
        subdir = calc.get("subdir")
        contract = calc.get("analysis_contract")
        if isinstance(subdir, str) and subdir.strip() and isinstance(contract, dict):
            entry = dict(contract)
            entry.setdefault("subdir", subdir.strip())
            task_type = calc.get("task_type")
            if isinstance(task_type, str) and task_type.strip():
                entry.setdefault("task_type", task_type.strip())
            contracts[subdir.strip()] = entry
    return contracts


def subdir_name(workspace_dir: str, subdir: Path) -> str:
    try:
        return subdir.resolve().relative_to(Path(workspace_dir).resolve()).as_posix()
    except ValueError:
        return subdir.name


def resolve_authoritative_subdir(workspace_dir: str, logical_subdir: Path) -> Path:
    """Resolve a stable logical calculation path to its selected physical attempt."""
    logical_name = subdir_name(workspace_dir, logical_subdir)
    selected = read_authoritative_calculations(workspace_dir).get(logical_name)
    if not selected:
        return logical_subdir
    workspace = Path(workspace_dir).resolve()
    candidate = (workspace / selected).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        return logical_subdir
    return candidate if candidate.is_dir() else logical_subdir


def artifact_flags(subdir: Path) -> dict[str, bool]:
    names = [
        "INCAR",
        "KPOINTS",
        "POTCAR",
        "POSCAR",
        "CONTCAR",
        "OUTCAR",
        "OSZICAR",
        "vasprun.xml",
        "EIGENVAL",
        "DOSCAR",
        "CHGCAR",
        "AECCAR0",
        "AECCAR2",
        "ACF.dat",
    ]
    return {name.replace(".", "_").lower(): (subdir / name).exists() for name in names}


SI_INCAR_KEYS = [
    "ENCUT",
    "ISPIN",
    "NSW",
    "ISIF",
    "IBRION",
    "EDIFF",
    "EDIFFG",
    "ISMEAR",
    "SIGMA",
    "LORBIT",
    "NELM",
    "PREC",
    "ALGO",
    "LREAL",
    "LDAU",
    "LDAUL",
    "LDAUU",
    "LDAUJ",
    "IVDW",
    "NCORE",
    "KPAR",
]


def parse_all_incar_params(incar_path: Path) -> dict[str, str | None]:
    from src.monitor.convergence import parse_incar_value

    return {key.lower(): parse_incar_value(incar_path, key) for key in SI_INCAR_KEYS}


def parse_kpoints_info(kpoints_path: Path) -> dict[str, str | None]:
    if not kpoints_path.is_file():
        return {"kpoints_type": None, "kpoints_grid": None}
    try:
        lines = kpoints_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {"kpoints_type": None, "kpoints_grid": None}
    if len(lines) < 4:
        return {"kpoints_type": None, "kpoints_grid": None}
    mode_line = lines[2].strip().lower()
    kp_type = "Gamma" if mode_line.startswith("g") else "Monkhorst-Pack"
    grid_parts = lines[3].strip().split()[:3]
    grid = "x".join(grid_parts) if len(grid_parts) == 3 else lines[3].strip()
    return {"kpoints_type": kp_type, "kpoints_grid": grid}


def parse_potcar_symbols(potcar_path: Path) -> list[str] | None:
    if not potcar_path.is_file():
        return None
    symbols: list[str] = []
    try:
        with open(potcar_path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "TITEL" in line:
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        titel_parts = parts[1].strip().split()
                        symbols.append(
                            titel_parts[1] if len(titel_parts) >= 2 else titel_parts[0] if titel_parts else ""
                        )
    except OSError:
        return None
    return symbols or None


def parse_structure_info(subdir: Path) -> dict[str, Any]:
    structure_path = subdir / "CONTCAR"
    if not structure_path.is_file() or structure_path.stat().st_size == 0:
        structure_path = subdir / "POSCAR"
    if not structure_path.is_file():
        return {"atom_count": None, "formula": None}
    try:
        from pymatgen.core import Structure

        s = Structure.from_file(str(structure_path))
        return {"atom_count": len(s), "formula": s.composition.reduced_formula}
    except Exception:
        return {"atom_count": None, "formula": None}


def build_computational_params(subdir: Path) -> dict[str, Any]:
    incar = parse_all_incar_params(subdir / "INCAR")
    kpoints = parse_kpoints_info(subdir / "KPOINTS")
    potcar = parse_potcar_symbols(subdir / "POTCAR")
    struct = parse_structure_info(subdir)
    return {**incar, **kpoints, "potcar_symbols": potcar, **struct}


def infer_job_state(workspace_dir: str, subdir: Path, converged: bool, errors: list[str]) -> str:
    job_state = read_job_status_results(workspace_dir).get(subdir_name(workspace_dir, subdir))
    if job_state in {"CONVERGED", "FAILED", "CANCELLED", "NOT_CONVERGED", "RUNNING", "SUBMITTED", "COMPLETED"}:
        return job_state
    if converged:
        return "CONVERGED"
    if errors:
        joined = " ".join(errors).lower()
        if "cancelled" in joined or "canceled" in joined:
            return "CANCELLED"
        if "not converged" in joined or "reached nelm" in joined or "reached nsw" in joined:
            return "NOT_CONVERGED"
        return "FAILED"
    return "COMPLETED" if (subdir / ".job_id").exists() else "UNKNOWN"


def structure_health(subdir: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"checked": False, "min_distance_A": None, "has_severe_overlap": False, "message": ""}
    structure_path = subdir / "CONTCAR"
    if not structure_path.is_file() or structure_path.stat().st_size == 0:
        structure_path = subdir / "POSCAR"
    if not structure_path.is_file():
        report["message"] = "POSCAR/CONTCAR not found."
        return report
    try:
        from pymatgen.core import Structure

        s = Structure.from_file(str(structure_path))
        if len(s) < 2:
            report["checked"] = True
            report["message"] = "Single-atom system; distance screening skipped."
            return report
        dm = s.distance_matrix
        min_d = min(float(dm[i][j]) for i in range(len(dm)) for j in range(i + 1, len(dm)))
        report["checked"] = True
        report["min_distance_A"] = round(min_d, 4)
        report["has_severe_overlap"] = min_d < 0.75
        report["message"] = (
            f"Minimum interatomic distance is only {min_d:.3f} A; severe overlap is suspected."
            if report["has_severe_overlap"]
            else "Normal."
        )
    except Exception as exc:
        report["message"] = f"Structure screening failed: {exc}"
    return report


def diagnose_failure(
    convergence_data: dict[str, Any],
    structure_health: dict[str, Any],
    oscillation_data: dict[str, Any],
    artifacts: dict[str, bool],
) -> dict[str, Any]:
    errors = list(convergence_data.get("errors") or [])
    evidence: list[str] = []
    recommended_actions: list[str] = []

    if convergence_data.get("converged"):
        status, root_cause = "ok", "none"
        if structure_health.get("has_severe_overlap"):
            status, root_cause = "uncertain", "structure"
            evidence.append(structure_health["message"])
        return {
            "status": status,
            "root_cause": root_cause,
            "evidence": evidence,
            "recommended_actions": recommended_actions,
        }

    fatal_keywords = ("BRMIX", "EDDDAV", "ZBRENT", "LAPACK", "sub-space matrix", "VERY BAD NEWS")
    missing_outputs = [n for n, exists in artifacts.items() if n in ("outcar", "oszicar", "contcar") and not exists]

    if structure_health.get("has_severe_overlap"):
        evidence.append(structure_health["message"])
        evidence.extend(errors[:3])
        return {
            "status": "needs_remodel",
            "root_cause": "structure",
            "evidence": evidence,
            "recommended_actions": ["Trace back ModelingWorker structure construction and substrate setup."],
        }

    if missing_outputs and not artifacts.get("outcar"):
        evidence.append(f"Critical output files are missing: {', '.join(missing_outputs)}")
        evidence.extend(errors[:2])
        return {
            "status": "infra_error",
            "root_cause": "infrastructure",
            "evidence": evidence,
            "recommended_actions": ["Prioritize checking job logs, queue status, and runtime environment."],
        }

    if any(kw in err for err in errors for kw in fatal_keywords) or convergence_data.get("reached_nelm"):
        evidence.extend(errors[:4])
        if oscillation_data.get("details"):
            evidence.append(oscillation_data["details"])
        return {
            "status": "needs_replan",
            "root_cause": "parameter",
            "evidence": evidence,
            "recommended_actions": [
                "Prioritize tracing ParamWorker settings: ENCUT / KPOINTS / ISPIN / DFT+U / mixing."
            ],
        }

    if oscillation_data.get("oscillating") or oscillation_data.get("stagnating"):
        evidence.append(oscillation_data.get("details") or "Convergence process exhibits oscillation/stagnation.")
        return {
            "status": "needs_replan",
            "root_cause": "parameter",
            "evidence": evidence,
            "recommended_actions": ["Recommend ParamWorker re-evaluate electronic-step and convergence settings."],
        }

    if convergence_data.get("reached_nsw"):
        evidence.extend(errors[:3])
        return {
            "status": "needs_replan",
            "root_cause": "parameter",
            "evidence": evidence,
            "recommended_actions": ["Recommend ParamWorker review NSW, EDIFFG, and ISIF."],
        }

    evidence.extend(errors[:4] or ["Insufficient evidence for causal attribution at this stage."])
    return {
        "status": "uncertain",
        "root_cause": "unknown",
        "evidence": evidence,
        "recommended_actions": ["Recommend manual review of output files."],
    }


# SummarizeCalculationResult


class SummarizeCalculationResultAction(Action):
    working_dir: str = Field(
        description="Absolute path of the calculation subdirectory to analyze, or a workspace-relative path."
    )


class SummarizeCalculationResultExecutor(ToolExecutor[SummarizeCalculationResultAction, AnalyzerToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: SummarizeCalculationResultAction, conversation=None) -> AnalyzerToolObservation:
        from src.monitor.convergence import (
            ConvergenceResult,
            check_oscillation,
            check_vasp_convergence,
            parse_site_magnetizations,
            parse_total_magnetization,
        )
        from src.utils.results import read_runner_result

        logical_subdir = resolve_dir(self.workspace_dir, action.working_dir)
        if not logical_subdir.is_dir():
            return AnalyzerToolObservation(
                message=f"Result summarization failed: directory does not exist: {logical_subdir}"
            )
        logical_name = subdir_name(self.workspace_dir, logical_subdir)
        subdir = resolve_authoritative_subdir(self.workspace_dir, logical_subdir)

        plan_entry = read_analysis_contract_entries(self.workspace_dir).get(logical_name, {})

        # Try cached convergence data from CalculationRunner's ExtractCalcResults
        cached_entry = None
        runner_result = read_runner_result(self.workspace_dir) or {}
        full_summary = runner_result.get("calc_summary")
        if isinstance(full_summary, dict):
            cached_entry = full_summary.get(subdir_name(self.workspace_dir, subdir))

        if cached_entry and isinstance(cached_entry, dict):
            convergence = ConvergenceResult(
                converged=cached_entry.get("converged", False),
                final_energy=cached_entry.get("final_energy_eV"),
                max_force=cached_entry.get("max_force_eVA"),
                ionic_steps=cached_entry.get("ionic_steps", 0),
                electronic_steps_last=cached_entry.get("electronic_steps_last", 0),
                reached_nelm=cached_entry.get("reached_nelm", False),
                reached_nsw=cached_entry.get("reached_nsw", False),
                errors=cached_entry.get("errors", []),
            )
        else:
            convergence = check_vasp_convergence(subdir)

        artifacts = artifact_flags(subdir)
        sh = structure_health(subdir)

        notes: list[str] = []
        for key in ("calc_purpose", "state_label", "reaction_group"):
            val = plan_entry.get(key)
            if isinstance(val, str) and val:
                notes.append(f"{key}={val}")
        suggested = plan_entry.get("suggested_analyses")
        if isinstance(suggested, list) and suggested:
            notes.append("suggested_analyses=" + ",".join(str(x) for x in suggested))
        for key in ("param_worker_notes_for_analyzer", "planner_notes_for_analyzer", "analyzer_goal"):
            val = plan_entry.get(key)
            if isinstance(val, str) and val.strip():
                notes.append(f"{key}={val.strip()}")

        oscillation = check_oscillation(subdir)
        diagnosis = diagnose_failure(asdict(convergence), sh, asdict(oscillation), artifacts)

        entry = {
            "subdir": logical_name,
            "working_dir": str(subdir),
            "selected_attempt": subdir_name(self.workspace_dir, subdir),
            "task_type": plan_entry.get("task_type"),
            "calc_purpose": plan_entry.get("calc_purpose"),
            "job_id": (subdir / ".job_id").read_text(encoding="utf-8").strip()
            if (subdir / ".job_id").is_file()
            else None,
            "job_state": infer_job_state(self.workspace_dir, subdir, convergence.converged, convergence.errors),
            "convergence": {
                "converged": convergence.converged,
                "final_energy_eV": convergence.final_energy,
                "ionic_steps": convergence.ionic_steps,
                "electronic_steps_last": convergence.electronic_steps_last,
                "reached_nelm": convergence.reached_nelm,
                "reached_nsw": convergence.reached_nsw,
                "max_force_eVA": convergence.max_force,
                "errors": convergence.errors,
            },
            "artifacts": artifacts,
            "structure_health": sh,
            "computational_params": build_computational_params(subdir),
            "notes": notes,
            "diagnosis": diagnosis,
        }
        if cached_entry and isinstance(cached_entry, dict):
            entry["total_magnetization"] = cached_entry.get("total_magnetization")
            entry["site_magnetizations"] = cached_entry.get("site_magnetizations")
            entry["min_bond_length_A"] = cached_entry.get("min_bond_length_A")
        else:
            entry["total_magnetization"] = parse_total_magnetization(subdir / "OSZICAR")
            species_list = None
            for fname in ("CONTCAR", "POSCAR"):
                struct_file = subdir / fname
                if struct_file.is_file() and struct_file.stat().st_size > 0:
                    try:
                        from pymatgen.core import Structure

                        species_list = [site.specie.symbol for site in Structure.from_file(str(struct_file))]
                    except Exception:
                        pass
                    break
            entry["site_magnetizations"] = parse_site_magnetizations(
                subdir / "OUTCAR",
                threshold=0.1,
                species_list=species_list,
            )
        return AnalyzerToolObservation(message="Calculation result summary:\n" + json_block(entry))


class SummarizeCalculationResultTool(ToolDefinition[SummarizeCalculationResultAction, AnalyzerToolObservation]):
    name: ClassVar[str] = "SummarizeCalculationResult"
    DESCRIPTION: ClassVar[str] = "Summarize convergence and key result files for one VASP work directory."

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=SummarizeCalculationResultAction,
                observation_type=AnalyzerToolObservation,
                executor=SummarizeCalculationResultExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


# WriteNodeResult


class WriteNodeResultAction(Action):
    node_dir: str = Field(
        description="Absolute active node directory, or a path relative to the current Analyzer workspace."
    )
    node_status: str = Field(description="Final node status: success, failed, or blocked.")
    error: str | None = Field(default=None, description="Concise error or blocker summary; use null for success.")
    artifacts: dict[str, str] = Field(
        default_factory=dict, description="Additional artifact key/path entries to merge into node_result.json."
    )
    calculations: list[dict[str, Any]] = Field(
        default_factory=list, description="Per-calculation summaries assembled by Analyzer."
    )
    analysis: dict[str, Any] = Field(default_factory=dict, description="Node-level analysis summary and findings.")
    recovery_attempted: bool = Field(
        default=False,
        description="True when an in-node runtime convergence recovery was performed for this node; consumes the shared per-node retry budget.",
    )


class WriteNodeResultExecutor(ToolExecutor[WriteNodeResultAction, AnalyzerToolObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: WriteNodeResultAction, conversation=None) -> AnalyzerToolObservation:
        try:
            node_dir = resolve_dir(self.workspace_dir, action.node_dir)
            if not node_dir.is_dir():
                return AnalyzerToolObservation(message=f"WriteNodeResult failed: node_dir does not exist: {node_dir}")

            status = action.node_status.strip().lower()
            if status not in {"success", "failed", "blocked"}:
                return AnalyzerToolObservation(
                    message="WriteNodeResult failed: node_status must be one of success, failed, or blocked."
                )

            artifacts = self.build_artifacts(node_dir, action.artifacts)
            analysis = self.normalize_analysis(action.analysis)
            payload = {
                "node_status": status,
                "error": action.error if status != "success" else None,
                "recovery_attempted": bool(
                    action.recovery_attempted or (read_runner_result(node_dir) or {}).get("recovery_attempts")
                ),
                "artifacts": artifacts,
                "calculations": action.calculations,
                "analysis": analysis,
            }

            result_path = node_dir / NODE_RESULT_FILENAME
            write_json(result_path, payload)
            return AnalyzerToolObservation(
                message=(
                    f"node_result.json written: {result_path}\n\n"
                    + json_block(
                        {
                            "node_status": status,
                            "error": payload["error"],
                            "node_result": str(result_path),
                            "calculation_count": len(action.calculations),
                        }
                    )
                )
            )
        except Exception as exc:
            return AnalyzerToolObservation(message=f"WriteNodeResult failed: {exc}")

    @staticmethod
    def build_artifacts(node_dir: Path, extra_artifacts: dict[str, str]) -> dict[str, str]:
        artifacts = {
            "node_result": abs_existing_or_expected(node_dir, NODE_RESULT_FILENAME),
            "modeling_summary": abs_existing_or_expected(node_dir, MODELING_SUMMARY_FILENAME),
            "modeling_validation": abs_existing_or_expected(node_dir, MODELING_VALIDATION_FILENAME),
            "parameter_summary": abs_existing_or_expected(node_dir, PARAMETER_SUMMARY_FILENAME),
            "parameter_validation": abs_existing_or_expected(node_dir, PARAMETER_VALIDATION_FILENAME),
            "runner_result": abs_existing_or_expected(node_dir, RUNNER_RESULT_FILENAME),
        }
        final_structure = node_dir / "CONTCAR"
        if final_structure.is_file() and final_structure.stat().st_size > 0:
            artifacts["final_structure"] = str(final_structure.resolve())
        elif (node_dir / "POSCAR").is_file():
            artifacts["initial_structure"] = str((node_dir / "POSCAR").resolve())

        for filename, key in (
            ("OUTCAR", "final_outcar"),
            ("CHGCAR", "final_chgcar"),
            ("WAVECAR", "final_wavecar"),
        ):
            path = node_dir / filename
            if path.is_file() and path.stat().st_size > 0:
                artifacts[key] = str(path.resolve())

        for key, value in extra_artifacts.items():
            clean_key = str(key).strip()
            clean_value = str(value).strip()
            if not clean_key or not clean_value:
                continue
            path = Path(clean_value)
            if not path.is_absolute():
                path = node_dir / path
            if not path.exists():
                continue
            if path.is_file() and path.stat().st_size == 0:
                continue
            artifacts[clean_key] = str(path.resolve())
        # Each new node is one logical calculation; recovery keeps its identity.
        selected = read_authoritative_calculations(str(node_dir))
        summary_path = node_dir / PARAMETER_SUMMARY_FILENAME
        calculations = json.loads(summary_path.read_text())["calculations"] if summary_path.is_file() else []
        if len(calculations) == 1:
            logical = calculations[0]["subdir"]
            calc_dir = node_dir / selected.get(logical, logical)
            for filename, key in (
                ("CONTCAR", "final_structure"),
                ("OUTCAR", "final_outcar"),
                ("CHGCAR", "final_chgcar"),
                ("WAVECAR", "final_wavecar"),
            ):
                path = calc_dir / filename
                if path.is_file() and path.stat().st_size:
                    artifacts[key] = str(path.resolve())
        return artifacts

    @staticmethod
    def normalize_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(analysis) if isinstance(analysis, dict) else {}
        normalized["summary"] = str(normalized.get("summary") or "")
        for key in ("key_findings", "limitations", "suggested_followups"):
            value = normalized.get(key)
            if isinstance(value, list):
                normalized[key] = value
            elif value in (None, ""):
                normalized[key] = []
            else:
                normalized[key] = [value]
        return normalized


class WriteNodeResultTool(ToolDefinition[WriteNodeResultAction, AnalyzerToolObservation]):
    name: ClassVar[str] = "WriteNodeResult"
    DESCRIPTION: ClassVar[str] = (
        "Write the node_result.json DAG completion contract with normalized status, artifacts, "
        "calculations, and analysis sections."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=WriteNodeResultAction,
                observation_type=AnalyzerToolObservation,
                executor=WriteNodeResultExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


# Registration

register_tool("SummarizeCalculationResult", SummarizeCalculationResultTool)
register_tool("WriteNodeResult", WriteNodeResultTool)


def get_analyzer_tools_config() -> list[Tool]:
    return [
        Tool(name="SummarizeCalculationResult"),
        Tool(name="WriteNodeResult"),
    ]
