from __future__ import annotations

import json
from pathlib import Path

from src.graph.fork import fork_workspace_from_completed_upstream
from src.graph.resume import KEEP, SKIP, build_resume_plan
from src.graph.state import DAGNode, DAGState
from src.utils.results import write_json, write_pipeline_context


def make_source_workspace(root: Path) -> DAGState:
    nodes = [
        DAGNode(
            node_id="clean",
            title="clean",
            goal="clean slab",
            workspace_subdir="clean",
            task_md_path="clean/task.md",
            status="done",
        ),
        DAGNode(
            node_id="ads",
            title="ads",
            goal="adsorbate",
            workspace_subdir="ads",
            task_md_path="ads/task.md",
            depends_on=["clean"],
            status="running",
            retry_count=1,
            restart_count=1,
        ),
        DAGNode(
            node_id="product",
            title="product",
            goal="product",
            workspace_subdir="product",
            task_md_path="product/task.md",
            depends_on=["ads"],
            status="pending",
            restart_count=1,
        ),
    ]
    for node in nodes:
        node_dir = root / node.workspace_subdir
        node_dir.mkdir(parents=True)
        (node_dir / "task.md").write_text(f"# {node.node_id}\n", encoding="utf-8")

    result_path = root / "clean" / "node_result.json"
    structure_path = root / "clean" / "CONTCAR"
    structure_path.write_text("structure\n", encoding="utf-8")
    write_json(
        result_path,
        {
            "node_status": "success",
            "findings": [f"Read completed structure at {structure_path}."],
            "artifacts": {
                "node_result": str(result_path),
                "final_structure": str(structure_path),
            },
        },
    )
    write_pipeline_context(
        str(root / "clean"),
        user_command="clean slab",
        review_mode="enabled",
    )
    nodes[0].artifacts = {
        "node_result": str(result_path),
        "final_structure": str(structure_path),
    }
    active_job = root / "ads" / "slab_opt" / ".job_id"
    active_job.parent.mkdir()
    active_job.write_text("12345\n", encoding="utf-8")

    state = DAGState(workspace_path=str(root), user_goal="reaction", nodes=nodes)
    state.save()
    state.save_blueprint()
    return state


def test_fork_rebases_completed_evidence_and_excludes_reset_jobs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    make_source_workspace(source)

    result = fork_workspace_from_completed_upstream(
        source,
        target,
        restart_nodes=["ads"],
    )

    assert result.preserved_nodes == ("clean",)
    assert result.reset_nodes == ("ads", "product")
    assert result.ready_nodes == ("ads",)
    assert (target / "clean" / "CONTCAR").is_file()
    assert (target / "ads" / "task.md").is_file()
    assert (target / "product" / "task.md").is_file()
    assert not (target / "ads" / "slab_opt" / ".job_id").exists()

    state = DAGState.load(str(target))
    assert state.workspace_path == str(target.resolve())
    assert state.get_node("clean").status == "done"
    assert state.get_node("ads").status == "ready"
    assert state.get_node("product").status == "pending"
    assert state.get_node("ads").retry_count == 1
    assert state.get_node("ads").restart_count == 1
    assert state.get_node("ads").artifacts == {}
    assert all(str(target.resolve()) in value for value in state.get_node("clean").artifacts.values())

    node_result = json.loads((target / "clean" / "node_result.json").read_text(encoding="utf-8"))
    assert str(source.resolve()) not in json.dumps(node_result)
    assert str(target.resolve()) in json.dumps(node_result)
    context = json.loads((target / ".pipeline_context.json").read_text(encoding="utf-8"))
    assert context["review_mode"] == "disabled"
    assert context["forked_from"] == str(source.resolve())

    def unexpected_inspector(*args, **kwargs):
        raise AssertionError("Forked reset jobs must not retain .job_id files")

    decisions = {
        decision.node_id: decision.action for decision in build_resume_plan(state, job_inspector=unexpected_inspector)
    }
    assert decisions == {"clean": SKIP, "ads": KEEP, "product": KEEP}
