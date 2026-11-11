import base64
import json
import select
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from src.execution.workflows import wait_for_web_review
from src.graph.state import DAGNode, DAGState
from src.web.calculation_progress import iteration_history
from src.web.dashboard import (
    DashboardDataStore,
    DashboardRequestHandler,
    conversations,
)


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("fail_headers", [False, True])
def test_response_closes_disconnected_client(error, streaming, fail_headers):
    handler = DashboardRequestHandler.__new__(DashboardRequestHandler)
    handler.close_connection = False
    handler.send_response = lambda status: None
    handler.send_header = lambda *args: None

    def disconnected(*args):
        raise error("client disconnected")

    handler.end_headers = disconnected if fail_headers else lambda: None
    handler.wfile = SimpleNamespace(write=disconnected)
    handler.data_store = SimpleNamespace(events=SimpleNamespace(stream=lambda: iter([{"workspaces": None}])))
    if streaming:
        handler.send_events()
    else:
        handler.send_json({"ok": True})
    assert handler.close_connection


def test_conversations_follow_call_tree_and_sibling_start_times(tmp_path):
    records = [
        ("z_supervisor", "00:00:00"),
        ("b_coordinator", "00:01:00"),
        ("b_coordinator/subagents/z_worker", "00:02:00"),
        ("b_coordinator/subagents/a_validator", "00:04:00"),
        ("a_parallel_coordinator", "00:03:00"),
        ("c_without_events", None),
    ]
    root = tmp_path / ".openhands" / "conversations"
    for name, start in records:
        directory = root / name
        (directory / "events").mkdir(parents=True)
        (directory / "base_state.json").write_text(json.dumps({
            "workspace": {"working_dir": str(tmp_path)},
            "agent": {"system_prompt_filename": "coordinator.j2"},
            "execution_status": "finished",
        }))
        if start:
            (directory / "events" / "event-00000.json").write_text(
                json.dumps({"timestamp": f"2026-09-16T{start}+00:00"})
            )
    assert [item["id"] for item in conversations(tmp_path)] == [name for name, _ in records]


def test_http_projects_dag_node_and_trace_without_sdk_credentials(tmp_path):
    workspace = tmp_path / "example"
    (workspace / "clean").mkdir(parents=True)
    DAGState(
        workspace_path=str(workspace),
        user_goal="adsorption",
        nodes=[
            DAGNode("clean", "Clean surface", "relax", "clean", "clean/task.md"),
            DAGNode("oh", "OH", "relax", "oh", "oh/task.md", inputs={"structure": "clean/final_structure"}),
        ],
    ).save()
    (workspace / ".weave.json").write_text(
        json.dumps({"project": "team/catpaw", "url": "https://wandb.ai/team/catpaw/weave"})
    )
    (workspace / "clean" / "task.md").write_text("# Slab optimization\n\ntask_type: slab_opt")
    (workspace / "clean" / "parameter_validation.json").write_text(json.dumps({"notes": "x" * 100001}))
    (workspace / "clean" / "runner_result.json").write_text(
        json.dumps(
            {
                "job_status": {
                    "jobs": [{"subdir": "clean", "state": "RUNNING", "last_checked": "2026-09-07T00:00:00+00:00"}]
                }
            }
        )
    )
    (workspace / "clean" / ".monitor_state.json").write_text(
        json.dumps(
            {
                "jobs": {
                    "clean": {
                        "subdir": "clean",
                        "state": "COMPLETED",
                        "converged": False,
                        "last_checked": "2026-09-07T00:01:00+00:00",
                    }
                }
            }
        )
    )
    api_wait_dir = workspace / ".webui" / "api_wait"
    api_wait_dir.mkdir(parents=True)
    future = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    past = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    (api_wait_dir / "active.json").write_text(json.dumps({"role": "supervisor", "retry_at": future}))
    (api_wait_dir / "expired.json").write_text(json.dumps({"role": "coordinator", "retry_at": past}))
    conversation = workspace / ".openhands" / "conversations" / "coordinator" / "subagents" / "modeler"
    (conversation / "events").mkdir(parents=True)
    (conversation / "base_state.json").write_text(
        json.dumps(
            {
                "workspace": {"working_dir": str(workspace / "clean")},
                "agent": {"system_prompt_filename": "modeling_worker.j2", "llm": {"api_key": "do-not-expose"}},
                "execution_status": "finished",
            }
        )
    )
    (conversation / "events" / "event-00001-id.json").write_text(
        json.dumps(
            {
                "id": "id",
                "kind": "ActionEvent",
                "timestamp": "2026-09-07T00:00:00",
                "tool_name": "terminal",
                "action": {"command": "echo test", "content": "tool input"},
                "thought": "private reasoning",
            }
        )
    )
    (conversation / "events" / "event-00002-message.json").write_text(
        json.dumps(
            {
                "id": "message",
                "kind": "MessageEvent",
                "timestamp": "2026-09-07T00:00:01",
                "llm_message": {"role": "assistant", "content": [{"type": "text", "text": "First line\nSecond line"}]},
            }
        )
    )
    calculation = workspace / "clean" / "slab opt"
    calculation.mkdir()
    structure = b"Pt\n1.0\n3 0 0\n0 3 0\n0 0 3\nPt\n1\nDirect\n0 0 0\n"
    for name in ("POSCAR", "CONTCAR"):
        (calculation / name).write_bytes(structure)
    (workspace / "clean" / ".private").mkdir()
    (workspace / "clean" / ".private" / "POSCAR").write_bytes(structure)
    outside = tmp_path / "POSCAR"
    outside.write_bytes(structure)
    (workspace / "clean" / "POSCAR").symlink_to(outside)
    store = DashboardDataStore(tmp_path)
    store.events.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(DashboardRequestHandler, data_store=store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = f"http://127.0.0.1:{server.server_port}"
    try:

        def get(route):
            with build_opener(ProxyHandler({})).open(root + route) as response:
                return json.loads(response.read())

        with build_opener(ProxyHandler({})).open(root + "/api/events", timeout=5) as stream:
            assert json.loads(stream.readline().decode().removeprefix("data: ")) == {"workspaces": None}
            stream.readline()
            # Publish a new SDK event and receive an unsolicited workspace notification.
            (conversation / "events" / "event-00003-new.json").write_text(
                json.dumps(
                    {
                        "id": "new",
                        "kind": "MessageEvent",
                        "timestamp": "2026-09-07T00:00:02",
                        "llm_message": {"role": "assistant", "content": [{"type": "text", "text": "New message"}]},
                    }
                )
            )
            assert json.loads(stream.readline().decode().removeprefix("data: "))["workspaces"] == ["example"]
        detail = get("/api/workspaces/example")
        assert detail["nodes"][1]["dependencies"] == ["clean"]
        assert detail["weave"]["url"] == "https://wandb.ai/team/catpaw/weave"
        assert detail["conversations"][0]["parent"] == "coordinator"
        assert detail["conversations"][0]["started_at"] == "2026-09-07T00:00:00"
        assert detail["conversations"][0]["last_event_at"] == "2026-09-07T00:00:02"
        assert detail["api_waits"] == [{"role": "supervisor", "retry_at": future}]
        assert "do-not-expose" not in json.dumps(detail)
        node = get("/api/workspaces/example/nodes/clean")
        assert node["document_html"]["task.md"].startswith("<h1>Slab optimization</h1>")
        assert json.loads(node["documents"]["parameter_validation.json"])["notes"] == "x" * 100001
        assert node["inspection"]["jobs"][0]["state"] == "COMPLETED"
        assert node["inspection"]["jobs"][0]["converged"] is False
        assert node["inspection"]["structure_files"] == ["clean/slab opt/CONTCAR", "clean/slab opt/POSCAR"]
        for name in ("POSCAR", "CONTCAR"):
            with build_opener(ProxyHandler({})).open(
                root + "/workspace-assets/example/clean/slab%20opt/" + name
            ) as response:
                assert response.read() == structure
        with pytest.raises(HTTPError) as escaped:
            get("/workspace-assets/example/clean/POSCAR")
        assert escaped.value.code == 400
        # Reading a node never generates a preview; explicit requests persist one snapshot.
        assert node["inspection"]["structure_previews"] == {}
        assert not list(calculation.glob("*.preview.json"))

        def preview(source):
            request = Request(
                root + "/api/workspaces/example/nodes/clean/preview",
                data=json.dumps({"path": source}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with build_opener(ProxyHandler({})).open(request) as response:
                return json.loads(response.read())

        for source in ("clean/POSCAR", "../POSCAR", "clean/task.md"):
            with pytest.raises(HTTPError) as invalid:
                preview(source)
            assert invalid.value.code == 400
        for name in ("POSCAR", "CONTCAR"):
            source = "clean/slab opt/" + name
            assert preview(source) == {"path": source + ".preview.json"}
            snapshot = get("/workspace-assets/example/clean/slab%20opt/" + name + ".preview.json")
            assert snapshot["formula"] == "Pt" and snapshot["atoms"] == 1
            from pymatgen.core import Structure

            restored = Structure.from_str(snapshot["cif"], fmt="cif")
            assert len(restored) == 1 and restored.lattice.a == pytest.approx(3)
            assert (calculation / name).read_bytes() == structure
        preview("clean/slab opt/POSCAR")
        assert len(list(calculation.glob("*.preview.json"))) == 2
        assert not list(calculation.glob("*.html"))
        assert len(get("/api/workspaces/example/nodes/clean")["inspection"]["structure_previews"]) == 2

        # Progress reads live output without waiting for the Runner snapshot.
        (calculation / ".job_id").write_text("123")
        (calculation / "OSZICAR").write_text("RMM: 269 -0.9947E+03 0.22E-01 0 10\n")
        (calculation / "slurm-123.out").write_text("<script>not markup</script>\n")
        route = "/api/workspaces/example/nodes/clean/progress?subdir=slab%20opt"
        progress = get(route)
        assert progress["job_id"] == "123"
        assert progress["history"]["electronic"][-1]["step"] == 269
        assert progress["history"]["electronic"][-1]["energy"] == pytest.approx(-994.7)
        assert progress["history"]["ionic"] == []
        assert progress["log_tail"] == "<script>not markup</script>"
        (calculation / "OSZICAR").write_text("RMM: 270 -0.9946E+03 0.1E+00 0 10\n")
        assert get(route)["history"]["electronic"][-1]["step"] == 270
        with pytest.raises(HTTPError) as escaped_progress:
            get("/api/workspaces/example/nodes/clean/progress?subdir=../../")
        assert escaped_progress.value.code == 400
        (calculation / "slurm-123.out").unlink()
        (calculation / "slurm-123.out").symlink_to(outside)
        with pytest.raises(HTTPError) as escaped_log:
            get(route)
        assert escaped_log.value.code == 400

        trace = get("/api/workspaces/example/trace?conversation=coordinator/subagents/modeler")
        assert trace["events"][0]["tool"] == "terminal"
        assert trace["events"][1]["content"] == "First line\nSecond line"
        assert "private reasoning" not in json.dumps(trace)

        # Exercise the real runner gate through HTTP, without LLM/Slurm calls.
        from src.graph.runner import SupervisorRunner

        runner = object.__new__(SupervisorRunner)
        runner.review_handler = partial(wait_for_web_review, workspace)
        (workspace / ".webui").mkdir(exist_ok=True)

        def post(payload):
            request = Request(
                root + "/api/workspaces/example/review",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with build_opener(ProxyHandler({})).open(request) as response:
                return json.loads(response.read())

        with ThreadPoolExecutor(max_workers=1) as pool:
            for decision in ("APPROVE", "REVISE"):
                pending = pool.submit(
                    runner.read_human_review_response, "node_result", "Check result", node_id="clean"
                )
                deadline = time.monotonic() + 5
                while not (workspace / ".webui" / "review.json").exists():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                detail = get("/api/workspaces/example")
                assert detail["summary"]["status"] == "awaiting_review"
                assert not pending.done()
                payload = {"id": detail["review"]["id"], "decision": decision, "feedback": "Check energy"}
                with pytest.raises(HTTPError) as stale:
                    post({**payload, "id": "old-request"})
                assert stale.value.code == 400
                assert not pending.done()
                assert post(payload) == {"submitted": True}
                with pytest.raises(HTTPError) as duplicate:
                    post(payload)
                assert duplicate.value.code == 400
                assert pending.result(timeout=5) == f"{decision} Check energy"
                assert not get("/api/workspaces/example")["review"]
    finally:
        store.events.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_launch_uses_the_actual_cli_workspace(tmp_path, monkeypatch):
    import src.execution.workflows as module

    calls = []

    def spawn(command, **kwargs):
        calls.append(command)
        return type("Process", (), {"pid": 123, "wait": lambda self: 0})()

    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    structure = b"Pt\n1.0\n3 0 0\n0 3 0\n0 0 3\nPt\n1\nDirect\n0 0 0\n"
    result = module.create_run(
        tmp_path, {
            "goal": "Optimize Pt",
            "workspace_name": "pt",
            "review_mode": "enabled",
            "structure": {"name": "../POSCAR", "content": base64.b64encode(structure).decode()},
        }
    )
    path = tmp_path / result["workspace_name"]
    assert Path(calls[0][-1]) == path
    assert json.loads((path / ".pipeline_context.json").read_text())["user_command"] == "Optimize Pt"
    assert json.loads((path / ".pipeline_context.json").read_text())["review_mode"] == "enabled"
    assert (path / ".webui" / "run.log").is_file()
    assert (path / "POSCAR").read_bytes() == structure
    assert json.loads((path / ".pipeline_context.json").read_text())["input_path"] == str(path / "POSCAR")

    import src.execution.workflows as lifecycle

    monkeypatch.setattr(lifecycle, "workflow_process", lambda path: None)
    (path / ".job_id").write_text("41")
    queue = [f"41|{path}", f"42|{path}/unrecorded", f"99|{tmp_path}/another_task"]
    cancellations = []

    def slurm(command, **kwargs):
        if command[0] == "squeue":
            return type("Result", (), {"stdout": "\n".join(queue)})()
        assert command == ["scancel", "41", "42"]
        cancellations.extend(command[1:])
        queue[:] = [queue[-1]]
        return type("Result", (), {"stdout": ""})()

    monkeypatch.setattr(lifecycle.subprocess, "run", slurm)
    with pytest.raises(ValueError, match="确认"):
        lifecycle.delete_workflow(path, "wrong-workspace")
    with pytest.raises(ValueError, match="仍在运行"):
        lifecycle.delete_workflow(path, path.name)
    assert path.is_dir()
    assert lifecycle.stop_workflow(path)["cancelled_jobs"] == ["41", "42"]
    assert cancellations == ["41", "42"]
    assert json.loads((path / ".webui" / "control.json").read_text())["status"] == "stopped"
    assert lifecycle.delete_workflow(path, path.name) == {"deleted": True}
    assert not path.exists()


def test_progress_history_keeps_step_numbers_and_separates_ionic_energy():
    text = "\n".join(f"RMM: {step} -0.9947E+03 0.22E-01 0 10" for step in range(1, 401))
    history = iteration_history(text)
    assert len(history["electronic"]) == 120
    assert history["electronic"][-1]["step"] == 400
    history = iteration_history(text + "\n 1 F= -995 E0= -996\nDAV: 1 0.468836D+05 0.1E+00\nRMM: 2 **** NaN\n")
    assert history["ionic"] == [{"step": 1, "energy": -996}]
    assert len(history["electronic"]) == 1
    assert history["electronic"][0]["energy"] == pytest.approx(46883.6)


def test_web_restart_does_not_interrupt_execution(tmp_path, monkeypatch):
    from src.execution import workflows
    from src.execution.service import ExecutionRequestHandler

    config = tmp_path / "config.yaml"
    config.write_text(json.dumps({"agents": {
        "supervisor": {"url": "https://default.example/v1", "api_key": "default-key", "model": "default-model"},
        "analyzer": {"url": "https://other.example/v1", "api_key": "other-key", "model": "other-model"},
    }}))
    monkeypatch.setenv("CATPAW_CONFIG", str(config))
    real_popen = subprocess.Popen
    workers = []
    # Substitute only the expensive pipeline; keep the real worker entry point.
    worker_code = """
import argparse
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
def parser():
    result = argparse.ArgumentParser()
    result.add_argument("goal")
    result.add_argument("--review", action="store_true")
    result.add_argument("--no-review", action="store_true")
    return result
def pipeline(args, *, prepared_workspace, review_handler):
    from src.utils.config import load_config
    for role, agent in load_config()["agents"].items():
        assert agent["url"] == "https://llm.example.test/v1"
        assert agent["api_key"] == "test-task-key"
        assert agent["model"] == ("openai/analyzer-model" if role == "analyzer" else "openai/test-model")
    response = review_handler("node_result", "Isolation check")
    Path(prepared_workspace[0], "completed").write_text(response)
sys.modules["src.main"] = SimpleNamespace(build_parser=parser, run_pipeline_command=pipeline)
runpy.run_module("src.execution.service", run_name="__main__")
"""

    def spawn(command, **kwargs):
        assert command[2] == "src.execution.service"
        process = real_popen([sys.executable, "-c", worker_code, *command[-2:]], **kwargs)
        workers.append(process)
        return process

    monkeypatch.setattr(workflows.subprocess, "Popen", spawn)
    backend = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(ExecutionRequestHandler, workspace_root=tmp_path, command_lock=threading.Lock()),
    )
    backend_thread = threading.Thread(target=backend.serve_forever, daemon=True)
    backend_thread.start()
    web = None

    def start_web():
        process = real_popen(
            [sys.executable, "-u", "-m", "src.web.dashboard", "--port", "0",
             "--workspace-root", str(tmp_path), "--executor-url", f"http://127.0.0.1:{backend.server_port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        return process

    def web_url(process):
        assert select.select([process.stdout], [], [], 30)[0], "Web failed to start"
        line = process.stdout.readline()
        assert line.startswith("CatPaw dashboard:"), line
        return line.strip().split()[-1]

    def post(url, route, payload):
        request = Request(url + route, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            return json.loads(response.read())

    try:
        web = start_web()
        root = web_url(web)
        with pytest.raises(HTTPError) as incomplete:
            post(root, "/api/runs", {"goal": "Incomplete", "agent_connections": {"all": {"base_url": "https://llm.example.test/v1"}}})
        assert incomplete.value.code == 400
        assert not workers
        result = post(root, "/api/runs", {
            "goal": "Isolation", "review_mode": "enabled",
            "agent_connections": {
                "all": {"base_url": "https://llm.example.test/v1", "api_key": "test-task-key", "model": "openai/test-model"},
                "analyzer": {"base_url": "https://llm.example.test/v1", "api_key": "test-task-key", "model": "openai/analyzer-model"},
            },
        })
        path = tmp_path / result["workspace_name"]
        deadline = time.monotonic() + 10
        while not (path / ".webui" / "review.json").is_file():
            assert workers[0].poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        worker_pid = json.loads((path / ".webui" / "run.json").read_text())["pid"]
        web.kill()
        web.wait(timeout=5)
        assert workers[0].poll() is None
        web.stdout.close()
        web = start_web()
        root = web_url(web)
        request = json.loads((path / ".webui" / "review.json").read_text())
        assert post(root, f"/api/workspaces/{path.name}/review", {"id": request["id"], "decision": "APPROVE"}) == {"submitted": True}
        assert workers[0].wait(timeout=10) == 0
        assert (path / "completed").read_text() == "APPROVE"
        for file in (path / ".pipeline_context.json", path / ".webui" / "run.json", path / ".webui" / "run.log"):
            assert "test-task-key" not in file.read_text()
        assert json.loads((path / ".webui" / "run.json").read_text())["pid"] == worker_pid
        backend.shutdown()
        backend.server_close()
        with pytest.raises(HTTPError) as unavailable:
            post(root, "/api/runs", {"goal": "Must not start"})
        assert unavailable.value.code == 502
        assert len(workers) == 1
        with build_opener(ProxyHandler({})).open(root + "/api/workspaces", timeout=5) as response:
            assert response.status == 200
    finally:
        if web is not None:
            web.terminate()
            web.wait(timeout=5)
            web.stdout.close()
        for process in workers:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        backend.shutdown()
        backend.server_close()
        backend_thread.join()
