"""Bounded, read-only VASP progress for the selected dashboard calculation."""

import re
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from typing import Any

from src.monitor.convergence import extract_last_force_block_max, read_tail_text
from src.utils.results import read_json_file

TAIL_BYTES = 131072
HISTORY_POINTS = 120


def iteration_history(text: str) -> dict[str, list[dict[str, int | float]]]:
    ionic, electronic = [], []
    for line in text.splitlines():
        ion = re.match(r"^\s*(\d+)\s+F=\s*(\S+)\s+E0=\s*(\S+)", line)
        electron = re.match(r"^\s*(?:DAV|RMM|CG|N|DMP):\s*(\d+)\s+(\S+)\s+(\S+)", line)
        match = ion or electron
        if not match:
            continue
        try:
            step = int(match[1])
            energy = float(match[3 if ion else 2].replace("D", "E"))
            delta = None if ion else float(match[3].replace("D", "E"))
        except ValueError:
            continue
        if not isfinite(energy) or (delta is not None and not isfinite(delta)):
            continue
        history = ionic if ion else electronic
        if history and step <= history[-1]["step"]:
            history.clear()
        point = {"step": step, "energy": energy}
        if delta is not None:
            point["delta"] = delta
        history.append(point)
    return {"ionic": ionic[-HISTORY_POINTS:], "electronic": electronic[-HISTORY_POINTS:]}


def calculation_progress(node_dir: Path, subdir: str) -> dict[str, Any]:
    root = node_dir.resolve()
    directory = (root / subdir).resolve()
    if not directory.is_relative_to(root) or any(part.startswith(".") for part in directory.relative_to(root).parts):
        raise ValueError("Calculation must remain inside the selected node.")
    if not directory.is_dir():
        raise FileNotFoundError("Calculation directory not found")
    marker = directory / ".job_id"
    if not marker.resolve().is_relative_to(root):
        raise ValueError("Job marker points outside the selected node.")
    job_id = marker.read_text().strip() if marker.is_file() else None
    if job_id is not None and not job_id.isdigit():
        raise ValueError("Invalid job ID")
    files = {name: directory / name for name in ("OSZICAR", "OUTCAR", "POSCAR", "CONTCAR")}
    if job_id:
        files[f"slurm-{job_id}.out"] = directory / f"slurm-{job_id}.out"
    for path in files.values():
        if not path.resolve().is_relative_to(root):
            raise ValueError("Calculation output points outside the selected node.")
    output_files = [path for name, path in files.items() if name not in {"POSCAR", "CONTCAR"} and path.is_file()]
    histories = []
    for path in output_files:
        if path.name == "OUTCAR":
            continue
        history = iteration_history(read_tail_text(path, TAIL_BYTES))
        if history["ionic"] or history["electronic"]:
            histories.append((path.stat().st_mtime, path.name, history))
    source, history = None, {"ionic": [], "electronic": []}
    if histories:
        _, source, history = max(histories, key=lambda item: item[0])
    runner = read_json_file(root / "runner_result.json") or {}
    monitor = read_json_file(root / ".monitor_state.json") or {}
    canonical = directory.relative_to(root).as_posix()
    jobs = [job for job in runner.get("job_status", {}).get("jobs", []) if job["subdir"] == canonical]
    if canonical in monitor.get("jobs", {}):
        jobs.append(monitor["jobs"][canonical])
    job = max(jobs, key=lambda item: item.get("last_checked") or "", default={})
    log = files.get(f"slurm-{job_id}.out")
    updated = max((path.stat().st_mtime for path in output_files), default=None)
    force = extract_last_force_block_max(directory / "OUTCAR")
    return {
        "job_id": job_id,
        "job": job,
        "sampled_at": datetime.now(UTC).isoformat(),
        "output_updated_at": datetime.fromtimestamp(updated, UTC).isoformat() if updated is not None else None,
        "source": source,
        "history": history,
        "max_force": force if force is not None and isfinite(force) else None,
        "log_name": log.name if log and log.is_file() else None,
        "log_tail": "\n".join(read_tail_text(log, 32768).splitlines()[-60:]) if log and log.is_file() else "",
    }
