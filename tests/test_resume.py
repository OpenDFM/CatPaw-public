from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.graph.resume import (
    ANALYZE,
    CONTINUE,
    MONITOR,
    build_resume_plan,
)
from src.graph.state import NODE_FAILED, DAGNode, DAGState
from src.utils.results import write_json


def make_state(tmp_path: Path, *, status: str = NODE_FAILED) -> tuple[DAGState, Path]:
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    state = DAGState(
        workspace_path=str(tmp_path),
        user_goal="test resume",
        nodes=[
            DAGNode(
                node_id="node",
                title="Node",
                goal="test",
                workspace_subdir="node",
                task_md_path="node/task.md",
                status=status,
            )
        ],
    )
    state.save()
    return state, node_dir


def converged_runner_result(node_dir: Path) -> None:
    write_json(
        node_dir / "runner_result.json",
        {
            "job_status": {
                "all_terminal": True,
                "all_converged": True,
                "has_failure": False,
            },
            "calc_summary": {"calc": {"converged": True}},
        },
    )


def test_converged_runner_result_uses_analyzer_only(tmp_path: Path) -> None:
    state, node_dir = make_state(tmp_path)
    converged_runner_result(node_dir)
    (node_dir / ".job_id").write_text("123\n", encoding="utf-8")

    def unexpected_inspector(*args, **kwargs):
        raise AssertionError("Slurm should not be queried for a durable converged runner_result")

    first = build_resume_plan(state, job_inspector=unexpected_inspector)
    second = build_resume_plan(DAGState.load(str(tmp_path)), job_inspector=unexpected_inspector)

    assert first == second
    assert first[0].action == ANALYZE
    assert (node_dir / ".job_id").read_text(encoding="utf-8").strip() == "123"
    assert DAGState.load(str(tmp_path)).get_node("node").retry_count == 0


def test_existing_active_job_is_monitored_without_submission(tmp_path: Path) -> None:
    state, node_dir = make_state(tmp_path)
    (node_dir / ".job_id").write_text("456\n", encoding="utf-8")

    def active_inspector(workspace_path: str, *, persist: bool):
        assert workspace_path == str(node_dir)
        assert persist is False
        return object(), {
            "all_converged": False,
            "has_failure": False,
            "all_terminal": False,
            "results": {"calc": "RUNNING"},
        }

    decision = build_resume_plan(state, job_inspector=active_inspector)[0]

    assert decision.action == MONITOR
    assert decision.evidence["job_ids"] == ["456"]


def test_failed_job_without_final_result_returns_to_coordinator(tmp_path: Path) -> None:
    state, node_dir = make_state(tmp_path)
    (node_dir / ".job_id").write_text("789\n", encoding="utf-8")

    def failed_inspector(workspace_path: str, *, persist: bool):
        return object(), {
            "all_converged": False,
            "has_failure": True,
            "all_terminal": True,
            "results": {"calc": "FAILED"},
        }

    decision = build_resume_plan(state, job_inspector=failed_inspector)[0]

    assert decision.action == CONTINUE
    assert (node_dir / ".job_id").read_text().strip() == "789"
    assert DAGState.load(str(tmp_path)).get_node("node").retry_count == 0


def test_restored_conversation_reuses_persistence_without_duplicate_prompt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import openhands.sdk
    from openhands.sdk.conversation.state import ConversationExecutionStatus

    from src.graph.runner import SupervisorRunner

    captured: dict = {}

    class FakeState:
        events = [object()]
        execution_status = ConversationExecutionStatus.ERROR

    class FakeConversation:
        state = FakeState()
        id = "conversation"

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.sent: list[str] = []
            self.ran = False
            self.closed = False

        def send_message(self, message: str) -> None:
            self.sent.append(message)

        def run(self) -> None:
            self.ran = True

        def close(self) -> None:
            self.closed = True

    fake = FakeConversation()

    def conversation_factory(**kwargs):
        FakeConversation.__init__(fake, **kwargs)
        return fake

    monkeypatch.setattr(openhands.sdk, "Conversation", conversation_factory)
    runner = SupervisorRunner.__new__(SupervisorRunner)
    runner.workspace_path = str(tmp_path)

    runner.run_agent(
        agent=object(),
        prompt="do not duplicate",
        conversation_tag="NODE",
        step_title="resume",
    )

    assert captured["persistence_dir"] == tmp_path / ".openhands" / "conversations"
    assert fake.sent == []
    assert fake.ran is True
    assert fake.closed is True


def test_reconcile_only_review_override_does_not_modify_context(tmp_path: Path, monkeypatch) -> None:
    import src.main as main_module
    from src.utils.results import write_pipeline_context

    make_state(tmp_path)
    context_path = write_pipeline_context(
        str(tmp_path),
        user_command="test",
        review_mode="disabled",
    )
    before = context_path.read_bytes()
    captured = {}

    class FakeRunner:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def run(self) -> str:
            return "job"

    monkeypatch.setattr(main_module, "SupervisorRunner", FakeRunner)
    args = SimpleNamespace(
        resume=str(tmp_path),
        restart_nodes=None,
        input=None,
        workspace=None,
        goal=None,
        review=True,
        reconcile_only=True,
        completion_profile=None,
        run_node=None,
    )
    main_module.run_pipeline_command(args)

    assert captured["human_review"] is True
    assert context_path.read_bytes() == before


def test_analyzer_review_revision_preserves_calculation_evidence(tmp_path: Path, monkeypatch) -> None:
    import src.agents.analyzer as analyzer_module
    from src.graph.runner import SupervisorRunner

    state, node_dir = make_state(tmp_path, status="running")
    node = state.get_node("node")
    result_path = node_dir / "node_result.json"
    write_json(result_path, {"node_status": "success", "artifacts": {}, "analysis": {"summary": "old"}})
    job_id_path = node_dir / "calc" / ".job_id"
    job_id_path.parent.mkdir()
    job_id_path.write_text("12345\n", encoding="utf-8")
    runner_result_path = node_dir / "runner_result.json"
    runner_result_path.write_text('{"job_status": {"all_converged": true}}\n', encoding="utf-8")
    incar_path = node_dir / "calc" / "INCAR"
    incar_path.write_text("ENCUT = 500\n", encoding="utf-8")

    runner = SupervisorRunner.__new__(SupervisorRunner)
    runner.workspace_path = str(tmp_path)
    runner.user_command = "test"
    runner.metrics_waves = []
    captured: dict = {}
    monkeypatch.setattr(analyzer_module, "create_vasp_analyzer_agent", lambda: object())

    def fake_run_agent(**kwargs):
        captured.update(kwargs)
        write_json(
            result_path,
            {"node_status": "success", "artifacts": {}, "analysis": {"summary": "revised"}},
        )
        return {"tag": "analyzer-review"}

    monkeypatch.setattr(runner, "run_agent", fake_run_agent)

    metadata = runner.rerun_analyzer_for_node_review(
        node,
        feedback="REVISE clarify",
        revision_round=1,
    )

    restored = DAGState.load(str(tmp_path)).get_node("node")
    assert metadata["analysis"]["summary"] == "revised"
    assert job_id_path.read_text(encoding="utf-8") == "12345\n"
    assert runner_result_path.read_text(encoding="utf-8") == '{"job_status": {"all_converged": true}}\n'
    assert incar_path.read_text(encoding="utf-8") == "ENCUT = 500\n"
    assert restored.retry_count == 0
    assert restored.status == "running"
    assert "Do not submit, monitor, restart" in captured["prompt"]
    assert runner.metrics_waves == [{"tag": "analyzer-review"}]
