import fcntl
import re
import shlex
import subprocess
from collections.abc import Sequence
from datetime import UTC
from fnmatch import fnmatch
from math import isfinite
from pathlib import Path
from typing import Any, ClassVar

from openhands.sdk import Action, Observation, TextContent, ToolDefinition
from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from pydantic import Field

from src.utils.config import load_config, validate_submission_resources

SBATCH_TEMPLATE = """#!/bin/bash
#SBATCH -J {{JOB_NAME}}
#SBATCH -p {{PARTITION}}
#SBATCH -N 1
#SBATCH --ntasks-per-node={{TOTAL_CPUS}}

# HPC environment initialization
{{MODULE_SETUP}}
{{LD_LIBRARY_PATH_EXPORT}}

ulimit -s unlimited

# OMP thread configuration
export OMP_NUM_THREADS={{OMP_THREADS}}

# Launch VASP
{{VASP_COMMAND}}
"""


def split_config_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def hpc_config() -> dict:
    defaults = {
        "slurm_partition": "intel",
        "module_purge": True,
        "module_loads": "openmpi/5.0.7, vasp/6.3.2",
        "submit_module_purge": True,
        "submit_module_loads": "openmpi/5.0.7, vasp/6.3.2",
        "sbatch_export": "ALL",
        "ld_library_path": "",
        "vasp_command": "mpirun -n {{TOTAL_CPUS}} vasp_std",
    }
    section = load_config().get("hpc", {})
    if isinstance(section, dict):
        defaults.update({key: value for key, value in section.items() if value is not None})

    import os

    env_partition = os.getenv("VASP_DEFAULT_PARTITION", "").strip()
    if env_partition:
        defaults["slurm_partition"] = env_partition
    return defaults


def module_setup_lines(config: dict) -> str:
    lines: list[str] = []
    if bool(config.get("module_purge", True)):
        lines.append("module purge")
    lines.extend(f"module load {name}" for name in split_config_list(config.get("module_loads")))
    return "\n".join(lines) if lines else "# No module setup configured."


def ld_library_path_export(config: dict) -> str:
    value = str(config.get("ld_library_path") or "").strip()
    if not value:
        return "# No extra LD_LIBRARY_PATH configured."
    return f"export LD_LIBRARY_PATH={value}:$LD_LIBRARY_PATH"


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds as human-readable string."""
    total = int(seconds)
    h, r = divmod(total, 3600)
    m, s = divmod(r, 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def apply_authoritative_job_results(
    snapshot: dict,
    authoritative: dict[str, str],
) -> dict:
    """Collapse physical attempts into stable logical calculation results."""
    raw_results = snapshot.get("results")
    if not authoritative or not isinstance(raw_results, dict):
        return snapshot

    attempt_results = dict(raw_results)
    collapsed_attempts: set[str] = set()
    for logical, physical in authoritative.items():
        collapsed_attempts.update({logical, physical})
        collapsed_attempts.update(subdir for subdir in attempt_results if subdir.startswith(f"{logical}_recovery"))
    logical_results = {subdir: state for subdir, state in attempt_results.items() if subdir not in collapsed_attempts}
    for logical, physical in authoritative.items():
        logical_results[logical] = attempt_results.get(physical, "SUBMITTED")

    terminal = {"CONVERGED", "NOT_CONVERGED", "FAILED", "CANCELLED", "STALLED"}
    failures = {"NOT_CONVERGED", "FAILED", "CANCELLED", "STALLED"}
    snapshot["attempt_results"] = attempt_results
    snapshot["results"] = logical_results
    snapshot["job_count"] = len(logical_results)
    snapshot["all_terminal"] = bool(logical_results) and all(state in terminal for state in logical_results.values())
    snapshot["all_converged"] = bool(logical_results) and all(
        state == "CONVERGED" for state in logical_results.values()
    )
    snapshot["has_failure"] = any(state in failures for state in logical_results.values())
    return snapshot


def build_completion_assessment(
    snapshot: dict,
    calc_summary: dict[str, Any],
    authoritative: dict[str, str],
    policy: dict[str, Any],
    *,
    node_id: str,
) -> dict[str, Any]:
    """Assess node-objective readiness without weakening strict job status."""
    profile = str(policy.get("profile") or "strict")
    patterns = policy.get("partial_candidate_node_patterns")
    partial_allowed = (
        profile == "demonstration"
        and bool(policy.get("allow_partial_candidate_success"))
        and isinstance(patterns, list)
        and any(fnmatch(node_id, str(pattern)) for pattern in patterns)
    )
    strict_satisfied = bool(snapshot.get("all_terminal") and snapshot.get("all_converged"))
    assessment: dict[str, Any] = {
        "profile": profile,
        "node_id": node_id,
        "partial_candidate_policy_applied": partial_allowed,
        "strict_all_converged": strict_satisfied,
        "objective_satisfied": strict_satisfied,
        "usable_calculations": [],
        "excluded_calculations": [],
        "requires_agent_sanity_review": ["structure", "magnetization"],
    }
    if not partial_allowed or not snapshot.get("all_terminal"):
        return assessment

    max_force = float(policy.get("max_force_eVA", 0.10))
    strict_force = float(policy.get("strict_force_eVA", 0.05))
    min_bond = float(policy.get("min_bond_length_A", 0.60))
    results = snapshot.get("results")
    if not isinstance(results, dict):
        return assessment

    terminal_states = {"CONVERGED", "NOT_CONVERGED", "FAILED", "CANCELLED", "STALLED"}
    for logical_subdir, state in results.items():
        physical_subdir = authoritative.get(logical_subdir, logical_subdir)
        entry = calc_summary.get(physical_subdir)
        if not isinstance(entry, dict):
            entry = calc_summary.get(logical_subdir)
        entry = entry if isinstance(entry, dict) else {}

        quality = None
        reasons: list[str] = []
        force = entry.get("max_force_eVA")
        force_is_finite = not isinstance(force, bool) and isinstance(force, (int, float)) and isfinite(float(force))
        if state in terminal_states:
            energy = entry.get("final_energy_eV")
            bond = entry.get("min_bond_length_A")
            errors = entry.get("errors")
            errors = errors if isinstance(errors, list) else []
            non_ionic_errors = [str(error) for error in errors if "ionic relaxation" not in str(error).lower()]
            if isinstance(energy, bool) or not isinstance(energy, (int, float)) or not isfinite(float(energy)):
                reasons.append("missing finite final energy")
            if not force_is_finite:
                reasons.append("missing finite maximum force")
            elif float(force) > max_force:
                reasons.append(f"Fmax {float(force):.4f} exceeds demonstration limit {max_force:.4f} eV/A")
            if entry.get("reached_nelm") is True:
                reasons.append("electronic iteration reached NELM")
            if non_ionic_errors:
                reasons.append("non-ionic convergence error: " + non_ionic_errors[0])
            if policy.get("require_structure_health"):
                if isinstance(bond, bool) or not isinstance(bond, (int, float)) or not isfinite(float(bond)):
                    reasons.append("missing structure-health bond evidence")
                elif float(bond) < min_bond:
                    reasons.append(f"minimum bond length {float(bond):.3f} A is below {min_bond:.3f} A")
            if not reasons:
                quality = (
                    "strictly_converged"
                    if state == "CONVERGED" and entry.get("converged") is True and float(force) <= strict_force
                    else "demonstration_accepted"
                )
        else:
            reasons.append(f"job state {state} is not terminal")

        payload = {
            "logical_subdir": logical_subdir,
            "physical_subdir": physical_subdir,
            "job_state": state,
        }
        if quality:
            payload["quality_tier"] = quality
            payload["final_energy_eV"] = entry.get("final_energy_eV")
            payload["max_force_eVA"] = entry.get("max_force_eVA")
            payload["acceptance_basis"] = [
                "terminal calculation with finite energy and force evidence",
                (
                    f"Fmax <= {strict_force:.4f} eV/A strict limit"
                    if quality == "strictly_converged"
                    else f"Fmax <= {max_force:.4f} eV/A demonstration limit"
                ),
                f"minimum bond length >= {min_bond:.3f} A",
                "no reached-NELM or non-ionic convergence error",
            ]
            assessment["usable_calculations"].append(payload)
        else:
            payload["reasons"] = reasons or ["calculation is not usable"]
            assessment["excluded_calculations"].append(payload)

    minimum = max(int(policy.get("min_usable_calculations", 1)), 1)
    assessment["minimum_usable_calculations"] = minimum
    assessment["objective_satisfied"] = len(assessment["usable_calculations"]) >= minimum
    return assessment


class MonitorSubmittedJobsAction(Action):
    workspace_path: str | None = Field(
        default=None,
        description="Absolute workspace path; defaults to current runner workspace when omitted.",
    )
    subdirs: list[str] | None = Field(
        default=None,
        description="Monitor only specified subdirectories; empty means monitor all submitted jobs.",
    )
    max_poll_seconds: int = Field(
        default=7200,
        description="Maximum polling duration per call in seconds (default: 2 h); returns current status when time is reached.",
    )


class MonitorSubmittedJobsObservation(Observation):
    summary_text: str = Field(default="")
    snapshot: dict = Field(default_factory=dict)

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=f"Job-status inspection result:\n{self.summary_text}")]


class MonitorSubmittedJobsExecutor(ToolExecutor[MonitorSubmittedJobsAction, MonitorSubmittedJobsObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: MonitorSubmittedJobsAction, conversation=None) -> MonitorSubmittedJobsObservation:
        try:
            workspace_path = action.workspace_path or self.workspace_dir
            return self.poll_loop(workspace_path, action)
        except Exception as e:
            return MonitorSubmittedJobsObservation(
                summary_text=f"Job-status inspection failed: {str(e)}",
                snapshot={},
            )

    def poll_loop(
        self,
        workspace_path: str,
        action: MonitorSubmittedJobsAction,
    ) -> MonitorSubmittedJobsObservation:
        """Poll until all jobs reach terminal states or max_poll_seconds is reached."""
        import time as time

        from src.monitor.job_monitor import JobMonitor, JobState
        from src.utils.ui import (
            Colors,
            print_info,
            print_monitor_header,
            print_monitor_summary,
        )

        start = time.monotonic()
        deadline = start + action.max_poll_seconds
        poll_count = 0

        print_monitor_header()
        print_info(f"Monitoring started (max polling time per call: {format_elapsed(action.max_poll_seconds)})")

        while True:
            poll_count += 1
            elapsed = time.monotonic() - start

            monitor = JobMonitor(workspace_path)
            state = monitor.initialize()
            if state.jobs:
                state = monitor.check_all(
                    state,
                    allow_stall_cancel=True,
                    subdirs=action.subdirs,
                )

            if action.subdirs:
                requested = set(action.subdirs)
                state.jobs = {k: v for k, v in state.jobs.items() if k in requested}

            if not state.jobs:
                return self.build_observation(state, elapsed, poll_count, "no_jobs")

            elapsed_str = format_elapsed(elapsed)
            now_str = time.strftime("%Y-%m-%d %H:%M:%S")
            sep = "-" * 60
            print(f"\n{Colors.YELLOW}{sep}{Colors.END}")
            print(f"{Colors.YELLOW}Monitoring cycle #{poll_count} | elapsed: {elapsed_str} | {now_str}{Colors.END}")
            print(f"{Colors.YELLOW}{sep}{Colors.END}")
            print(monitor.get_status_summary(state))

            all_terminal = all(j.state.is_terminal for j in state.jobs.values())
            if any(j.state == JobState.STALLED for j in state.jobs.values()):
                return self.build_observation(state, elapsed, poll_count, "recovery_required")
            if all_terminal:
                return self.build_observation(state, elapsed, poll_count, "all_terminal")

            if time.monotonic() >= deadline:
                return self.build_observation(state, elapsed, poll_count, "check_in")

            active = [j for j in state.jobs.values() if not j.state.is_terminal]
            interval = min(30 if j.recovery_cancel_reason else j.poll_interval for j in active) if active else 300
            interval = max(interval, 30)
            remaining = deadline - time.monotonic()
            interval = min(interval, remaining)
            if interval <= 0:
                return self.build_observation(state, elapsed, poll_count, "check_in")

            print_monitor_summary(elapsed, int(interval))
            time.sleep(interval)

    def build_observation(
        self,
        state,
        elapsed_sec: float,
        poll_count: int,
        reason: str,
    ) -> MonitorSubmittedJobsObservation:
        """Build a compact observation from MonitorState."""
        from src.monitor.job_monitor import JobState
        from src.utils.ui import print_info, print_success, print_warning

        elapsed_str = format_elapsed(elapsed_sec)
        all_terminal = bool(state.jobs) and all(j.state.is_terminal for j in state.jobs.values())
        all_converged = bool(state.jobs) and all(j.state == JobState.CONVERGED for j in state.jobs.values())
        has_failure = any(
            j.state in {JobState.FAILED, JobState.CANCELLED, JobState.NOT_CONVERGED, JobState.STALLED}
            for j in state.jobs.values()
        )
        still_running = any(not j.state.is_terminal for j in state.jobs.values())

        lines: list[str] = []
        if reason == "no_jobs":
            lines.append("No .job_id files were found.")
        elif reason == "recovery_required":
            lines.append("Monitoring stopped early because a job requires convergence recovery")
        elif still_running:
            lines.append(f"Scheduled check-in (elapsed {elapsed_str}, {poll_count} poll(s), jobs still running)")
        else:
            lines.append(f"Monitoring completed (elapsed {elapsed_str}, {poll_count} poll(s))")

        lines.append(
            f"all_terminal={all_terminal}  all_converged={all_converged}  "
            f"has_failure={has_failure}  still_running={still_running}"
        )
        lines.append("")

        for subdir, job in sorted(state.jobs.items()):
            parts = [f"{subdir}: {job.state.value}"]
            if job.final_energy is not None:
                parts.append(f"E={job.final_energy:.4f}")
            if job.max_force is not None:
                force_str = f"Fmax={job.max_force:.4f}"
                if job.force_convergence_target is not None:
                    force_str += f"(target<={job.force_convergence_target:.4f})"
                parts.append(force_str)
            if job.ionic_steps is not None and job.ionic_steps > 0:
                parts.append(f"ionic={job.ionic_steps}")
            if job.electronic_steps_last is not None and job.electronic_steps_last > 0:
                parts.append(f"elec={job.electronic_steps_last}")
            if (
                job.ionic_steps is not None
                and job.min_energy_ionic_step is not None
                and job.ionic_steps > job.min_energy_ionic_step
            ):
                stale = job.ionic_steps - job.min_energy_ionic_step
                parts.append(f"stale={stale}")
            if job.time_used:
                time_str = f"time={job.time_used}"
                if job.time_limit:
                    time_str += f"/{job.time_limit}"
                parts.append(time_str)
            if job.error_message and job.state != JobState.CONVERGED:
                parts.append(f"err={job.error_message[:80]}")
            if job.oscillation_detected:
                parts.append("OSCILLATING")
            lines.append("  ".join(parts))

        if still_running and reason != "recovery_required":
            lines.append("")
            lines.append("Some jobs are still incomplete. Reinvoke MonitorSubmittedJobs to continue monitoring.")

        if reason == "recovery_required":
            print_warning(
                "A stalled job requires bounded convergence recovery; returning control to CalculationRunner."
            )
        elif reason == "all_terminal":
            if all_converged:
                print_success(f"All {len(state.jobs)} job(s) converged successfully.")
            elif has_failure:
                print_warning("At least one job failed or did not converge.")
        elif still_running:
            active_count = sum(1 for j in state.jobs.values() if not j.state.is_terminal)
            print_info(
                f"Scheduled check-in: {active_count} job(s) still running; returning control to CalculationRunner."
            )

        snapshot = {
            "workspace": str(state.workspace),
            "job_count": len(state.jobs),
            "all_terminal": all_terminal,
            "all_converged": all_converged,
            "has_failure": has_failure,
            "still_running": still_running,
            "poll_count": poll_count,
            "elapsed_seconds": int(elapsed_sec),
            "results": {sd: j.state.value for sd, j in state.jobs.items()},
        }

        return MonitorSubmittedJobsObservation(
            summary_text="\n".join(lines),
            snapshot=snapshot,
        )


class MonitorSubmittedJobsTool(ToolDefinition[MonitorSubmittedJobsAction, MonitorSubmittedJobsObservation]):
    """Monitor submitted VASP jobs in a workspace."""

    name: ClassVar[str] = "MonitorSubmittedJobs"
    DESCRIPTION: ClassVar[str] = (
        "Monitor submitted VASP jobs with internal polling. "
        "Prints real-time progress to terminal and returns a condensed status summary."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=MonitorSubmittedJobsAction,
                observation_type=MonitorSubmittedJobsObservation,
                executor=MonitorSubmittedJobsExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class SubmitVaspJobAction(Action):
    working_dir: str = Field(
        description="Absolute calculation subdirectory path, or a path relative to the current runner workspace.",
    )
    job_name: str | None = Field(
        default=None,
        description="SLURM job name used when rendering vasp.sbatch from the template; defaults to the directory name.",
    )
    total_cpus: int = Field(
        default=8,
        description="Total CPU cores requested in vasp.sbatch when rendering from the template.",
    )
    num_dcus: int = Field(
        default=1,
        description="Compatibility field retained for existing parameter summaries; this CPU partition does not request DCU resources.",
    )
    omp_threads: int | None = Field(
        default=None,
        description="OMP_NUM_THREADS when rendering from the template; defaults to 1 for the Intel CPU partition.",
    )
    sbatch_filename: str = Field(
        default="vasp.sbatch",
        description="Submission script filename inside working_dir.",
    )
    render_sbatch_if_missing: bool = Field(
        default=True,
        description="Render vasp.sbatch from SubmitVaspJob's built-in template if it is missing.",
    )
    force_render_sbatch: bool = Field(
        default=False,
        description="Overwrite vasp.sbatch from SubmitVaspJob's built-in template before submission.",
    )


class SubmitVaspJobObservation(Observation):
    submitted: bool = Field(default=False)
    job_id: str | None = Field(default=None)
    working_dir: str = Field(default="")
    sbatch_path: str = Field(default="")
    job_id_path: str = Field(default="")
    message: str = Field(default="")
    stdout: str = Field(default="")
    stderr: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=f"VASP job submission result:\n{self.message}")]


def resolve_working_dir(workspace_dir: str, working_dir: str) -> Path:
    path = Path(working_dir)
    if not path.is_absolute():
        path = Path(workspace_dir) / path
    return path.resolve()


def render_sbatch_template(
    *,
    working_dir: Path,
    sbatch_path: Path,
    job_name: str,
    total_cpus: int,
    num_dcus: int,
    omp_threads: int,
) -> None:
    text = SBATCH_TEMPLATE
    hpc = hpc_config()
    replacements = {
        "{{JOB_NAME}}": job_name,
        "{{PARTITION}}": str(hpc["slurm_partition"]),
        "{{TOTAL_CPUS}}": str(total_cpus),
        "{{OMP_THREADS}}": str(omp_threads),
        "{{MODULE_SETUP}}": module_setup_lines(hpc),
        "{{LD_LIBRARY_PATH_EXPORT}}": ld_library_path_export(hpc),
        "{{VASP_COMMAND}}": str(hpc["vasp_command"]),
    }
    for _ in range(2):
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
    unresolved = sorted(set(re.findall(r"\{\{[^{}]+\}\}", text)))
    if unresolved:
        raise ValueError(f"Unresolved sbatch template placeholder(s): {', '.join(unresolved)}")
    sbatch_path.write_text(text, encoding="utf-8")


class SubmitVaspJobExecutor(ToolExecutor[SubmitVaspJobAction, SubmitVaspJobObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: SubmitVaspJobAction, conversation: Any = None) -> SubmitVaspJobObservation:
        working_dir = resolve_working_dir(self.workspace_dir, action.working_dir)
        if not working_dir.is_dir():
            return SubmitVaspJobObservation(message=f"Submission directory does not exist: {working_dir}")
        with (working_dir / ".submission.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self.submit(action)

    def submit(self, action: SubmitVaspJobAction) -> SubmitVaspJobObservation:
        from src.tools.repair_tools import validate_repair_submission

        try:
            working_dir = resolve_working_dir(self.workspace_dir, action.working_dir)
            sbatch_path = working_dir / action.sbatch_filename
            job_id_path = working_dir / ".job_id"
            base_obs = {
                "working_dir": str(working_dir),
                "sbatch_path": str(sbatch_path),
                "job_id_path": str(job_id_path),
            }

            if not working_dir.is_dir():
                return SubmitVaspJobObservation(
                    **base_obs,
                    message=f"Submission failed: working_dir does not exist: {working_dir}",
                )

            if job_id_path.is_file():
                existing_job_id = job_id_path.read_text(encoding="utf-8").strip()
                if existing_job_id:
                    return SubmitVaspJobObservation(
                        **base_obs,
                        submitted=False,
                        job_id=existing_job_id,
                        message=(
                            f"Existing .job_id found ({existing_job_id}); skipped duplicate submission. "
                            "Use a new Coordinator-prepared attempt instead of overwriting submitted evidence."
                        ),
                    )

            pending_path = working_dir / ".submission_pending"
            if pending_path.exists():
                raise ValueError("Previous submission outcome is unknown; reconcile with Slurm before retrying.")
            validate_repair_submission(working_dir)

            missing_inputs = [
                name
                for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR")
                if not (working_dir / name).is_file() or (working_dir / name).stat().st_size == 0
            ]
            if missing_inputs:
                return SubmitVaspJobObservation(
                    **base_obs,
                    message="Submission failed: missing or empty required input file(s): " + ", ".join(missing_inputs),
                )

            num_dcus = max(int(action.num_dcus), 1)
            total_cpus = int(action.total_cpus)
            omp_threads = int(action.omp_threads) if action.omp_threads is not None else 1
            validate_submission_resources(total_cpus, omp_threads)
            job_name = (action.job_name or working_dir.name).strip() or working_dir.name

            if action.force_render_sbatch or (action.render_sbatch_if_missing and not sbatch_path.is_file()):
                render_sbatch_template(
                    working_dir=working_dir,
                    sbatch_path=sbatch_path,
                    job_name=job_name,
                    total_cpus=total_cpus,
                    num_dcus=num_dcus,
                    omp_threads=omp_threads,
                )

            if not sbatch_path.is_file() or sbatch_path.stat().st_size == 0:
                return SubmitVaspJobObservation(
                    **base_obs,
                    message=f"Submission failed: missing or empty sbatch script: {sbatch_path}",
                )
            sbatch_text = sbatch_path.read_text(encoding="utf-8", errors="replace")
            unresolved = sorted(set(re.findall(r"\{\{[^{}]+\}\}", sbatch_text)))
            if unresolved:
                return SubmitVaspJobObservation(
                    **base_obs,
                    message="Submission failed: unresolved sbatch placeholder(s): " + ", ".join(unresolved),
                )

            hpc = hpc_config()
            submit_module_loads = split_config_list(hpc.get("submit_module_loads"))
            sbatch_export = str(hpc.get("sbatch_export") or "ALL").strip()
            sbatch_args = ["sbatch"]
            if sbatch_export:
                sbatch_args.append(f"--export={sbatch_export}")
            sbatch_args.append(sbatch_path.name)

            pending_path.write_text("Submission requested; preserve until Slurm outcome is reconciled.\n")
            if submit_module_loads:
                setup_lines = ["set -e", "source /etc/profile.d/modules.sh"]
                if bool(hpc.get("submit_module_purge", hpc.get("module_purge", True))):
                    setup_lines.append("module purge")
                setup_lines.extend("module load " + shlex.quote(name) for name in submit_module_loads)
                setup_lines.append(" ".join(shlex.quote(part) for part in sbatch_args))
                result = subprocess.run(
                    ["bash", "-lc", "; ".join(setup_lines)],
                    cwd=str(working_dir),
                    text=True,
                    capture_output=True,
                    check=False,
                )
            else:
                result = subprocess.run(
                    sbatch_args,
                    cwd=str(working_dir),
                    text=True,
                    capture_output=True,
                    check=False,
                )
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            if result.returncode != 0:
                pending_path.unlink()
                return SubmitVaspJobObservation(
                    **base_obs,
                    stdout=stdout,
                    stderr=stderr,
                    message=(
                        f"Submission failed: sbatch exited with code {result.returncode}."
                        + (f"\nstdout: {stdout}" if stdout else "")
                        + (f"\nstderr: {stderr}" if stderr else "")
                    ),
                )

            match = re.search(r"Submitted\s+batch\s+job\s+(\d+)", stdout)
            if not match:
                return SubmitVaspJobObservation(
                    **base_obs,
                    stdout=stdout,
                    stderr=stderr,
                    message=(
                        "Submission failed: sbatch succeeded but no numeric Job ID was found in stdout."
                        + (f"\nstdout: {stdout}" if stdout else "")
                    ),
                )

            job_id = match.group(1)
            job_id_path.write_text(job_id + "\n", encoding="utf-8")
            pending_path.unlink()
            return SubmitVaspJobObservation(
                **base_obs,
                submitted=True,
                job_id=job_id,
                stdout=stdout,
                stderr=stderr,
                message=f"Submitted VASP job {job_id}; .job_id written to {job_id_path}.",
            )

        except Exception as e:
            return SubmitVaspJobObservation(message=f"Submission failed: {e}")


class SubmitVaspJobTool(ToolDefinition[SubmitVaspJobAction, SubmitVaspJobObservation]):
    """Submit a prepared VASP calculation directory through sbatch and write .job_id."""

    name: ClassVar[str] = "SubmitVaspJob"
    DESCRIPTION: ClassVar[str] = (
        "Validate a prepared VASP calculation directory, ensure/render vasp.sbatch from the built-in template, "
        "submit with sbatch, parse the numeric SLURM Job ID, and write .job_id."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=SubmitVaspJobAction,
                observation_type=SubmitVaspJobObservation,
                executor=SubmitVaspJobExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


class ExtractCalcResultsAction(Action):
    workspace_path: str | None = Field(
        default=None,
        description="Absolute node-directory path; defaults to current runner workspace when omitted.",
    )
    subdirs: list[str] | None = Field(
        default=None,
        description="Extract results only for specified subdirectories; empty means all subdirectories containing .job_id.",
    )


class ExtractCalcResultsObservation(Observation):
    summary_text: str = Field(default="")

    @property
    def to_llm_content(self) -> Sequence[TextContent]:
        return [TextContent(text=f"Calculation-result extraction report:\n{self.summary_text}")]


class ExtractCalcResultsExecutor(ToolExecutor[ExtractCalcResultsAction, ExtractCalcResultsObservation]):
    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    def __call__(self, action: ExtractCalcResultsAction, conversation=None) -> ExtractCalcResultsObservation:
        from datetime import datetime

        from src.monitor.convergence import (
            check_vasp_convergence,
            parse_site_magnetizations,
            parse_total_magnetization,
        )
        from src.monitor.service import inspect_workspace_jobs
        from src.utils.results import (
            RUNNER_RESULT_FILENAME,
            read_authoritative_calculations,
            read_completion_policy,
            read_pipeline_context,
            read_runner_result,
            update_runner_result,
        )

        try:
            ws = Path(action.workspace_path or self.workspace_dir)
            if not ws.is_dir():
                return ExtractCalcResultsObservation(summary_text=f"Directory does not exist: {ws}")

            _, job_snapshot = inspect_workspace_jobs(str(ws))
            authoritative = read_authoritative_calculations(ws)
            apply_authoritative_job_results(job_snapshot, authoritative)
            job_snapshot["timestamp"] = datetime.now(UTC).isoformat()

            all_subdirs: list[str] = []
            for jf in sorted(ws.rglob(".job_id")):
                rel = jf.parent.relative_to(ws).as_posix()
                all_subdirs.append(rel if rel != "." else ".")
            if action.subdirs:
                requested = set(action.subdirs)
                all_subdirs = [s for s in all_subdirs if s in requested]

            if not all_subdirs:
                update_runner_result(ws, job_status=job_snapshot)
                return ExtractCalcResultsObservation(summary_text="No subdirectory containing .job_id was found.")

            runner_result = read_runner_result(ws) or {}
            existing = runner_result.get("calc_summary")
            if not isinstance(existing, dict):
                existing = {}

            lines: list[str] = []
            for subdir in all_subdirs:
                try:
                    subdir_path = ws / subdir if subdir != "." else ws
                    entry = self.extract_one(
                        subdir_path,
                        check_vasp_convergence,
                        parse_total_magnetization,
                        parse_site_magnetizations,
                    )
                    existing[subdir] = entry
                    lines.append(self.format_line(subdir, entry))
                except Exception as e:
                    existing[subdir] = {"converged": False, "errors": [str(e)]}
                    lines.append(f"{subdir}: ERROR  {e}")

            context = read_pipeline_context(str(ws)) or {}
            completion_assessment = build_completion_assessment(
                job_snapshot,
                existing,
                authoritative,
                read_completion_policy(ws),
                node_id=str(context.get("node_id") or ws.name),
            )
            update_runner_result(
                ws,
                job_status=job_snapshot,
                calc_summary=existing,
                completion_assessment=completion_assessment,
            )
            header = (
                f"Captured job status and extracted calculation results for {len(all_subdirs)} subdirectory(ies) "
                f"-> {RUNNER_RESULT_FILENAME}:job_status and calc_summary"
            )
            return ExtractCalcResultsObservation(summary_text=header + "\n\n" + "\n".join(lines))

        except Exception as e:
            return ExtractCalcResultsObservation(summary_text=f"Calculation-result extraction failed: {e}")

    @staticmethod
    def extract_one(subdir_path: Path, check_conv, parse_mag, parse_site_mag) -> dict:
        from src.monitor.convergence import parse_incar_value

        conv = check_conv(subdir_path)
        entry: dict = {
            "converged": conv.converged,
            "final_energy_eV": conv.final_energy,
            "max_force_eVA": conv.max_force,
            "ionic_steps": conv.ionic_steps,
            "electronic_steps_last": conv.electronic_steps_last,
            "reached_nelm": conv.reached_nelm,
            "reached_nsw": conv.reached_nsw,
            "errors": conv.errors,
        }

        ediffg_raw = parse_incar_value(subdir_path / "INCAR", "EDIFFG")
        try:
            ediffg = float(ediffg_raw) if ediffg_raw is not None else None
        except (TypeError, ValueError):
            ediffg = None
        entry["force_target_eVA"] = abs(ediffg) if ediffg is not None and ediffg < 0 else None

        entry["total_magnetization"] = parse_mag(subdir_path / "OSZICAR")

        struct = None
        contcar = subdir_path / "CONTCAR"
        poscar = subdir_path / "POSCAR"
        struct_file = contcar if (contcar.is_file() and contcar.stat().st_size > 0) else poscar
        if struct_file.is_file():
            try:
                from pymatgen.core import Structure

                struct = Structure.from_file(str(struct_file))
            except Exception:
                pass

        species_list = [site.specie.symbol for site in struct] if struct else None
        entry["site_magnetizations"] = parse_site_mag(
            subdir_path / "OUTCAR",
            threshold=0.1,
            species_list=species_list,
        )

        total_mag = entry["total_magnetization"]
        if entry["site_magnetizations"] is None and total_mag is not None and abs(total_mag) > 1.0:
            lorbit_raw = parse_incar_value(subdir_path / "INCAR", "LORBIT")
            lorbit_val = int(lorbit_raw) if lorbit_raw and lorbit_raw.isdigit() else 0
            if lorbit_val < 10:
                entry["site_magnetizations_note"] = (
                    f"LORBIT={lorbit_val} is too low for per-site magnetization decomposition. "
                    f"Set LORBIT=11 in INCAR to enable site-projected magnetic moments."
                )

        min_bond = None
        if struct and len(struct) >= 2:
            try:
                dm = struct.distance_matrix
                min_bond = round(
                    min(float(dm[i][j]) for i in range(len(dm)) for j in range(i + 1, len(dm))),
                    4,
                )
            except Exception:
                pass
        entry["min_bond_length_A"] = min_bond

        return entry

    @staticmethod
    def format_line(subdir: str, entry: dict) -> str:
        status = "CONVERGED" if entry.get("converged") else "NOT_CONVERGED"
        parts = [f"{subdir}: {status}"]
        e = entry.get("final_energy_eV")
        if e is not None:
            parts.append(f"E={e:.4f}")
        f = entry.get("max_force_eVA")
        if f is not None:
            parts.append(f"Fmax={f:.4f}")
        ionic = entry.get("ionic_steps")
        if ionic:
            parts.append(f"ionic={ionic}")
        mag = entry.get("total_magnetization")
        if mag is not None:
            parts.append(f"mag={mag:.2f}")
        bond = entry.get("min_bond_length_A")
        if bond is not None:
            parts.append(f"min_bond={bond:.3f}A")
        errs = entry.get("errors")
        if errs:
            parts.append(f"err={errs[0][:60]}")
        note = entry.get("site_magnetizations_note")
        if note:
            parts.append(note)
        return "  ".join(parts)


class ExtractCalcResultsTool(ToolDefinition[ExtractCalcResultsAction, ExtractCalcResultsObservation]):
    """Extract key results from completed VASP jobs and update runner_result.json."""

    name: ClassVar[str] = "ExtractCalcResults"
    DESCRIPTION: ClassVar[str] = (
        "Capture job status and extract key results (convergence, energy, forces, magnetization, bond lengths) "
        "from completed VASP jobs, then write job_status and calc_summary in runner_result.json."
    )

    @classmethod
    def create(cls, conv_state) -> Sequence[ToolDefinition]:
        return [
            cls(
                description=cls.DESCRIPTION,
                action_type=ExtractCalcResultsAction,
                observation_type=ExtractCalcResultsObservation,
                executor=ExtractCalcResultsExecutor(workspace_dir=conv_state.workspace.working_dir),
            )
        ]


register_tool("SubmitVaspJob", SubmitVaspJobTool)
register_tool("MonitorSubmittedJobs", MonitorSubmittedJobsTool)
register_tool("ExtractCalcResults", ExtractCalcResultsTool)


def get_calculation_runner_tools_config() -> list[Tool]:
    """Job-monitoring and result-extraction tools used after submission."""
    return [
        Tool(name="SubmitVaspJob"),
        Tool(name="MonitorSubmittedJobs"),
        Tool(name="ExtractCalcResults"),
    ]
