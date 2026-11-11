"""Exercise the node repair contract with Slurm mocked at the process boundary."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.graph.resume import CONTINUE, build_resume_plan
from src.graph.state import DAGNode, DAGState
from src.monitor.job_monitor import JobMonitor, JobState
from src.tools.analyzer_tools import WriteNodeResultAction, WriteNodeResultExecutor
from src.tools.repair_tools import PrepareNodeRepairAction, PrepareNodeRepairExecutor
from src.tools.runner_tools import SubmitVaspJobAction, SubmitVaspJobExecutor, apply_authoritative_job_results
from src.utils.results import read_runner_result, update_runner_result, write_json


def repair_case(tmp_path: Path, subdir: str = ".") -> tuple[DAGState, Path, PrepareNodeRepairAction]:
    node_dir = tmp_path / "node"
    source = node_dir / subdir
    source.mkdir(parents=True)
    (node_dir / "task.md").write_text("task_type: slab_opt")
    for name in ("POSCAR", "INCAR", "KPOINTS", "POTCAR"):
        (source / name).write_text(f"original {name}")
    (source / ".job_id").write_text("123")
    (source / "CONTCAR").write_text("")
    write_json(node_dir / "parameter_summary.json", {"calculations": [{"subdir": subdir}]})
    update_runner_result(node_dir, job_status={"results": {subdir: "FAILED"}})
    state = DAGState(
        workspace_path=str(tmp_path),
        user_goal="repair",
        nodes=[
            DAGNode(
                node_id="node",
                title="test",
                goal="test",
                workspace_subdir="node",
                task_md_path="node/task.md",
                status="running",
            )
        ],
    )
    state.save()
    action = PrepareNodeRepairAction(
        node_dir=str(node_dir),
        calculations=[subdir],
        diagnosis={
            "worker": "param_worker_agent",
            "evidence": ["slurm.out: electronic instability"],
            "hypotheses": ["algorithm instability"],
            "repair_goal": "stable electronic minimization",
            "acceptance_criteria": ["same scientific model", "converged result"],
        },
    )
    return state, node_dir, action


@pytest.mark.parametrize("subdir", [".", "slab"])
def test_repair_validation_submission_resume_and_finalization(tmp_path, monkeypatch, subdir):
    import src.tools.runner_tools as runner_tools

    state, node_dir, action = repair_case(tmp_path, subdir)
    monkeypatch.setattr(JobMonitor, "check_slurm_status", lambda *args: (JobState.FAILED, 6, None, None))
    prepare = PrepareNodeRepairExecutor(str(node_dir))
    prepared = prepare(action)
    assert prepared.prepared, prepared.message
    target = node_dir / prepared.plan["calculations"][subdir]
    assert (target / "POSCAR").read_text() == "original POSCAR"
    assert not (target / ".job_id").exists()
    assert not (node_dir / "node_result.json").exists()
    assert build_resume_plan(state)[0].action == CONTINUE
    assert prepare(action).plan == prepared.plan

    # Missing/stale validation is not authorization to submit a repair.
    submit = SubmitVaspJobExecutor(str(node_dir))
    submission = SubmitVaspJobAction(working_dir=str(target), total_cpus=24, render_sbatch_if_missing=False)
    write_json(
        node_dir / "parameter_summary.json", {"calculations": [{"subdir": target.relative_to(node_dir).as_posix()}]}
    )
    assert "fresh PASS" in submit(submission).message
    for name in ("modeling_validation.json", "parameter_validation.json"):
        write_json(node_dir / name, {"status": "PASS", "repair_attempt": 1})
    (target / "INCAR").write_text("ALGO = Normal")
    (target / "vasp.sbatch").write_text("#!/bin/bash\n")
    monkeypatch.setattr(runner_tools, "hpc_config", lambda: {"submit_module_loads": [], "sbatch_export": "ALL"})
    submissions = []

    def sbatch(command, **kwargs):
        submissions.append(command)
        return SimpleNamespace(returncode=0, stdout="Submitted batch job 456", stderr="")

    monkeypatch.setattr(runner_tools.subprocess, "run", sbatch)
    assert submit(submission).submitted
    assert not submit(submission).submitted
    assert len(submissions) == 1
    assert (node_dir / subdir / ".job_id").read_text() == "123"
    assert (node_dir / subdir / "INCAR").read_text() == "original INCAR"
    assert build_resume_plan(state)[0].action == CONTINUE
    assert prepare(action).plan == prepared.plan
    assert (target / "INCAR").read_text() == "ALGO = Normal"

    different = action.model_copy(
        update={"diagnosis": action.diagnosis.model_copy(update={"repair_goal": "try again"})}
    )
    assert not prepare(different).prepared
    snapshot = {"results": {subdir: "FAILED", target.relative_to(node_dir).as_posix(): "CONVERGED"}}
    apply_authoritative_job_results(snapshot, read_runner_result(node_dir)["authoritative_calculations"])
    assert snapshot["all_converged"] and not snapshot["has_failure"]
    update_runner_result(node_dir, job_status=snapshot)
    (target / "CONTCAR").write_text("repaired final geometry")
    write_json(
        node_dir / "parameter_summary.json", {"calculations": [{"subdir": target.relative_to(node_dir).as_posix()}]}
    )
    WriteNodeResultExecutor(str(node_dir))(WriteNodeResultAction(node_dir=str(node_dir), node_status="success"))
    result = json.loads((node_dir / "node_result.json").read_text())
    assert result["recovery_attempted"] is True
    assert result["artifacts"]["final_structure"] == str(target / "CONTCAR")
    assert not prepare(action).prepared


def test_repair_refuses_unsafe_jobs_and_ambiguous_submission(tmp_path, monkeypatch):
    import src.tools.repair_tools as repair_tools
    import src.tools.runner_tools as runner_tools

    state, node_dir, action = repair_case(tmp_path)
    prepare = PrepareNodeRepairExecutor(str(node_dir))
    for status in (JobState.RUNNING, None, JobState.CANCELLED):
        monkeypatch.setattr(JobMonitor, "check_slurm_status", lambda *args, status=status: (status, None, None, None))
        assert not prepare(action).prepared
        assert not read_runner_result(node_dir).get("recovery_attempts")
    monkeypatch.setattr(JobMonitor, "check_slurm_status", lambda *args: (JobState.FAILED, 6, None, None))
    monkeypatch.setenv("CATPAW_NODE_RETRY_LIMIT", "0")
    assert not prepare(action).prepared
    monkeypatch.setenv("CATPAW_NODE_RETRY_LIMIT", "1")
    state.nodes[0].retry_count = 1
    state.save()
    assert not prepare(action).prepared
    state.nodes[0].retry_count = 0
    state.save()
    with monkeypatch.context() as interrupted:

        def copy_interrupted(*args, **kwargs):
            raise OSError("preparation interrupted")

        interrupted.setattr(repair_tools.shutil, "copy2", copy_interrupted)
        assert not prepare(action).prepared
    assert read_runner_result(node_dir)["recovery_attempts"][0]["status"] == "preparing"
    assert build_resume_plan(state)[0].action == CONTINUE
    plan = prepare(action).plan
    assert len(read_runner_result(node_dir)["recovery_attempts"]) == 1
    target = node_dir / plan["calculations"]["."]
    write_json(
        node_dir / "parameter_summary.json", {"calculations": [{"subdir": target.relative_to(node_dir).as_posix()}]}
    )
    for name in ("modeling_validation.json", "parameter_validation.json"):
        write_json(node_dir / name, {"status": "PASS", "repair_attempt": 1})
    (target / "vasp.sbatch").write_text("#!/bin/bash\n")
    monkeypatch.setattr(runner_tools, "hpc_config", lambda: {"submit_module_loads": []})
    calls = []

    def lost_reply(*args, **kwargs):
        calls.append(True)
        raise OSError("submission reply lost")

    monkeypatch.setattr(runner_tools.subprocess, "run", lost_reply)
    submit = SubmitVaspJobExecutor(str(node_dir))
    submission = SubmitVaspJobAction(working_dir=str(target), total_cpus=24, render_sbatch_if_missing=False)
    assert not submit(submission).submitted
    assert "reconcile" in submit(submission).message
    assert len(calls) == 1
    snapshot = {"results": {".": "FAILED"}}
    apply_authoritative_job_results(snapshot, plan["calculations"])
    assert not snapshot["all_terminal"]
    assert not snapshot["all_converged"]
