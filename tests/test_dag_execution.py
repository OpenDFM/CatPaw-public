"""Representative contracts: dependency wakeup, shared inputs, and local recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_start_method
from threading import Event

import pytest

from src.graph.state import DAGNode, DAGState
from src.tools.dag_tools import DAGControlAction, DAGControlExecutor
from src.tools.param_validation_tools import compare_reference


def initialize(path, nodes):
    control = DAGControlExecutor(str(path))
    payload = [
        {"node_id": name, "task_md_content": "# Task\ntask_type: slab_opt", **fields} for name, fields in nodes.items()
    ]
    result = json.loads(control(DAGControlAction(command="initialize", nodes=payload)).message)
    assert result["status"] == "success", result
    return control


def test_shared_inputs_release_members_and_restart_only_descendants(tmp_path):
    control = initialize(
        tmp_path,
        {
            "clean": {},
            "oh": {"inputs": {"structure": "clean/final_structure"}},
            "o": {"inputs": {"structure": "clean/final_structure"}},
            "gas": {},
        },
    )
    state = DAGState.load(str(tmp_path))
    assert {n.node_id for n in state.get_ready_nodes()} == {"clean", "gas"}
    structure = tmp_path / "clean" / "CONTCAR"
    structure.write_text("validated basis")
    state.mark_done("clean", {"final_structure": str(structure)})
    for name in ["oh", "o"]:
        result = json.loads(control(DAGControlAction(command="start", node_ids=[name])).message)
        assert result["status"] == "success"
        assert json.loads((tmp_path / name / "inputs.json").read_text())["structure"] == str(structure)
    state = DAGState.load(str(tmp_path))
    assert {n.node_id for n in state.transitive_descendants(["clean"])} == {"clean", "oh", "o"}
    # Blueprint must retain bindings, including derived dependencies.
    rebuilt = DAGState.create_from_blueprint(str(tmp_path), "test")
    assert rebuilt.get_node("oh").dependencies == ["clean"]
    assert rebuilt.get_node("oh").depends_on == []


def test_scheduler_wakes_fast_branch_before_slow_root_finishes(tmp_path, monkeypatch):
    import src.graph.runner as module

    initialize(tmp_path, {"clean": {}, "slow_gas": {}, "oh": {"depends_on": ["clean"]}})
    member_started = Event()
    finished = []

    def execute(**kwargs):
        name = kwargs["node_payload"]["node_id"]
        if name == "slow_gas":
            assert member_started.wait(5), "A slow root blocked the ready surface branch"
        (tmp_path / name / "node_result.json").write_text(json.dumps({"node_status": "success", "artifacts": {}}))
        finished.append(name)
        if name == "oh":
            member_started.set()
        return {"metrics": {}}

    monkeypatch.setattr(module, "ProcessPoolExecutor", lambda *, max_workers, mp_context: ThreadPoolExecutor(max_workers))
    monkeypatch.setattr(module, "run_coordinator_child", execute)
    runner = module.SupervisorRunner.__new__(module.SupervisorRunner)
    runner.workspace_path = str(tmp_path)
    runner.user_command = "test"
    runner.human_review = runner.review_node_result = runner.resume = False
    runner.max_node_parallel = 2
    runner.max_expansion_rounds = 0
    runner.metrics_waves = []
    runner.run_dag_scheduler_loop()
    assert finished.index("oh") < finished.index("slow_gas")
    assert DAGState.load(str(tmp_path)).status == "done"


def execute_coordinator_in_spawned_process(**kwargs):
    """Exercise the real child entry point without LLM/Slurm/network calls."""
    import src.graph.runner as module

    assert get_start_method() == "spawn"

    def coordinator(self, *, node, node_workspace, **unused):
        state = DAGState.load(self.workspace_path)
        assert all(state.get_node(dep).status == "done" for dep in node.dependencies)
        (node_workspace / "node_result.json").write_text(
            json.dumps({"node_status": "success", "artifacts": {}})
        )
        return {"tag": node.node_id}

    def forbidden_flush():
        raise AssertionError("Trace export must not block the child result")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module.SupervisorRunner, "register_subagents", lambda self: None)
        patch.setattr(module.SupervisorRunner, "run_coordinator_agent", coordinator)
        patch.setattr(module.tracing, "enabled", lambda: False)
        patch.setattr(module.tracing, "flush", forbidden_flush, raising=False)
        return module.run_coordinator_child(**kwargs)


def test_spawned_coordinator_results_release_dependents(tmp_path, monkeypatch):
    import src.graph.runner as module

    initialize(tmp_path, {"clean": {}, "gas": {}, "ads": {"depends_on": ["clean"]}})
    monkeypatch.setattr(module, "run_coordinator_child", execute_coordinator_in_spawned_process)
    monkeypatch.setattr(module.tracing, "parent_context", lambda: None)
    runner = module.SupervisorRunner.__new__(module.SupervisorRunner)
    runner.workspace_path = str(tmp_path)
    runner.user_command = "test"
    runner.human_review = runner.review_node_result = runner.resume = False
    runner.max_node_parallel = 2
    runner.max_expansion_rounds = 0
    runner.metrics_waves = []
    runner.run_dag_scheduler_loop()

    state = DAGState.load(str(tmp_path))
    assert state.status == "done"
    assert {metrics["tag"] for metrics in runner.metrics_waves} == {"clean", "gas", "ads"}
    assert all((tmp_path / node.node_id / "node_result.json").is_file() for node in state.nodes)


def test_input_cycle_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        DAGState(
            workspace_path=str(tmp_path),
            user_goal="test",
            nodes=[DAGNode("a", "A", "test", "a", "a/task.md", inputs={"structure": "a/final_structure"})],
        )


def test_parameter_reference_compares_common_species_and_actual_precision(tmp_path):
    clean, ads = tmp_path / "clean", tmp_path / "ads"
    for path in (clean, ads):
        path.mkdir()
        (path / "INCAR").write_text("ENCUT=520\nPREC=Accurate\nISPIN=2\n")
        (path / "KPOINTS").write_text("mesh\n0\nGamma\n3 3 1\n0 0 0\n")
    (clean / "POTCAR").write_text("TITEL = PAW_PBE Pt 01Jan2000\n")
    (ads / "POTCAR").write_text("TITEL = PAW_PBE Pt 01Jan2000\nTITEL = PAW_PBE O 01Jan2000\n")
    reference = {"input_files": {key.lower(): str(clean / key) for key in ("INCAR", "KPOINTS", "POTCAR")}}
    assert compare_reference(ads, reference) == []
    (ads / "INCAR").write_text("ENCUT=400\nPREC=Accurate\nISPIN=2\n")
    assert compare_reference(ads, reference) == ["ENCUT"]
