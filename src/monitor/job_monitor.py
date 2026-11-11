"""Track VASP Slurm jobs through scheduler and scientific terminal states."""

from __future__ import annotations

import enum
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.monitor.convergence import check_vasp_convergence, read_live_progress
from src.utils.results import write_json

STAGNATION_STALE_IONIC_STEPS = 50


class JobState(enum.Enum):
    """Lifecycle states for a monitored SLURM job."""

    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CONVERGED = "CONVERGED"
    NOT_CONVERGED = "NOT_CONVERGED"
    STALLED = "STALLED"

    @property
    def is_terminal(self) -> bool:
        return self in (
            JobState.CONVERGED,
            JobState.NOT_CONVERGED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.STALLED,
        )


def estimate_poll_interval(subdir_path: Path) -> int:
    """Estimate poll interval from POSCAR atom count in the subdirectory.

    Mirrors the thresholds in SystemComplexityEvaluatorTool so that the
    monitor produces identical timing without depending on ParamWorker output.
    """
    poscar = subdir_path / "POSCAR"
    if not poscar.is_file():
        return 300  # conservative default

    try:
        # Lightweight atom-count extraction: read the 7th line of POSCAR
        # (ion counts line) to avoid importing pymatgen.
        lines = poscar.read_text(encoding="utf-8").splitlines()
        if len(lines) < 7:
            return 300
        counts_line = lines[6] if not lines[5][0].isdigit() else lines[5]
        num_atoms = sum(int(x) for x in counts_line.split())
    except (ValueError, IndexError, OSError):
        return 300

    if num_atoms <= 20:
        return 120
    elif num_atoms <= 40:
        return 180
    elif num_atoms <= 100:
        return 300
    else:
        return 600


@dataclass
class MonitoredJob:
    """State for a single monitored VASP job."""

    subdir: str
    job_id: str
    state: JobState
    poll_interval: int = 300
    slurm_exit_code: int | None = None
    converged: bool | None = None
    ionic_steps: int | None = None
    electronic_steps_last: int | None = None
    final_energy: float | None = None
    max_force: float | None = None
    force_convergence_target: float | None = None
    min_energy: float | None = None
    min_energy_ionic_step: int | None = None
    error_message: str | None = None
    oscillation_detected: bool = False
    oscillation_details: str | None = None
    recovery_cancel_reason: str | None = None
    time_used: str | None = None
    time_limit: str | None = None
    scheduler_missing_count: int = 0
    last_checked: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MonitoredJob:
        d = dict(d)
        d["state"] = JobState(d["state"])
        # Forward-compatible: ignore unknown keys, use defaults for missing ones
        import dataclasses

        known_fields = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return cls(**filtered)


@dataclass
class MonitorState:
    """Full monitor state for a workspace, persisted to JSON."""

    workspace: str
    jobs: dict[str, MonitoredJob]  # key = subdir relative path
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "jobs": {k: v.to_dict() for k, v in self.jobs.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MonitorState:
        jobs = {k: MonitoredJob.from_dict(v) for k, v in d["jobs"].items()}
        return cls(
            workspace=d["workspace"],
            jobs=jobs,
            created_at=d["created_at"],
            updated_at=d["updated_at"],
        )


# SLURM status mapping
# sacct State → JobState mapping
SACCT_STATE_MAP = {
    "COMPLETED": JobState.COMPLETED,
    "FAILED": JobState.FAILED,
    "TIMEOUT": JobState.FAILED,
    "CANCELLED": JobState.CANCELLED,
    "CANCELLED+": JobState.CANCELLED,
    "OUT_OF_MEMORY": JobState.FAILED,
    "NODE_FAIL": JobState.FAILED,
    "PREEMPTED": JobState.FAILED,
    "RUNNING": JobState.RUNNING,
    "PENDING": JobState.SUBMITTED,
    "REQUEUED": JobState.SUBMITTED,
    "SUSPENDED": JobState.SUBMITTED,
}

# squeue State → JobState mapping
SQUEUE_STATE_MAP = {
    "RUNNING": JobState.RUNNING,
    "PENDING": JobState.SUBMITTED,
    "SUSPENDED": JobState.SUBMITTED,
    "COMPLETING": JobState.RUNNING,
    "CONFIGURING": JobState.SUBMITTED,
}


class JobMonitor:
    """Monitor VASP Slurm jobs with single-pass convergence checks."""

    def __init__(self, workspace_path: str):
        self.workspace = Path(workspace_path)
        self.state_path = self.workspace / ".monitor_state.json"

    def initialize(self) -> MonitorState:
        """Build MonitorState by scanning .job_id files and parsing dependencies."""
        now = datetime.now(UTC).isoformat()
        jobs: dict[str, MonitoredJob] = {}
        previous = self.load_state()

        # Scan all .job_id files
        for jf in sorted(self.workspace.rglob(".job_id")):
            if not jf.is_file():
                continue
            job_id = jf.read_text(encoding="utf-8").strip()
            if not job_id:
                continue
            subdir = str(jf.parent.relative_to(self.workspace))
            previous_job = previous.jobs.get(subdir) if previous else None
            if previous_job and previous_job.job_id == job_id:
                previous_job.poll_interval = estimate_poll_interval(jf.parent)
                jobs[subdir] = previous_job
            else:
                jobs[subdir] = MonitoredJob(
                    subdir=subdir,
                    job_id=job_id,
                    state=JobState.SUBMITTED,
                    poll_interval=estimate_poll_interval(jf.parent),
                    last_checked=now,
                )

        state = MonitorState(
            workspace=str(self.workspace),
            jobs=jobs,
            created_at=previous.created_at if previous else now,
            updated_at=now,
        )
        return state

    def load_state(self) -> MonitorState | None:
        if not self.state_path.is_file():
            return None
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return MonitorState.from_dict(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
            return None

    def save_state(self, state: MonitorState) -> None:
        write_json(self.state_path, state.to_dict())

    def check_slurm_status(self, job_id: str) -> tuple[JobState | None, int | None, str | None, str | None]:
        """Query SLURM for job status using three-level fallback.

        1. sacct (can see finished jobs)
        2. squeue (only sees queued/running jobs, also returns time info)
        3. File detection (OUTCAR existence as last resort)

        Returns (JobState, exit_code, time_used, time_limit).
        """
        # Level 1: sacct
        state, exit_code = self.query_sacct(job_id)
        if state is not None:
            return state, exit_code, None, None

        # Level 2: squeue
        state, time_used, time_limit = self.query_squeue(job_id)
        if state is not None:
            return state, None, time_used, time_limit

        # Level 3: unknown — caller should handle via file detection
        return None, None, None, None

    def query_sacct(self, job_id: str) -> tuple[JobState | None, int | None]:
        """Query sacct for job state and exit code.

        sacct returns multiple lines per job (main job + sub-steps like .batch, .extern).
        We use --format=JobID to identify the main job line (no '.' suffix) and ignore sub-steps.
        """
        try:
            result = subprocess.run(
                ["sacct", "-j", job_id, "--format=JobID,State,ExitCode", "-n", "-P", "--noconvert"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None, None

            for line in result.stdout.strip().splitlines():
                parts = line.split("|")
                if len(parts) >= 3:
                    sacct_job_id = parts[0].strip()
                    # Skip sub-step lines like "12345.batch", "12345.extern"
                    if "." in sacct_job_id:
                        continue
                    raw_state = parts[1].strip().split()[0].rstrip("+")
                    exit_str = parts[2].strip().split(":")[0]  # "0:0" → "0"
                    state = SACCT_STATE_MAP.get(raw_state)
                    exit_code = int(exit_str) if exit_str.isdigit() else None
                    if state is not None:
                        return state, exit_code
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None, None

    def query_squeue(self, job_id: str) -> tuple[JobState | None, str | None, str | None]:
        """Query squeue for active job state and time info."""
        try:
            result = subprocess.run(
                ["squeue", "-j", job_id, "-h", "-o", "%T|%M|%l"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None, None, None

            raw = result.stdout.strip()
            if not raw:
                return None, None, None
            parts = raw.splitlines()[0].split("|")
            state = SQUEUE_STATE_MAP.get(parts[0].strip()) if parts else None
            time_used = parts[1].strip() if len(parts) > 1 else None
            time_limit = parts[2].strip() if len(parts) > 2 else None
            return state, time_used, time_limit
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
        return None, None, None

    def check_file_completion(self, subdir_path: Path) -> bool:
        """Last-resort check: OUTCAR exists and contains finalization markers."""
        outcar = subdir_path / "OUTCAR"
        if not outcar.is_file():
            return False
        try:
            # Read last 4KB to check for completion markers
            size = outcar.stat().st_size
            if size == 0:
                return False
            with open(outcar, encoding="utf-8", errors="replace") as f:
                f.seek(max(0, size - 4096))
                tail = f.read()
            return "Voluntary context switches" in tail or "General timing" in tail
        except OSError:
            return False

    def check_file_stalled(self, subdir_path: Path, poll_interval: int) -> bool:
        """Check if VASP output files have stopped updating for 2x poll_interval.

        If both OSZICAR and OUTCAR exist but neither has been modified within
        the threshold, the job is likely dead (cancelled/killed) without SLURM
        having reported it.
        """
        threshold = poll_interval * 2
        now = time.time()
        candidates = [subdir_path / "OSZICAR", subdir_path / "OUTCAR"]
        latest_mtime = 0.0
        found_any = False
        for f in candidates:
            try:
                mtime = f.stat().st_mtime
                latest_mtime = max(latest_mtime, mtime)
                found_any = True
            except OSError:
                continue
        if not found_any:
            return False
        return (now - latest_mtime) > threshold

    def check_all(
        self,
        state: MonitorState,
        *,
        persist: bool = True,
        allow_stall_cancel: bool = False,
        subdirs: list[str] | None = None,
    ) -> MonitorState:
        """Run one check cycle: update SLURM status + convergence for all active jobs."""
        requested = set(subdirs or [])
        active = [
            sd
            for sd, job in state.jobs.items()
            if not job.state.is_terminal and (not requested or sd in requested or sd == ".")
        ]
        return self.check_subset(
            state,
            active,
            persist=persist,
            allow_stall_cancel=allow_stall_cancel,
        )

    def check_subset(
        self,
        state: MonitorState,
        subdirs: list[str],
        *,
        persist: bool = True,
        allow_stall_cancel: bool = False,
    ) -> MonitorState:
        """Run a check cycle on a specific subset of jobs."""
        now = datetime.now(UTC).isoformat()

        for subdir in subdirs:
            job = state.jobs.get(subdir)
            if job is None or job.state.is_terminal:
                continue

            subdir_path = self.workspace / subdir

            slurm_state, exit_code, time_used, time_limit = self.check_slurm_status(job.job_id)
            if time_used:
                job.time_used = time_used
            if time_limit:
                job.time_limit = time_limit

            if slurm_state is None:
                if job.recovery_cancel_reason:
                    if self.check_scheduler_disappeared(job):
                        job.state = JobState.STALLED
                        job.error_message = (
                            f"{job.recovery_cancel_reason}; CatPaw cancellation confirmed "
                            "after the job left the scheduler"
                        )
                    else:
                        job.scheduler_missing_count += 1
                    job.last_checked = now
                    continue
                elif self.check_file_completion(subdir_path):
                    slurm_state = JobState.COMPLETED
                    exit_code = 0
                    job.scheduler_missing_count = 0
                elif job.state in {JobState.RUNNING, JobState.SUBMITTED} and self.check_scheduler_disappeared(job):
                    job.state = JobState.CANCELLED
                    job.error_message = (
                        "SLURM returned no sacct record and squeue no longer lists the job for consecutive checks; "
                        "the job was likely cancelled or terminated before normal completion."
                    )
                    job.last_checked = now
                    continue
                elif job.state == JobState.RUNNING and self.check_file_stalled(subdir_path, job.poll_interval):
                    job.state = JobState.STALLED
                    job.error_message = (
                        f"SLURM returned no status and output files were not updated for over {job.poll_interval * 2}s; "
                        "the job may have been cancelled or terminated abnormally"
                    )
                    job.last_checked = now
                    continue
                else:
                    job.scheduler_missing_count += 1

            if slurm_state is not None:
                job.scheduler_missing_count = 0
                if exit_code is not None:
                    job.slurm_exit_code = exit_code

                if slurm_state == JobState.RUNNING:
                    job.state = JobState.RUNNING
                    live = read_live_progress(subdir_path)
                    if live.ionic_steps > 0:
                        job.ionic_steps = live.ionic_steps
                    if live.electronic_steps_last > 0:
                        job.electronic_steps_last = live.electronic_steps_last
                    if live.final_energy is not None:
                        job.final_energy = live.final_energy
                        if job.min_energy is None or live.final_energy < job.min_energy:
                            job.min_energy = live.final_energy
                            job.min_energy_ionic_step = job.ionic_steps
                    if live.max_force is not None:
                        job.max_force = live.max_force
                    if live.force_convergence_target is not None:
                        job.force_convergence_target = live.force_convergence_target
                    from src.monitor.convergence import check_oscillation

                    osc = check_oscillation(subdir_path)
                    job.oscillation_detected = osc.oscillating or osc.stagnating
                    job.oscillation_details = osc.details if job.oscillation_detected else None
                    if allow_stall_cancel and not job.recovery_cancel_reason:
                        reason = self.running_recovery_reason(job, subdir_path)
                        if reason:
                            self.request_stall_cancel(job, reason)
                elif slurm_state == JobState.SUBMITTED:
                    job.state = JobState.SUBMITTED
                elif slurm_state == JobState.FAILED:
                    if job.recovery_cancel_reason:
                        job.state = JobState.STALLED
                        job.error_message = (
                            f"{job.recovery_cancel_reason}; CatPaw cancellation confirmed "
                            f"with SLURM exit code {exit_code}"
                        )
                    else:
                        job.state = JobState.FAILED
                        job.error_message = f"SLURM job failed (exit code: {exit_code})"
                elif slurm_state == JobState.CANCELLED:
                    if job.recovery_cancel_reason:
                        job.state = JobState.STALLED
                        job.error_message = f"{job.recovery_cancel_reason}; CatPaw cancellation confirmed by SLURM"
                    else:
                        job.state = JobState.CANCELLED
                        job.error_message = "SLURM job was cancelled (scancel or scheduler cancellation)"
                elif slurm_state == JobState.COMPLETED:
                    job.recovery_cancel_reason = None
                    conv = check_vasp_convergence(subdir_path)
                    job.converged = conv.converged
                    job.ionic_steps = conv.ionic_steps
                    job.electronic_steps_last = conv.electronic_steps_last
                    job.final_energy = conv.final_energy
                    job.max_force = conv.max_force
                    if job.force_convergence_target is None:
                        from src.monitor.convergence import parse_force_convergence_target

                        job.force_convergence_target = parse_force_convergence_target(subdir_path / "INCAR")
                    if conv.final_energy is not None and (job.min_energy is None or conv.final_energy < job.min_energy):
                        job.min_energy = conv.final_energy
                        job.min_energy_ionic_step = conv.ionic_steps

                    if conv.converged:
                        job.state = JobState.CONVERGED
                    else:
                        job.state = JobState.NOT_CONVERGED
                        job.error_message = "; ".join(conv.errors) if conv.errors else "Did not converge"

            job.last_checked = now

        state.updated_at = now
        if persist:
            self.save_state(state)
        return state

    @staticmethod
    def running_recovery_reason(job: MonitoredJob, subdir_path: Path) -> str | None:
        """Return deterministic live non-convergence evidence, if any."""
        from src.monitor.convergence import parse_incar_value

        nelm_raw = parse_incar_value(subdir_path / "INCAR", "NELM")
        nelm = int(nelm_raw) if nelm_raw and nelm_raw.isdigit() else 400
        if job.electronic_steps_last is not None and job.electronic_steps_last >= nelm:
            return f"Electronic non-convergence: electronic steps ({job.electronic_steps_last}) reached NELM ({nelm})"

        if (
            job.ionic_steps is None
            or job.min_energy_ionic_step is None
            or job.max_force is None
            or job.force_convergence_target is None
        ):
            return None
        stale_steps = job.ionic_steps - job.min_energy_ionic_step
        if (
            stale_steps >= STAGNATION_STALE_IONIC_STEPS
            and job.oscillation_detected
            and job.max_force > job.force_convergence_target
        ):
            return (
                f"Ionic stagnation: no new minimum energy for {stale_steps} ionic steps, "
                f"Fmax={job.max_force:.4f} eV/A remains above "
                f"{job.force_convergence_target:.4f} eV/A"
            )
        return None

    @staticmethod
    def request_stall_cancel(job: MonitoredJob, reason: str) -> bool:
        """Request Slurm cancellation while preserving recovery intent."""
        try:
            result = subprocess.run(
                ["scancel", job.job_id],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            job.error_message = f"{reason}; failed to invoke scancel: {exc}"
            return False
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            job.error_message = f"{reason}; scancel failed: {detail}"
            return False
        job.recovery_cancel_reason = reason
        job.error_message = f"{reason}; CatPaw requested scancel and is awaiting confirmation"
        return True

    @staticmethod
    def check_scheduler_disappeared(job: MonitoredJob) -> bool:
        """Treat repeated absence from both sacct and squeue as abnormal termination.

        A single missing scheduler response can be transient, especially just
        after submission or on clusters with delayed accounting. Two consecutive
        misses after a known submitted/running state are enough to stop reporting
        the job as active.
        """
        return job.scheduler_missing_count >= 1

    def get_status_summary(self, state: MonitorState) -> str:
        """Generate human-readable status summary for terminal output."""
        return "\n".join(self.format_job_line(job) for job in state.jobs.values())

    @staticmethod
    def format_job_line(job: MonitoredJob) -> str:
        """Format a single job status line."""
        icon = {
            JobState.SUBMITTED: "  ⏳",
            JobState.RUNNING: "  🔄",
            JobState.COMPLETED: "  ✔️",
            JobState.CONVERGED: "  ✅",
            JobState.NOT_CONVERGED: "  ⚠️",
            JobState.FAILED: "  ❌",
            JobState.CANCELLED: "  🛑",
            JobState.STALLED: "  💤",
        }.get(job.state, "  ❓")

        header_parts = [f"Job {job.job_id}", job.state.value, f"poll {job.poll_interval}s"]
        if job.time_used and job.time_limit:
            header_parts.append(f"time {job.time_used}/{job.time_limit}")
        elif job.time_used:
            header_parts.append(f"time {job.time_used}")
        lines = [f"{icon} {job.subdir + '/':<22s} {'  '.join(header_parts)}"]

        progress_parts: list[str] = []
        if job.ionic_steps is not None and job.ionic_steps > 0:
            progress_parts.append(f"ionic_step={job.ionic_steps}")
        if job.electronic_steps_last is not None and job.electronic_steps_last > 0:
            progress_parts.append(f"elec_step={job.electronic_steps_last}")
        if job.final_energy is not None:
            progress_parts.append(f"E={job.final_energy:.4f} eV")
        if job.max_force is not None:
            progress_parts.append(f"max_F={job.max_force:.4f} eV/A")
        if job.force_convergence_target is not None:
            progress_parts.append(f"target_F<={job.force_convergence_target:.4f}")
        if (
            job.state == JobState.RUNNING
            and job.max_force is not None
            and job.force_convergence_target is not None
            and job.max_force <= job.force_convergence_target
        ):
            progress_parts.append("(force criterion satisfied; likely converging stress/lattice)")
        if progress_parts:
            lines.append("    " + "  ".join(progress_parts))

        trend_parts: list[str] = []
        if job.min_energy is not None:
            min_e = f"min_E={job.min_energy:.4f} eV"
            if job.min_energy_ionic_step is not None and job.min_energy_ionic_step > 0:
                min_e = f"{min_e} (ionic_step {job.min_energy_ionic_step})"
            trend_parts.append(min_e)
        if (
            job.state == JobState.RUNNING
            and job.ionic_steps is not None
            and job.min_energy_ionic_step is not None
            and job.ionic_steps >= job.min_energy_ionic_step
        ):
            trend_parts.append(f"stale_steps={job.ionic_steps - job.min_energy_ionic_step}")
        if trend_parts:
            lines.append("    " + "  ".join(trend_parts))

        if job.error_message and job.state in (
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.NOT_CONVERGED,
            JobState.STALLED,
        ):
            lines.append(f"    error: {job.error_message}")
        if job.oscillation_detected and job.oscillation_details:
            lines.append(f"    warn: {job.oscillation_details}")

        return "\n".join(lines)
