from __future__ import annotations

from types import SimpleNamespace

import src.monitor.convergence as convergence
from src.monitor.convergence import LiveProgress, OscillationResult
from src.monitor.job_monitor import JobMonitor, JobState, MonitoredJob, MonitorState
from src.tools.runner_tools import apply_authoritative_job_results


def make_state(tmp_path, *, max_force: float = 0.08, electronic_steps: int = 10):
    calc = tmp_path / "slab_opt"
    calc.mkdir()
    (calc / "INCAR").write_text("NELM = 400\n", encoding="utf-8")
    job = MonitoredJob(
        subdir="slab_opt",
        job_id="123",
        state=JobState.RUNNING,
        ionic_steps=79,
        electronic_steps_last=electronic_steps,
        min_energy=-10.0,
        min_energy_ionic_step=28,
        max_force=max_force,
        force_convergence_target=0.05,
    )
    state = MonitorState(
        workspace=str(tmp_path),
        jobs={"slab_opt": job},
    )
    return JobMonitor(str(tmp_path)), state


def mock_live(monkeypatch, *, max_force: float = 0.08, electronic_steps: int = 10):
    monkeypatch.setattr(
        "src.monitor.job_monitor.read_live_progress",
        lambda _: LiveProgress(
            ionic_steps=80,
            electronic_steps_last=electronic_steps,
            final_energy=-9.5,
            max_force=max_force,
            force_convergence_target=0.05,
        ),
    )
    monkeypatch.setattr(
        convergence,
        "check_oscillation",
        lambda _: OscillationResult(
            stagnating=True,
            ionic_steps_seen=80,
            details="flat recent ionic energies",
        ),
    )


def test_live_ionic_stagnation_requests_cancel_then_becomes_stalled(tmp_path, monkeypatch):
    monitor, state = make_state(tmp_path)
    mock_live(monkeypatch)
    monkeypatch.setattr(
        monitor,
        "check_slurm_status",
        lambda _: (JobState.RUNNING, None, None, None),
    )
    calls = []
    monkeypatch.setattr(
        "src.monitor.job_monitor.subprocess.run",
        lambda args, **_: calls.append(args) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    monitor.check_all(
        state,
        persist=False,
        allow_stall_cancel=True,
    )

    job = state.jobs["slab_opt"]
    assert calls == [["scancel", "123"]]
    assert job.state == JobState.RUNNING
    assert job.recovery_cancel_reason
    assert "52 ionic steps" in job.recovery_cancel_reason

    monkeypatch.setattr(
        monitor,
        "check_slurm_status",
        lambda _: (JobState.CANCELLED, 0, None, None),
    )
    monitor.check_all(
        state,
        persist=False,
        allow_stall_cancel=True,
    )
    assert job.state == JobState.STALLED
    assert "cancellation confirmed" in job.error_message


def test_read_only_check_never_cancels_stagnating_job(tmp_path, monkeypatch):
    monitor, state = make_state(tmp_path)
    mock_live(monkeypatch)
    monkeypatch.setattr(
        monitor,
        "check_slurm_status",
        lambda _: (JobState.RUNNING, None, None, None),
    )
    monkeypatch.setattr(
        "src.monitor.job_monitor.subprocess.run",
        lambda *unused_args, **unused_kwargs: (_ for _ in ()).throw(AssertionError("read-only check attempted scancel")),
    )

    monitor.check_all(state, persist=False)

    assert state.jobs["slab_opt"].state == JobState.RUNNING
    assert state.jobs["slab_opt"].recovery_cancel_reason is None


def test_force_converged_job_is_not_cancelled_for_stale_energy(tmp_path, monkeypatch):
    monitor, state = make_state(tmp_path, max_force=0.04)
    mock_live(monkeypatch, max_force=0.04)
    monkeypatch.setattr(
        monitor,
        "check_slurm_status",
        lambda _: (JobState.RUNNING, None, None, None),
    )
    monkeypatch.setattr(
        "src.monitor.job_monitor.subprocess.run",
        lambda *unused_args, **unused_kwargs: (_ for _ in ()).throw(AssertionError("force-converged job attempted scancel")),
    )

    monitor.check_all(
        state,
        persist=False,
        allow_stall_cancel=True,
    )

    assert state.jobs["slab_opt"].recovery_cancel_reason is None


def test_authoritative_attempt_replaces_failed_physical_attempt():
    snapshot = {
        "results": {
            "slab_opt": "STALLED",
            "slab_opt_recovery1": "CONVERGED",
            "reference": "CONVERGED",
        },
        "job_count": 3,
        "all_terminal": True,
        "all_converged": False,
        "has_failure": True,
    }

    apply_authoritative_job_results(
        snapshot,
        {"slab_opt": "slab_opt_recovery1"},
    )

    assert snapshot["results"] == {
        "slab_opt": "CONVERGED",
        "reference": "CONVERGED",
    }
    assert snapshot["attempt_results"]["slab_opt"] == "STALLED"
    assert snapshot["all_converged"] is True
    assert snapshot["has_failure"] is False
