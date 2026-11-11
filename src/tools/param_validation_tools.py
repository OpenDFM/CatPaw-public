import json
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from pydantic import Field

from src.utils.config import select_hpc_resource_profile
from src.utils.results import PARAMETER_SUMMARY_FILENAME
from src.utils.task_types import CANONICAL_TASK_TYPES

COMPARABLE_KEYS = (
    "ENCUT",
    "PREC",
    "GGA",
    "METAGGA",
    "LHFCALC",
    "HFSCREEN",
    "AEXX",
    "LASPH",
    "LREAL",
    "EDIFF",
    "LDIPOL",
    "IDIPOL",
    "LDAUTYPE",
    "ISPIN",
    "LDAU",
    "LDAUL",
    "LDAUU",
    "LDAUJ",
    "IVDW",
    "ISMEAR",
    "SIGMA",
)
FFT_KEYS = ("NGXF", "NGYF", "NGZF")


def empty_result() -> dict[str, Any]:
    return {
        "passed": True,
        "errors": [],
        "warnings": [],
        "findings": [],
        "repair_hints": [],
    }


def parse_incar(path: Path) -> dict[str, str]:
    params: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        clean = line.split("#", 1)[0].strip()
        if not clean or "=" not in clean:
            continue
        key, value = clean.split("=", 1)
        params[key.strip().upper()] = value.strip()
    return params


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    token = str(value).strip().split()[0] if str(value).strip() else ""
    try:
        return int(float(token))
    except ValueError:
        return None


def is_true(value: Any) -> bool:
    return str(value).strip().upper() in {".TRUE.", "TRUE", "T", "1"}


def normal_value(value: Any) -> str:
    return str(value if value is not None else "").strip().strip("'\"").lower()


def parse_kpoints_grid(path: Path) -> list[int] | None:
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
    if len(lines) < 4:
        return None
    try:
        grid = [int(part) for part in lines[3].split()[:3]]
    except ValueError:
        return None
    return grid if len(grid) == 3 and all(value >= 1 for value in grid) else None


def read_atom_count(poscar_path: Path) -> int:
    from pymatgen.io.vasp import Poscar

    return len(Poscar.from_file(str(poscar_path)).structure)


def read_parameter_summary_calculations(active_dir: Path) -> dict[str, dict[str, Any]]:
    path = active_dir / PARAMETER_SUMMARY_FILENAME
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing or empty {PARAMETER_SUMMARY_FILENAME}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {PARAMETER_SUMMARY_FILENAME}: {exc}") from exc
    calculations = payload.get("calculations") if isinstance(payload, dict) else None
    if not isinstance(calculations, list):
        raise ValueError(f"{PARAMETER_SUMMARY_FILENAME} calculations must be a list")

    by_subdir: dict[str, dict[str, Any]] = {}
    for calculation in calculations:
        if not isinstance(calculation, dict):
            continue
        subdir = str(calculation.get("subdir") or "").strip().rstrip("/")
        if subdir:
            by_subdir[subdir] = calculation
    return by_subdir


def validate_resource_plan(
    *,
    subdir: str,
    poscar_path: Path,
    incar: dict[str, str],
    summary_calculation: dict[str, Any] | None,
    result: dict[str, Any],
) -> None:
    if summary_calculation is None:
        result["errors"].append(f"{subdir}: calculation is missing from {PARAMETER_SUMMARY_FILENAME}.")
        result["repair_hints"].append(f"Add the {subdir} calculation entry to {PARAMETER_SUMMARY_FILENAME}.")
        return

    try:
        atom_count = read_atom_count(poscar_path)
        profile = select_hpc_resource_profile(atom_count)
    except Exception as exc:
        result["errors"].append(f"{subdir}: resource profile selection failed: {exc}")
        result["repair_hints"].append("Fix hpc.resource_profiles in config.yaml.")
        return

    recorded_profile = str(summary_calculation.get("resource_profile") or "").strip()
    if recorded_profile != profile.name:
        result["errors"].append(f"{subdir}: resource_profile must be {profile.name!r}; found {recorded_profile!r}.")
        result["repair_hints"].append(f"Record SystemComplexityEvaluator profile {profile.name!r} for {subdir}.")

    submit_params = summary_calculation.get("submit_params")
    if not isinstance(submit_params, dict):
        result["errors"].append(f"{subdir}: submit_params must be a mapping.")
        result["repair_hints"].append(
            f"Regenerate {PARAMETER_SUMMARY_FILENAME} submit_params from SystemComplexityEvaluator."
        )
        return

    expected = {
        "total_cpus": profile.total_cpus,
        "num_dcus": 1,
        "omp_threads": profile.omp_threads,
        "KPAR": profile.kpar,
        "NCORE": profile.ncore,
    }
    actual = {
        "total_cpus": parse_int(submit_params.get("total_cpus")),
        "num_dcus": parse_int(submit_params.get("num_dcus")),
        "omp_threads": parse_int(submit_params.get("omp_threads")),
        "KPAR": parse_int(incar.get("KPAR")),
        "NCORE": parse_int(incar.get("NCORE")),
    }
    for key, expected_value in expected.items():
        if actual[key] != expected_value:
            result["errors"].append(
                f"{subdir}: resource profile {profile.name!r} requires {key}={expected_value}; found {actual[key]!r}."
            )
            result["repair_hints"].append(
                f"Regenerate {subdir} resource settings from SystemComplexityEvaluator profile {profile.name!r}."
            )

    if not str(submit_params.get("job_name") or "").strip():
        result["errors"].append(f"{subdir}: submit_params.job_name cannot be empty.")
        result["repair_hints"].append(f"Set a non-empty submit_params.job_name for {subdir}.")

    result["findings"].append(
        f"{subdir}: resource profile={profile.name}, atoms={atom_count}, "
        f"total_cpus={actual['total_cpus']}, KPAR={actual['KPAR']}, "
        f"NCORE={actual['NCORE']}, omp_threads={actual['omp_threads']}."
    )


def find_vacuum_axes(poscar_path: Path) -> list[int]:
    from pymatgen.io.vasp import Poscar

    struct = Poscar.from_file(str(poscar_path)).structure
    return [idx for idx, length in enumerate(struct.lattice.abc) if float(length) > 15.0]


def potcar_titles(path: Path) -> tuple[str, ...]:
    """Read potential identities including functional, variant and version date."""
    with path.open(encoding="utf-8", errors="ignore") as handle:
        return tuple(line.split("=", 1)[1].strip() for line in handle if line.lstrip().startswith("TITEL"))


def fft_grid_from_incar(incar: dict[str, str]) -> dict[str, int] | None:
    grid: dict[str, int] = {}
    for key in FFT_KEYS:
        value = parse_int(incar.get(key))
        if value is None:
            return None
        grid[key] = value
    return grid


def fft_grid_from_file(path: Path) -> dict[str, int] | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    chunks: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if any(key in line for key in FFT_KEYS):
                chunks.append(line)
                if len(chunks) >= 20:
                    break
    text = "".join(chunks)
    grid: dict[str, int] = {}
    for key in FFT_KEYS:
        match = re.search(rf"\b{key}\b\s*=?\s*(\d+)", text, flags=re.IGNORECASE)
        if not match:
            return None
        grid[key] = int(match.group(1))
    return grid


def resolve_upstream_fft_grid(
    job: dict[str, Any], active_dir: Path, calc_dir: Path
) -> tuple[dict[str, int] | None, str]:
    explicit_grid = job.get("upstream_fft_grid")
    if isinstance(explicit_grid, dict):
        grid = {key: parse_int(explicit_grid.get(key)) for key in FFT_KEYS}
        if all(value is not None for value in grid.values()):
            return {key: int(value) for key, value in grid.items() if value is not None}, "job.upstream_fft_grid"

    source = job.get("upstream_fft_source")
    if isinstance(source, str) and source.strip():
        path = Path(source)
        if not path.is_absolute():
            path = active_dir / path
        grid = fft_grid_from_file(path)
        if grid:
            return grid, str(path)

    for root in (calc_dir, active_dir):
        for pattern in ("OUTCAR_from*", "INCAR_from*", "*_OUTCAR", "*_INCAR"):
            for path in sorted(root.glob(pattern)):
                grid = fft_grid_from_file(path)
                if grid:
                    return grid, str(path)
    return None, ""


def species_values(text: str) -> list[float]:
    """Expand VASP's N*value syntax for element-indexed +U arrays."""
    expanded = []
    for token in text.split():
        count, value = token.split("*", 1) if "*" in token else ("1", token)
        expanded.extend([float(value)] * int(count))
    return expanded


def compare_reference(calc_dir: Path, reference: dict[str, Any]) -> list[str]:
    """Compare actual same-stage family inputs, aligning element-indexed values."""
    current = parse_incar(calc_dir / "INCAR")
    files = reference["input_files"]
    baseline = parse_incar(Path(files["incar"]))
    differences = [
        key
        for key in COMPARABLE_KEYS
        if key not in {"LDAUL", "LDAUU", "LDAUJ"}
        and normal_value(current.get(key)) != normal_value(baseline.get(key))
    ]
    if parse_kpoints_grid(calc_dir / "KPOINTS") != parse_kpoints_grid(Path(files["kpoints"])):
        differences.append("KPOINTS")
    current_tags = potcar_titles(calc_dir / "POTCAR")
    reference_tags = potcar_titles(Path(files["potcar"]))
    current_by_element = {tag.split()[1].split("_")[0]: (i, tag) for i, tag in enumerate(current_tags)}
    for j, tag in enumerate(reference_tags):
        element = tag.split()[1].split("_")[0]
        if element not in current_by_element:
            continue  # A vacancy or substitution may remove a species entirely.
        i, current_tag = current_by_element[element]
        if current_tag != tag:
            differences.append(f"POTCAR[{element}]")
        if is_true(baseline.get("LDAU")):
            for key in ("LDAUL", "LDAUU", "LDAUJ"):
                if species_values(current[key])[i] != species_values(baseline[key])[j]:
                    differences.append(f"{key}[{element}]")
    return differences


def job_bool(job: dict[str, Any], key: str) -> bool:
    return bool(job.get(key))


def validate_parameter_policy(active_node_dir: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Validate high-risk VASP parameter policy errors from generated input files.

    The caller owns semantic context extraction from task.md/modeling_summary.json/
    parameter_summary.json. Each job should provide at least `subdir` and `task_type`.
    Optional keys: family_key, comparable, dftu_allowed, explicit_dftu_request,
    requires_fft_inheritance, upstream_fft_grid, upstream_fft_source,
    max_slab_inplane_k.
    """
    result = empty_result()
    active_dir = Path(active_node_dir).resolve()
    if not active_dir.is_dir():
        result["errors"].append(f"ACTIVE_NODE_DIR does not exist: {active_dir}")
        result["repair_hints"].append("Provide a valid ACTIVE_NODE_DIR.")
        result["passed"] = False
        return result
    if not jobs:
        result["errors"].append("ValidateParameterPolicy requires a non-empty jobs list.")
        result["repair_hints"].append(
            "ParamValidator must pass subdir/task_type context extracted from node artifacts."
        )
        result["passed"] = False
        return result

    comparable_groups: dict[str, list[tuple[str, Path]]] = defaultdict(list)

    try:
        summary_calculations = read_parameter_summary_calculations(active_dir)
    except ValueError as exc:
        result["errors"].append(str(exc))
        result["repair_hints"].append(f"Regenerate a valid {PARAMETER_SUMMARY_FILENAME} before parameter validation.")
        result["passed"] = False
        return result

    inputs_path = active_dir / "inputs.json"
    inputs = json.loads(inputs_path.read_text()) if inputs_path.is_file() else {}
    reference = None
    if "parameter_reference" in inputs:
        entries = json.loads(Path(inputs["parameter_reference"]).read_text())["calculations"]
        if len(entries) != 1:
            raise ValueError("parameter_reference must identify one prepared scientific state")
        reference = entries[0]

    for job in jobs:
        subdir = str(job.get("subdir") or "").strip().rstrip("/")
        task_type = str(job.get("task_type") or "").strip()
        if task_type == "dos":
            task_type = "dos_calc"
        if not subdir or not task_type:
            result["errors"].append(f"Invalid job entry missing subdir/task_type: {job}")
            result["repair_hints"].append("Pass each job as {'subdir': ..., 'task_type': ...}.")
            continue
        if task_type not in CANONICAL_TASK_TYPES:
            result["errors"].append(
                f"{subdir}: unknown task_type={task_type!r}; expected one of {sorted(CANONICAL_TASK_TYPES)}."
            )
            result["repair_hints"].append(
                f"Use a canonical task_type for {subdir}; for fixed-coordinate molecule energy use task_type='scf', not an alias such as 'mol_static'."
            )
            continue

        calc_dir = active_dir / subdir
        poscar_path = calc_dir / "POSCAR"
        incar_path = calc_dir / "INCAR"
        kpoints_path = calc_dir / "KPOINTS"
        missing = [
            path.name
            for path in (poscar_path, incar_path, kpoints_path)
            if not path.is_file() or path.stat().st_size == 0
        ]
        if missing:
            result["errors"].append(f"{subdir}: missing required input file(s) for policy validation: {missing}.")
            result["repair_hints"].append(
                f"Regenerate missing input files for {subdir} before ParamValidator policy checks."
            )
            continue

        try:
            incar = parse_incar(incar_path)
            kpoints = parse_kpoints_grid(kpoints_path)
            vacuum_axes = find_vacuum_axes(poscar_path)
        except Exception as exc:
            result["errors"].append(f"{subdir}: failed to parse VASP inputs for policy validation: {exc}")
            result["repair_hints"].append(f"Regenerate or repair POSCAR/INCAR/KPOINTS for {subdir}.")
            continue

        if reference is not None:
            differences = compare_reference(calc_dir, reference)
            if task_type != reference["task_type"]:
                differences.append("task_type (parameter_reference is for same-stage comparisons)")
            if differences:
                result["errors"].append(f"{subdir}: shared parameter reference mismatch: {', '.join(differences)}.")
                result["repair_hints"].append(
                    "Regenerate this member from its parameter_reference; do not edit the reference."
                )
            else:
                result["findings"].append(
                    f"{subdir}: matched shared parameter reference {inputs['parameter_reference']}."
                )

        isif = parse_int(incar.get("ISIF"))
        nsw = parse_int(incar.get("NSW"))
        ibrion = parse_int(incar.get("IBRION"))
        max_slab_inplane_k = int(job.get("max_slab_inplane_k") or 6)
        validate_resource_plan(
            subdir=subdir,
            poscar_path=poscar_path,
            incar=incar,
            summary_calculation=summary_calculations.get(subdir),
            result=result,
        )

        if task_type == "bulk_opt" and isif != 3:
            result["errors"].append(f"{subdir}: bulk_opt must use ISIF=3; found ISIF={incar.get('ISIF')!r}.")
            result["repair_hints"].append(f"Regenerate {subdir}/INCAR with task_type=bulk_opt and ISIF=3.")

        if task_type == "mol_opt":
            if isif != 2:
                result["errors"].append(f"{subdir}: mol_opt must use ISIF=2; found ISIF={incar.get('ISIF')!r}.")
                result["repair_hints"].append(f"Regenerate {subdir}/INCAR with task_type=mol_opt and ISIF=2.")
            if kpoints != [1, 1, 1]:
                result["errors"].append(f"{subdir}: mol_opt must use Gamma-only KPOINTS [1, 1, 1]; found {kpoints}.")
                result["repair_hints"].append(f"Regenerate {subdir}/KPOINTS as Gamma-only [1, 1, 1].")

        if task_type == "slab_opt":
            if isif != 2:
                result["errors"].append(f"{subdir}: slab_opt must use ISIF=2; found ISIF={incar.get('ISIF')!r}.")
                result["repair_hints"].append(f"Regenerate {subdir}/INCAR with ISIF=2.")
            if kpoints is None:
                result["errors"].append(f"{subdir}: KPOINTS mesh could not be parsed.")
                result["repair_hints"].append(f"Regenerate {subdir}/KPOINTS with CreateKpoints.")
            else:
                for axis in vacuum_axes:
                    if kpoints[axis] != 1:
                        result["errors"].append(
                            f"{subdir}: slab vacuum axis {axis + 1} has K={kpoints[axis]}; expected 1."
                        )
                        result["repair_hints"].append(
                            f"Regenerate {subdir}/KPOINTS with K=1 along the vacuum direction."
                        )
                in_plane = [value for axis, value in enumerate(kpoints) if axis not in vacuum_axes]
                if in_plane and max(in_plane) > max_slab_inplane_k and not job_bool(job, "allow_dense_kpoints"):
                    result["errors"].append(
                        f"{subdir}: slab_opt in-plane KPOINTS {kpoints} exceed limit {max_slab_inplane_k}."
                    )
                    result["repair_hints"].append(
                        f"Use normal KPOINTS density for {subdir} or pass allow_dense_kpoints=true with a documented reason."
                    )

        if task_type in {"scf", "dos_calc"}:
            if nsw is not None and nsw > 0:
                result["errors"].append(f"{subdir}: {task_type} should not continue ionic relaxation; found NSW={nsw}.")
                result["repair_hints"].append(f"Set NSW=0 for {subdir}.")
            if ibrion is not None and ibrion != -1:
                result["errors"].append(f"{subdir}: {task_type} should use IBRION=-1; found IBRION={ibrion}.")
                result["repair_hints"].append(f"Set IBRION=-1 for {subdir}.")

        if is_true(incar.get("LDAU")) and not (
            job_bool(job, "dftu_allowed") or job_bool(job, "explicit_dftu_request")
        ):
            result["errors"].append(
                f"{subdir}: LDAU=.TRUE. but ParamValidator did not mark DFT+U as allowed or explicitly requested."
            )
            result["repair_hints"].append(
                f"Remove DFT+U from {subdir}, or pass dftu_allowed=true only with a documented physical reason."
            )

        if job_bool(job, "requires_fft_inheritance"):
            current_grid = fft_grid_from_incar(incar)
            upstream_grid, upstream_source = resolve_upstream_fft_grid(job, active_dir, calc_dir)
            if upstream_grid is None:
                result["errors"].append(
                    f"{subdir}: requires FFT-grid inheritance but no upstream NGXF/NGYF/NGZF was provided or found."
                )
                result["repair_hints"].append(
                    f"Pass upstream_fft_grid/upstream_fft_source for {subdir}, or regenerate with inherited FFT grid."
                )
            elif current_grid is None:
                result["errors"].append(
                    f"{subdir}: requires FFT-grid inheritance but current INCAR lacks NGXF/NGYF/NGZF."
                )
                result["repair_hints"].append(f"Set {subdir}/INCAR NGXF/NGYF/NGZF to {upstream_grid}.")
            elif current_grid != upstream_grid:
                result["errors"].append(
                    f"{subdir}: current FFT grid {current_grid} does not match upstream {upstream_grid} from {upstream_source}."
                )
                result["repair_hints"].append(f"Set {subdir}/INCAR NGXF/NGYF/NGZF to {upstream_grid}.")

        family_key = str(job.get("family_key") or "").strip()
        if family_key and job.get("comparable", True):
            comparable_groups[family_key].append((subdir, calc_dir))

        result["findings"].append(
            f"{subdir}: checked task_type={task_type}, ISIF={incar.get('ISIF')}, NSW={incar.get('NSW')}, KPOINTS={kpoints}, LDAU={incar.get('LDAU', 'absent')}."
        )

    for family_key, entries in comparable_groups.items():
        if len(entries) < 2:
            continue
        ref_subdir, reference_dir = entries[0]
        reference = {
            "input_files": {name.lower(): str(reference_dir / name) for name in ("INCAR", "KPOINTS", "POTCAR")}
        }
        for subdir, calc_dir in entries[1:]:
            differences = compare_reference(calc_dir, reference)
            if differences:
                result["errors"].append(
                    f"Comparable family {family_key!r}: {subdir} differs from {ref_subdir}: {', '.join(differences)}."
                )
                result["repair_hints"].append(f"Align shared precision across family {family_key!r}.")

    result["passed"] = len(result["errors"]) == 0
    return result


def format_validation_report(result: dict[str, Any]) -> str:
    status = "PASSED" if result["passed"] else "FAILED"
    lines = [f"[Parameter Policy Validation] {status}"]
    for finding in result.get("findings", []):
        lines.append(f"  [FINDING] {finding}")
    for warning in result.get("warnings", []):
        lines.append(f"  [WARNING] {warning}")
    for error in result.get("errors", []):
        lines.append(f"  [ERROR] {error}")
    for hint in result.get("repair_hints", []):
        lines.append(f"  [REPAIR] {hint}")
    return "\n".join(lines)


class ValidateParameterPolicyAction(Action):
    active_node_dir: str = Field(description="ACTIVE_NODE_DIR containing generated VASP input subdirectories.")
    jobs: list[dict[str, Any]] = Field(
        description=(
            "Structured job context prepared by ParamValidator. Each item needs subdir and task_type; "
            "optional keys include family_key, comparable, dftu_allowed, explicit_dftu_request, "
            "requires_fft_inheritance, upstream_fft_grid, upstream_fft_source, max_slab_inplane_k."
        )
    )


class ValidateParameterPolicyObservation(Observation):
    message: str = Field(default="")
    passed: bool = Field(default=False)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    repair_hints: list[str] = Field(default_factory=list)

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=self.message)]


class ValidateParameterPolicyExecutor(ToolExecutor[ValidateParameterPolicyAction, ValidateParameterPolicyObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: ValidateParameterPolicyAction, conversation=None) -> ValidateParameterPolicyObservation:
        active_dir = Path(action.active_node_dir)
        if not active_dir.is_absolute():
            active_dir = Path(self.workspace_dir) / active_dir
        result = validate_parameter_policy(str(active_dir), action.jobs)
        return ValidateParameterPolicyObservation(
            message=format_validation_report(result),
            passed=bool(result["passed"]),
            errors=list(result.get("errors", [])),
            warnings=list(result.get("warnings", [])),
            findings=list(result.get("findings", [])),
            repair_hints=list(result.get("repair_hints", [])),
        )


class ValidateParameterPolicyTool(ToolDefinition[ValidateParameterPolicyAction, ValidateParameterPolicyObservation]):
    """Check high-risk VASP parameter policy mistakes before submission."""

    name: ClassVar[str] = "ValidateParameterPolicy"

    DESCRIPTION: ClassVar[str] = (
        "Validate generated INCAR/KPOINTS/POTCAR policy for task-type rules, DFT+U allowance, "
        "comparable-family precision consistency, slab KPOINTS limits, and FFT-grid inheritance."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=ValidateParameterPolicyAction,
                observation_type=ValidateParameterPolicyObservation,
                executor=ValidateParameterPolicyExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


register_tool("ValidateParameterPolicy", ValidateParameterPolicyTool)


def get_param_validator_tools_config() -> list[Tool]:
    """Tool configuration for ParamValidator."""
    return [Tool(name="ValidateParameterPolicy")]
