"""Runtime helpers for single-pass monitor inspection."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.monitor.job_monitor import JobMonitor, JobState, MonitorState
from src.utils.project_env import resolve_workspace_reference
from src.utils.results import update_runner_result
from src.utils.ui import (
    print_error,
    print_info,
    print_monitor_header,
    print_success,
    print_warning,
)


def inspect_workspace_jobs(
    workspace_path: str,
    *,
    persist: bool = True,
) -> tuple[MonitorState, dict[str, Any]]:
    """Inspect all submitted jobs once and return a structured snapshot."""
    monitor = JobMonitor(workspace_path)
    state = monitor.initialize()
    if state.jobs:
        state = monitor.check_all(state, persist=persist)

    active_jobs = [job for job in state.jobs.values() if not job.state.is_terminal]
    failed_states = {
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.NOT_CONVERGED,
        JobState.STALLED,
    }
    has_failure = any(job.state in failed_states for job in state.jobs.values())
    all_terminal = bool(state.jobs) and all(job.state.is_terminal for job in state.jobs.values())
    all_converged = bool(state.jobs) and all(job.state == JobState.CONVERGED for job in state.jobs.values())
    recommended_poll_interval = min(job.poll_interval for job in active_jobs) if active_jobs else None

    snapshot = {
        "workspace": str(Path(workspace_path).resolve()),
        "job_count": len(state.jobs),
        "all_terminal": all_terminal,
        "all_converged": all_converged,
        "has_failure": has_failure,
        "recommended_poll_interval": recommended_poll_interval,
        "results": {subdir: job.state.value for subdir, job in state.jobs.items()},
        "jobs": [
            {
                "subdir": subdir,
                "job_id": job.job_id,
                "state": job.state.value,
                "poll_interval": job.poll_interval,
                "slurm_exit_code": job.slurm_exit_code,
                "converged": job.converged,
                "ionic_steps": job.ionic_steps,
                "electronic_steps_last": job.electronic_steps_last,
                "final_energy": job.final_energy,
                "max_force": job.max_force,
                "force_convergence_target": job.force_convergence_target,
                "min_energy": job.min_energy,
                "min_energy_ionic_step": job.min_energy_ionic_step,
                "error_message": job.error_message,
                "oscillation_detected": job.oscillation_detected,
                "oscillation_details": job.oscillation_details,
                "recovery_cancel_reason": job.recovery_cancel_reason,
                "last_checked": job.last_checked,
            }
            for subdir, job in sorted(state.jobs.items())
        ],
    }
    return state, snapshot


def run_monitor_standalone(workspace_path: str) -> dict[str, str]:
    """Single-pass monitor status check for CLI/debug use."""
    print_monitor_header()
    print_info("Scanning .job_id files...")

    state, snapshot = inspect_workspace_jobs(workspace_path)
    monitor = JobMonitor(workspace_path)

    if not state.jobs:
        print_warning("No .job_id files were found in the workspace; no jobs to monitor.")
        return {}

    print_info(f"Detected {len(state.jobs)} jobs to inspect.")

    print(monitor.get_status_summary(state))

    for job in snapshot["jobs"]:
        subdir = job["subdir"]
        state_name = job["state"]
        if state_name == JobState.CONVERGED.value:
            detail = f"E={job['final_energy']:.4f} eV" if isinstance(job["final_energy"], (int, float)) else ""
            print_success(f"  {subdir}: CONVERGED {detail}".rstrip())
        elif state_name == JobState.NOT_CONVERGED.value:
            print_warning(f"  {subdir}: NOT_CONVERGED - {job['error_message']}")
        elif state_name == JobState.CANCELLED.value:
            print_warning(f"  {subdir}: CANCELLED - {job['error_message']}")
        elif state_name == JobState.STALLED.value:
            print_warning(f"  {subdir}: STALLED - {job['error_message']}")
        elif state_name == JobState.FAILED.value:
            print_error(f"  {subdir}: FAILED - {job['error_message']}")

    if snapshot["all_converged"]:
        print_success(f"All {snapshot['job_count']} jobs converged successfully.")
    elif snapshot["has_failure"]:
        converged_count = sum(1 for job in snapshot["jobs"] if job["state"] == JobState.CONVERGED.value)
        failed_count = sum(
            1
            for job in snapshot["jobs"]
            if job["state"]
            in {
                JobState.FAILED.value,
                JobState.CANCELLED.value,
                JobState.NOT_CONVERGED.value,
                JobState.STALLED.value,
            }
        )
        print_warning(f"Summary: {converged_count} converged, {failed_count} failed/not converged.")
    elif snapshot["recommended_poll_interval"] is not None:
        print_info(f"Recommended next polling interval: {snapshot['recommended_poll_interval']} s.")

    return snapshot["results"]


def write_job_status(workspace_path: str, results: dict[str, str]) -> Path:
    """Write the job_status section in runner_result.json."""
    job_status = {
        "workspace": workspace_path,
        "timestamp": datetime.now(UTC).isoformat(),
        "results": results,
        "all_converged": all(v == JobState.CONVERGED.value for v in results.values()) if results else False,
    }
    return update_runner_result(workspace_path, job_status=job_status)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CatPaw monitor single-pass status inspection entry point",
        epilog="Example:\n  python -m src.monitor.service status oer_demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    status_parser = subparsers.add_parser("status", help="Inspect workspace job status once")
    status_parser.add_argument("workspace", help="Workspace name or absolute workspace path")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    workspace_path = resolve_workspace_reference(args.workspace)
    os.environ["VASP_WORKSPACE"] = workspace_path
    assert args.subcommand == "status"
    print_info(f"Current computation workspace: {workspace_path}")
    run_monitor_standalone(workspace_path)


if __name__ == "__main__":
    main()
