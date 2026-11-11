"""VASP output file parsing for convergence checking.

Parses OSZICAR, OUTCAR, and INCAR to determine whether a VASP calculation
has converged (both electronically and ionically).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConvergenceResult:
    """Result of VASP convergence analysis for a single calculation directory."""

    converged: bool
    ionic_steps: int = 0
    electronic_steps_last: int = 0  # electronic iterations in the last ionic step
    final_energy: float | None = None
    reached_nelm: bool = False  # electronic steps == NELM → SCF did not converge
    reached_nsw: bool = False  # ionic steps == NSW → relaxation incomplete
    max_force: float | None = None  # maximum force on atoms, eV/A (None if unavailable)
    errors: list[str] = field(default_factory=list)


# Known VASP fatal error patterns in OUTCAR
FATAL_ERROR_PATTERNS = [
    re.compile(r"ZBRENT:\s*fatal", re.IGNORECASE),
    re.compile(r"VERY BAD NEWS", re.IGNORECASE),
    re.compile(r"BRMIX:\s*very serious problems", re.IGNORECASE),
    re.compile(r"internal error in subroutine", re.IGNORECASE),
    re.compile(r"EDDDAV:\s*sub-space matrix is not hermitian", re.IGNORECASE),
    re.compile(r"Error EDDDAV", re.IGNORECASE),
    re.compile(r"LAPACK:\s*Routine.*returned error", re.IGNORECASE),
]


def parse_incar_value(incar_path: Path, key: str) -> str | None:
    """Read a single parameter value from INCAR file.

    Handles comment lines (#, !) and inline comments.
    Returns None if key not found or file doesn't exist.
    """
    if not incar_path.is_file():
        return None
    try:
        text = incar_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    for line in text.splitlines():
        line = line.split("#")[0].split("!")[0].strip()
        if not line:
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip().upper() == key.upper():
            return v.strip()
    return None


def parse_oszicar(oszicar_path: Path) -> dict:
    """Parse OSZICAR for ionic steps, electronic steps, and energy.

    Returns dict with keys:
        ionic_steps: int
        electronic_steps_last: int  (electronic iterations in last ionic step)
        final_energy: float | None  (E0 from last ionic step)
    """
    result = {"ionic_steps": 0, "electronic_steps_last": 0, "final_energy": None}

    if not oszicar_path.is_file():
        return result

    try:
        text = oszicar_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result

    lines = text.strip().splitlines()
    if not lines:
        return result

    # Ionic step lines start with a number (e.g., "   1 F= -.23456E+02 E0= ...")
    # Electronic step lines start with spaces then "N:" or "DAV:" or "RMM:" etc.
    ionic_step_pattern = re.compile(r"^\s*(\d+)\s+F=\s*([^\s]+)\s+E0=\s*([^\s]+)")
    electronic_line_pattern = re.compile(r"^\s*(DAV|RMM|CG|N):")

    ionic_count = 0
    last_e0 = None
    electronic_count = 0
    in_current_ionic = True  # track electronic steps for the latest ionic step

    for line in lines:
        ionic_match = ionic_step_pattern.match(line)
        if ionic_match:
            ionic_count = int(ionic_match.group(1))
            try:
                last_e0 = float(ionic_match.group(3))
            except ValueError:
                pass
            electronic_count = 0  # reset for next ionic step
            in_current_ionic = True
        elif electronic_line_pattern.match(line) and in_current_ionic:
            electronic_count += 1

    # For single-point (NSW=0), there's no ionic step line — count electronic lines directly
    if ionic_count == 0:
        electronic_count = sum(1 for line in lines if electronic_line_pattern.match(line))
        # Try to get energy from last line that has E0
        for line in reversed(lines):
            # SCF single-point: last line may be "   1 F= ... E0= ..."
            m = ionic_step_pattern.match(line)
            if m:
                ionic_count = 1
                try:
                    last_e0 = float(m.group(3))
                except ValueError:
                    pass
                break

    result["ionic_steps"] = ionic_count
    result["electronic_steps_last"] = electronic_count
    result["final_energy"] = last_e0
    return result


def read_selective_dynamics_mask(calc_dir: Path) -> list[bool] | None:
    """Return per-atom movable flags from CONTCAR/POSCAR, or None if absent."""
    for name in ("CONTCAR", "POSCAR"):
        path = calc_dir / name
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if len(lines) < 9:
            continue

        try:
            counts = [int(value) for value in lines[6].split()]
        except ValueError:
            continue

        coord_idx = 7
        if not lines[coord_idx].strip().lower().startswith("s"):
            return None
        coord_idx += 1
        if coord_idx >= len(lines):
            continue
        coord_idx += 1

        atom_count = sum(counts)
        atom_lines = lines[coord_idx : coord_idx + atom_count]
        if len(atom_lines) != atom_count:
            continue

        mask: list[bool] = []
        for line in atom_lines:
            parts = line.split()
            if len(parts) < 6:
                return None
            flags = [part.upper().startswith("T") for part in parts[3:6]]
            mask.append(any(flags))
        return mask
    return None


def max_force_from_block(force_block: str, movable_mask: list[bool] | None = None) -> float | None:
    forces: list[float] = []
    for line in force_block.strip().splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            fx, fy, fz = float(parts[3]), float(parts[4]), float(parts[5])
        except (ValueError, IndexError):
            continue
        forces.append((fx**2 + fy**2 + fz**2) ** 0.5)

    if not forces:
        return None

    if movable_mask is not None and len(movable_mask) == len(forces):
        movable_forces = [force for force, movable in zip(forces, movable_mask, strict=True) if movable]
        if movable_forces:
            forces = movable_forces

    return max(forces)


def parse_outcar(outcar_path: Path) -> dict:
    """Parse OUTCAR for convergence indicators and errors.

    Returns dict with keys:
        reached_accuracy: bool   ("reached required accuracy" found)
        nelm: int | None         (NELM value used in calculation)
        max_force: float | None  (maximum force on atoms, eV/A)
        errors: list[str]        (fatal error messages found)
        total_cpu_time: float | None
    """
    result: dict = {
        "reached_accuracy": False,
        "nelm": None,
        "max_force": None,
        "errors": [],
        "total_cpu_time": None,
    }

    if not outcar_path.is_file():
        return result

    try:
        text = outcar_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    # Check for "reached required accuracy" — ionic convergence marker
    if "reached required accuracy" in text:
        result["reached_accuracy"] = True

    # Extract NELM
    nelm_match = re.search(r"NELM\s*=\s*(\d+)", text)
    if nelm_match:
        result["nelm"] = int(nelm_match.group(1))

    # Extract maximum force (last occurrence of "TOTAL-FORCE" block)
    # Format: "total drift: ..." preceded by force components
    # We look for the last "RMS" or parse individual forces
    force_pattern = re.compile(r"TOTAL-FORCE \(eV/Angst\)\s*\n\s*-+\n((?:\s*-?\d+\.\d+\s+.*\n)+)")
    force_blocks = force_pattern.findall(text)
    if force_blocks:
        max_f = max_force_from_block(
            force_blocks[-1],
            read_selective_dynamics_mask(outcar_path.parent),
        )
        if max_f is not None:
            result["max_force"] = max_f

    # Check for fatal errors
    for pattern in FATAL_ERROR_PATTERNS:
        match = pattern.search(text)
        if match:
            result["errors"].append(match.group(0).strip())

    # Extract CPU time
    cpu_match = re.search(r"Total CPU time used \(sec\):\s*([0-9.]+)", text)
    if cpu_match:
        try:
            result["total_cpu_time"] = float(cpu_match.group(1))
        except ValueError:
            pass

    return result


@dataclass
class OscillationResult:
    """Result of mid-run oscillation analysis for a RUNNING VASP job."""

    oscillating: bool = False  # energy sign-alternates for >= min_alternations steps
    stagnating: bool = False  # |dE| < threshold over window but not converged
    ionic_steps_seen: int = 0  # how many ionic steps extracted from tail
    energies: list[float] = field(default_factory=list)  # last N energies
    details: str = ""  # human-readable explanation


@dataclass
class LiveProgress:
    """Lightweight progress snapshot for a RUNNING VASP job."""

    ionic_steps: int = 0
    electronic_steps_last: int = 0
    final_energy: float | None = None
    max_force: float | None = None
    force_convergence_target: float | None = None


def read_tail_text(path: Path, max_bytes: int = 131072) -> str:
    """Read the tail of a potentially large text file."""
    if not path.is_file():
        return ""

    try:
        file_size = path.stat().st_size
        with open(path, encoding="utf-8", errors="replace") as handle:
            if file_size > max_bytes:
                handle.seek(file_size - max_bytes)
                handle.readline()  # discard a possible partial line
            return handle.read()
    except OSError:
        return ""


def extract_last_force_block_max(outcar_path: Path) -> float | None:
    """Extract the latest maximum force from the tail of OUTCAR."""
    tail = read_tail_text(outcar_path)
    if not tail:
        return None

    force_pattern = re.compile(r"TOTAL-FORCE \(eV/Angst\)\s*\n\s*-+\n((?:\s*-?\d+\.\d+\s+.*\n)+)")
    force_blocks = force_pattern.findall(tail)
    if not force_blocks:
        return None

    last_block = force_blocks[-1]
    return max_force_from_block(
        last_block,
        read_selective_dynamics_mask(outcar_path.parent),
    )


def read_live_progress(subdir: Path) -> LiveProgress:
    """Read a lightweight live-progress snapshot for a running VASP job."""
    oszicar_data = parse_oszicar(subdir / "OSZICAR")
    return LiveProgress(
        ionic_steps=oszicar_data["ionic_steps"],
        electronic_steps_last=oszicar_data["electronic_steps_last"],
        final_energy=oszicar_data["final_energy"],
        max_force=extract_last_force_block_max(subdir / "OUTCAR"),
        force_convergence_target=parse_force_convergence_target(subdir / "INCAR"),
    )


def parse_force_convergence_target(incar_path: Path) -> float | None:
    """Parse the force-convergence target from EDIFFG when it is force-based."""
    raw_value = parse_incar_value(incar_path, "EDIFFG")
    if not raw_value:
        return None

    try:
        value = float(raw_value)
    except ValueError:
        return None

    # In VASP, EDIFFG < 0 means force criterion in eV/A.
    if value < 0:
        return abs(value)
    return None


def check_oscillation(
    subdir: Path,
    window: int = 10,
    min_alternations: int = 6,
    stagnation_threshold: float = 0.001,
    stagnation_min_steps: int = 20,
) -> OscillationResult:
    """Lightweight oscillation check on a RUNNING job's OSZICAR.

    Reads only the last 4KB of OSZICAR to avoid heavy I/O on large files.
    Detects two pathological patterns:
    1. Energy oscillation: dE sign alternates for >= min_alternations consecutive steps
    2. Energy stagnation: all |dE| < threshold but step count exceeds stagnation_min_steps

    Args:
        subdir: Path to the VASP calculation directory.
        window: Number of recent ionic steps to analyze.
        min_alternations: Minimum consecutive sign alternations to flag oscillation.
        stagnation_threshold: Energy change threshold (eV) below which steps are "flat".
        stagnation_min_steps: Only flag stagnation if total ionic steps exceed this.
    """
    oszicar_path = subdir / "OSZICAR"
    if not oszicar_path.is_file():
        return OscillationResult()

    # Tail-read: only last 4KB to stay lightweight
    try:
        file_size = oszicar_path.stat().st_size
        with open(oszicar_path, encoding="utf-8", errors="replace") as f:
            if file_size > 4096:
                f.seek(file_size - 4096)
                f.readline()  # discard partial first line
            tail = f.read()
    except OSError:
        return OscillationResult()

    # Extract ionic step energies (E0) from tail
    ionic_step_pattern = re.compile(r"^\s*(\d+)\s+F=\s*[^\s]+\s+E0=\s*([^\s]+)", re.MULTILINE)
    matches = ionic_step_pattern.findall(tail)
    if len(matches) < 3:
        return OscillationResult(ionic_steps_seen=len(matches))

    # Take the last `window` steps
    recent = matches[-window:]
    steps = []
    energies = []
    for step_str, e0_str in recent:
        try:
            steps.append(int(step_str))
            energies.append(float(e0_str))
        except ValueError:
            continue

    if len(energies) < 3:
        return OscillationResult(ionic_steps_seen=len(matches))

    total_ionic_steps = int(matches[-1][0]) if matches else 0

    # Compute dE series
    dE = [energies[i] - energies[i - 1] for i in range(1, len(energies))]

    # Check oscillation: count consecutive sign alternations
    max_alternations = 0
    current_alternations = 0
    for i in range(1, len(dE)):
        if dE[i] * dE[i - 1] < 0:  # sign changed
            current_alternations += 1
            max_alternations = max(max_alternations, current_alternations)
        else:
            current_alternations = 0

    oscillating = max_alternations >= min_alternations

    # Check stagnation: all |dE| below threshold and enough steps elapsed
    all_flat = all(abs(d) < stagnation_threshold for d in dE)
    stagnating = all_flat and total_ionic_steps > stagnation_min_steps

    # Build details string
    details_parts = []
    if oscillating:
        details_parts.append(
            f"Energy oscillation: dE alternated in sign over the latest {max_alternations + 1} steps "
            f"(E range {min(energies):.4f} to {max(energies):.4f} eV)"
        )
    if stagnating:
        max_dE = max(abs(d) for d in dE)
        details_parts.append(
            f"Energy stagnation: |dE| < {stagnation_threshold} eV over the latest {len(dE)} steps "
            f"(max |dE| = {max_dE:.6f} eV), after {total_ionic_steps} ionic steps"
        )

    return OscillationResult(
        oscillating=oscillating,
        stagnating=stagnating,
        ionic_steps_seen=total_ionic_steps,
        energies=energies,
        details="; ".join(details_parts),
    )


def check_vasp_convergence(subdir: Path) -> ConvergenceResult:
    """Check VASP convergence for a calculation directory.

    Reads OSZICAR, OUTCAR, and INCAR to produce a comprehensive
    convergence assessment.

    Args:
        subdir: Path to the VASP calculation directory.

    Returns:
        ConvergenceResult with all convergence indicators.
    """
    errors: list[str] = []

    oszicar_path = subdir / "OSZICAR"
    outcar_path = subdir / "OUTCAR"
    incar_path = subdir / "INCAR"
    contcar_path = subdir / "CONTCAR"

    # Parse OSZICAR
    oszicar_data = parse_oszicar(oszicar_path)
    ionic_steps = oszicar_data["ionic_steps"]
    electronic_steps_last = oszicar_data["electronic_steps_last"]
    final_energy = oszicar_data["final_energy"]

    # Parse OUTCAR
    outcar_data = parse_outcar(outcar_path)
    errors.extend(outcar_data["errors"])

    # Get NELM and NSW from INCAR (with defaults matching VASP)
    nelm_str = parse_incar_value(incar_path, "NELM")
    nelm = (
        int(nelm_str)
        if nelm_str and nelm_str.isdigit()
        else (
            outcar_data["nelm"] if outcar_data["nelm"] else 400  # project default
        )
    )

    nsw_str = parse_incar_value(incar_path, "NSW")
    nsw = int(nsw_str) if nsw_str and nsw_str.isdigit() else 0

    # Determine electronic convergence: last ionic step must use fewer than NELM steps
    reached_nelm = electronic_steps_last >= nelm

    # Determine ionic convergence
    reached_nsw = (nsw > 0) and (ionic_steps >= nsw)

    # Check file integrity
    if not oszicar_path.is_file():
        errors.append("OSZICAR not found")
    if not outcar_path.is_file():
        errors.append("OUTCAR not found")
    if not contcar_path.is_file() or (contcar_path.is_file() and contcar_path.stat().st_size == 0):
        errors.append("CONTCAR missing or empty")

    # Overall convergence determination
    converged = True

    if reached_nelm:
        converged = False
        errors.append(f"SCF not converged: electronic steps ({electronic_steps_last}) reached NELM ({nelm})")

    if nsw > 0:
        # Relaxation job: need "reached required accuracy" in OUTCAR
        if not outcar_data["reached_accuracy"]:
            converged = False
            if reached_nsw:
                errors.append(f"Ionic relaxation incomplete: reached NSW ({nsw}) without convergence")
            elif not reached_nelm:
                errors.append("Ionic relaxation did not reach required accuracy")
    else:
        # Single-point (SCF): only electronic convergence matters
        if not reached_nelm and ionic_steps == 0 and not outcar_path.is_file():
            converged = False

    if outcar_data["errors"]:
        converged = False

    return ConvergenceResult(
        converged=converged,
        ionic_steps=ionic_steps,
        electronic_steps_last=electronic_steps_last,
        final_energy=final_energy,
        reached_nelm=reached_nelm,
        reached_nsw=reached_nsw,
        max_force=outcar_data["max_force"],
        errors=errors,
    )


def parse_total_magnetization(oszicar_path: Path) -> float | None:
    """Extract total magnetization from the last ionic step in OSZICAR.

    Returns None for non-spin-polarized calculations (no mag= field).
    """
    if not oszicar_path.is_file():
        return None
    try:
        text = oszicar_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    mag_pattern = re.compile(r"mag=\s*([-+]?[0-9]*\.?[0-9]+)")
    last_mag = None
    for line in text.splitlines():
        m = mag_pattern.search(line)
        if m:
            try:
                last_mag = float(m.group(1))
            except ValueError:
                pass
    return last_mag


def parse_site_magnetizations(
    outcar_path: Path,
    threshold: float = 0.1,
    species_list: list[str] | None = None,
) -> list[dict] | None:
    """Extract per-atom magnetizations from VASP output.

    Tries two sources in order:
    1. OUTCAR "magnetization (x)" table (requires LORBIT >= 10)
    2. vasprun.xml projected magnetization (requires LORBIT >= 10)

    Returns a list of dicts for ions with |tot| > threshold.
    Returns None if no per-site data is available (e.g. LORBIT < 10).
    """
    result = parse_site_mag_from_outcar(outcar_path, threshold, species_list)
    if result is not None:
        return result

    # Fallback: try vasprun.xml in the same directory
    vasprun_path = outcar_path.parent / "vasprun.xml"
    return parse_site_mag_from_vasprun(vasprun_path, threshold, species_list)


def parse_site_mag_from_outcar(
    outcar_path: Path,
    threshold: float = 0.1,
    species_list: list[str] | None = None,
) -> list[dict] | None:
    """Parse per-atom magnetizations from the last 'magnetization (x)' table in OUTCAR."""
    if not outcar_path.is_file():
        return None
    try:
        text = outcar_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    marker = "magnetization (x)"
    pos = text.rfind(marker)
    if pos == -1:
        return None

    block = text[pos:]
    lines = block.splitlines()

    results: list[dict] = []
    data_started = False
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            if data_started:
                break
            continue
        if stripped.startswith("# of ion") or stripped.startswith("---"):
            data_started = True
            continue
        if not data_started:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            break
        try:
            ion_index = int(parts[0])
            tot = float(parts[-1])
        except (ValueError, IndexError):
            break
        if abs(tot) > threshold:
            entry: dict = {"index": ion_index, "tot": round(tot, 4)}
            if species_list and 0 < ion_index <= len(species_list):
                entry["atom"] = species_list[ion_index - 1]
            results.append(entry)

    return results


def parse_site_mag_from_vasprun(
    vasprun_path: Path,
    threshold: float = 0.1,
    species_list: list[str] | None = None,
) -> list[dict] | None:
    """Parse per-atom magnetizations from vasprun.xml via pymatgen."""
    if not vasprun_path.is_file():
        return None
    try:
        from pymatgen.io.vasp import Vasprun

        vr = Vasprun(str(vasprun_path), parse_dos=False, parse_eigen=False, parse_potcar_file=False)
        if not vr.is_spin:
            return None

        # pymatgen exposes atomic_symbols from vasprun; use as fallback species list
        if species_list is None:
            species_list = vr.atomic_symbols

        # Attempt to get per-site magnetization from the last ionic step's charge data
        # vasprun.xml stores this only when LORBIT >= 10
        last_step = vr.ionic_steps[-1]
        mag_data = last_step.get("magmoms")

        if mag_data is not None:
            results: list[dict] = []
            for i, mag in enumerate(mag_data):
                m = float(mag)
                if abs(m) > threshold:
                    entry: dict = {"index": i + 1, "tot": round(m, 4)}
                    if species_list and i < len(species_list):
                        entry["atom"] = species_list[i]
                    results.append(entry)
            return results

        return None
    except Exception:
        return None
