from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.graph.restart import apply_restart_plan, build_restart_plan
from src.graph.state import DAG_ACTIVE, DAGNode, DAGState
from src.utils.results import write_json


def make_restart_workspace(tmp_path: Path) -> DAGState:
    specs = [
        ("clean", [], "done", 0, 0),
        ("ads", ["clean"], "done", 1, 2),
        ("h1", ["ads"], "done", 0, 1),
        ("h2", ["h1"], "failed", 1, 0),
        ("sibling", ["clean"], "done", 0, 0),
    ]
    nodes: list[DAGNode] = []
    for node_id, dependencies, status, retry_count, restart_count in specs:
        node_dir = tmp_path / node_id
        node_dir.mkdir()
        (node_dir / "task.md").write_text(f"# {node_id}\n", encoding="utf-8")
        (node_dir / "old_evidence.txt").write_text(f"old evidence for {node_id}\n", encoding="utf-8")
        result_path = node_dir / "node_result.json"
        write_json(
            result_path,
            {
                "node_status": "success",
                "artifacts": {"node_result": str(node_dir / "node_result.json")},
            },
        )
        nodes.append(
            DAGNode(
                node_id=node_id,
                title=node_id,
                goal=f"run {node_id}",
                workspace_subdir=node_id,
                task_md_path=f"{node_id}/task.md",
                depends_on=dependencies,
                status=status,
                artifacts={"node_result": str(result_path)},
                last_error="old failure" if status == "failed" else None,
                retry_count=retry_count,
                restart_count=restart_count,
            )
        )

    state = DAGState(
        workspace_path=str(tmp_path),
        user_goal="restart adsorption branch",
        nodes=nodes,
    )
    state.save()
    state.save_blueprint()
    return state


def file_snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_active_descendant_job_blocks_the_whole_restart(tmp_path: Path) -> None:
    state = make_restart_workspace(tmp_path)
    job_marker = tmp_path / "h1" / "slab_opt" / ".job_id"
    job_marker.parent.mkdir()
    job_marker.write_text("12345\n", encoding="utf-8")
    before = file_snapshot(tmp_path)

    def active_inspector(workspace_path: str, *, persist: bool):
        assert workspace_path == str(tmp_path / "h1")
        assert persist is False
        return object(), {
            "job_count": 1,
            "all_terminal": False,
            "all_converged": False,
            "has_failure": False,
            "results": {"slab_opt": "RUNNING"},
        }

    plan = build_restart_plan(state, ["ads"], job_inspector=active_inspector)

    assert plan.blocked_reasons
    assert "h1" in " ".join(plan.blocked_reasons)
    with pytest.raises(RuntimeError, match="active|blocked|terminal"):
        apply_restart_plan(plan)
    assert file_snapshot(tmp_path) == before
    assert not (tmp_path / ".restart_attempts").exists()


def test_apply_restart_archives_evidence_and_resets_only_the_closure(tmp_path: Path) -> None:
    state = make_restart_workspace(tmp_path)
    (tmp_path / "final_report.md").write_text("stale report\n", encoding="utf-8")
    plan = build_restart_plan(state, ["ads"])

    archive_root = apply_restart_plan(plan)

    assert archive_root == Path(plan.archive_root)
    assert archive_root.is_dir()
    for node_id in ("ads", "h1", "h2"):
        assert (archive_root / node_id / "old_evidence.txt").is_file()
        assert (archive_root / node_id / "node_result.json").is_file()
        assert (tmp_path / node_id / "task.md").is_file()
        assert not (tmp_path / node_id / "old_evidence.txt").exists()
        assert not (tmp_path / node_id / "node_result.json").exists()

    assert (tmp_path / "clean" / "old_evidence.txt").is_file()
    assert (tmp_path / "sibling" / "old_evidence.txt").is_file()

    restored = DAGState.load(str(tmp_path))
    assert restored.status == DAG_ACTIVE
    assert restored.get_node("clean").status == "done"
    assert restored.get_node("sibling").status == "done"
    assert restored.get_node("ads").status == "ready"
    assert restored.get_node("h1").status == "pending"
    assert restored.get_node("h2").status == "pending"
    for node_id in ("ads", "h1", "h2"):
        node = restored.get_node(node_id)
        assert node.artifacts == {}
        assert node.last_error is None

    assert restored.get_node("ads").retry_count == 1
    assert restored.get_node("h1").retry_count == 0
    assert restored.get_node("h2").retry_count == 1
    assert restored.get_node("ads").restart_count == 3
    assert restored.get_node("h1").restart_count == 2
    assert restored.get_node("h2").restart_count == 1

    assert not (tmp_path / "final_report.md").exists()
    assert (archive_root / "_workspace" / "final_report.md").read_text(encoding="utf-8") == "stale report\n"

    recovery_log = tmp_path / ".recovery_log.jsonl"
    assert recovery_log.is_file()
    records = [json.loads(line) for line in recovery_log.read_text(encoding="utf-8").splitlines()]
    assert records
    serialized = json.dumps(records, ensure_ascii=False)
    assert "scientific_invalidation" in serialized
    assert str(archive_root) in serialized


def test_restart_rolls_back_when_archiving_a_later_node_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.graph.restart as restart_module

    state = make_restart_workspace(tmp_path)
    plan = build_restart_plan(state, ["ads"])
    before = file_snapshot(tmp_path)
    real_replace = restart_module.os.replace

    def fail_on_second_node(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path == tmp_path / "h1" and ".restart_attempts" in destination_path.parts:
            raise OSError("injected archive failure")
        return real_replace(source, destination)

    monkeypatch.setattr(restart_module.os, "replace", fail_on_second_node)

    with pytest.raises(OSError, match="injected archive failure"):
        apply_restart_plan(plan)

    assert file_snapshot(tmp_path) == before
    assert not Path(plan.archive_root).exists()
    assert not (tmp_path / ".restart_attempts").exists()
