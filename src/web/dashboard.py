"""Web projections and a proxy to the independent execution service."""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from markdown_it import MarkdownIt

from src.graph.state import DAGState
from src.utils.results import write_json
from src.web.calculation_progress import calculation_progress
from src.web.events import WorkspaceEvents

STATIC_DIR = Path(__file__).parent / "static"
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")
DOCUMENTS = (
    "task.md",
    "inputs.json",
    "modeling_summary.json",
    "modeling_validation.json",
    "parameter_summary.json",
    "parameter_validation.json",
    "runner_result.json",
    "node_result.json",
)


def read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text())


def tail(path: Path, size: int = 16000) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - size))
        return stream.read(size).decode("utf-8", errors="replace")


def contained(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Path must remain inside the selected workspace")
    return path


def workspace_summary(path: Path) -> dict:
    state = DAGState.load(str(path)) if DAGState.exists(str(path)) else None
    context = read_json(path / ".pipeline_context.json")
    control = read_json(path / ".webui" / "control.json")
    return {
        "name": path.name,
        "goal": state.user_goal if state else context.get("user_command", ""),
        "status": control.get("status")
        or (
            "awaiting_review"
            if (path / ".webui" / "review.json").exists()
            else (state.status if state else "initializing")
        ),
        "counts": dict(Counter(n.status for n in state.nodes)) if state else {},
        "report": (path / "final_report.md").is_file(),
    }


def conversations(path: Path) -> list[dict]:
    root = path / ".openhands" / "conversations"
    items = []
    for base in sorted(root.rglob("base_state.json")):
        data = read_json(base)
        relative = base.parent.relative_to(root)
        parent = relative.parent.parent.as_posix() if relative.parent.name == "subagents" else None
        directory = Path(data["workspace"]["working_dir"])
        events = sorted((base.parent / "events").glob("event-*.json"))
        items.append(
            {
                "id": relative.as_posix(),
                "parent": parent,
                "workspace": directory.name if directory != path else None,
                "agent": Path(data["agent"]["system_prompt_filename"]).stem,
                "status": data["execution_status"],
                "started_at": read_json(events[0])["timestamp"] if events else None,
                "last_event_at": read_json(events[-1])["timestamp"] if events else None,
            }
        )
    # Keep each call's descendants together, with siblings in start-time order.
    ids = {item["id"] for item in items}
    children: dict[str | None, list[dict]] = {}
    for item in sorted(items, key=lambda item: (item["started_at"] is None, item["started_at"] or "", item["id"])):
        parent = item["parent"] if item["parent"] in ids else None
        children.setdefault(parent, []).append(item)

    ordered = []

    def visit(parent):
        for item in children.get(parent, []):
            ordered.append(item)
            visit(item["id"])

    visit(None)
    return ordered


def trace(path: Path, conversation: str, before: int | None = None) -> dict:
    root = path / ".openhands" / "conversations"
    directory = contained(root, conversation)
    if not (directory / "base_state.json").is_file():
        raise FileNotFoundError("Conversation not found")
    files = sorted((directory / "events").glob("event-*.json"))
    if before is not None:
        files = [f for f in files if int(f.name.split("-")[1]) < before]
    selected = files[-100:]
    events = []
    for file in selected:
        data = read_json(file)
        # Only observable messages, tool requests/results and errors are projected.
        # SDK base_state contains model credentials and is never returned to clients.
        kind = data["kind"]
        if kind == "ActionEvent":
            content = data["action"]
        elif kind == "ObservationEvent":
            content = data["observation"]
        elif kind == "MessageEvent":
            content = data["llm_message"]
        elif kind in {"AgentErrorEvent", "ConversationErrorEvent"}:
            content = {k: data[k] for k in ("error", "message", "detail") if k in data}
        else:
            continue
        if kind in {"MessageEvent", "ObservationEvent"} and "content" in content:
            text = "\n".join(block["text"] for block in content["content"] if block["type"] == "text")
        else:
            text = json.dumps(
                {key: value for key, value in content.items() if key != "kind"}, ensure_ascii=False, indent=2
            )
        events.append(
            {
                "id": data["id"],
                "timestamp": data["timestamp"],
                "kind": kind,
                "tool": data.get("tool_name"),
                "call_id": data.get("tool_call_id"),
                "summary": data.get("summary", ""),
                "content": text[:20000],
                "truncated": len(text) > 20000,
            }
        )
    return {"events": events, "before": int(selected[0].name.split("-")[1]) if len(files) > len(selected) else None}


def node_detail(path: Path, node_id: str) -> dict:
    node = DAGState.load(str(path)).get_node(node_id)
    directory = contained(path, node.workspace_subdir)
    documents = {name: (directory / name).read_text() if (directory / name).is_file() else "" for name in DOCUMENTS}
    visuals = [
        p.relative_to(path).as_posix()
        for p in directory.rglob("*")
        if p.is_file()
        and p.suffix in {".png", ".svg", ".html"}
        and not any(part.startswith(".") for part in p.relative_to(path).parts)
    ]
    input_files = [
        p.relative_to(path).as_posix()
        for p in directory.rglob("*")
        if p.name in {"POSCAR", "CONTCAR", "INCAR", "KPOINTS"}
        and p.is_file()
        and p.resolve().is_relative_to(path.resolve())
        and not any(part.startswith(".") for part in p.relative_to(path).parts)
        and not any(part.startswith(".") for part in p.resolve().relative_to(path.resolve()).parts)
    ]
    structure_files = [name for name in input_files if Path(name).name in {"POSCAR", "CONTCAR"}]
    parameter_files = {}
    for name in sorted(input_files):
        source = path / name
        if source.name in {"INCAR", "KPOINTS", "POSCAR"}:
            subdir = source.parent.relative_to(directory).as_posix()
            files = parameter_files.setdefault(subdir, {})
            if source.name == "POSCAR":
                from pymatgen.io.vasp.inputs import Poscar

                try:
                    lattice = Poscar.from_file(source, check_for_potcar=False).structure.lattice
                    files["POSCAR"] = {"lengths": lattice.abc, "angles": lattice.angles}
                except (ValueError, IndexError) as error:
                    files["POSCAR"] = {"error": f"POSCAR 无法解析：{error}"}
            else:
                files[source.name] = source.read_text()
    runner = read_json(directory / "runner_result.json")
    monitor = read_json(directory / ".monitor_state.json")
    jobs = {job["subdir"]: job for job in runner.get("job_status", {}).get("jobs", [])}
    for subdir, job in monitor.get("jobs", {}).items():
        if subdir not in jobs or (job.get("last_checked") or "") > (jobs[subdir].get("last_checked") or ""):
            jobs[subdir] = job
    return {
        "node": node.to_dict(),
        "documents": documents,
        "document_html": {name: MARKDOWN.render(text) for name, text in documents.items() if name.endswith(".md")},
        "visuals": sorted(visuals)[:24],
        "inspection": {
            "structure_files": sorted(structure_files),
            "parameter_files": parameter_files,
            "structure_previews": {
                source: {"path": source + ".preview.json", "revision": preview.stat().st_mtime_ns}
                for source in structure_files
                if (preview := contained(path, source + ".preview.json")).is_file()
            },
            "modeling": read_json(directory / "modeling_summary.json"),
            "modeling_validation": read_json(directory / "modeling_validation.json"),
            "parameters": read_json(directory / "parameter_summary.json"),
            "parameter_validation": read_json(directory / "parameter_validation.json"),
            "result": read_json(directory / "node_result.json"),
            "jobs": list(jobs.values()),
            "authoritative_calculations": runner.get("authoritative_calculations", {}),
        },
        "conversations": [c for c in conversations(path) if c["workspace"] == directory.name],
    }


def workspace_detail(path: Path) -> dict:
    state = DAGState.load(str(path)) if DAGState.exists(str(path)) else None
    nodes = []
    if state:
        for node in state.nodes:
            nodes.append(
                {
                    "id": node.node_id,
                    "title": node.title,
                    "status": node.status,
                    "dependencies": node.dependencies,
                    "inputs": node.inputs,
                    "error": node.last_error,
                    "workspace_subdir": node.workspace_subdir,
                }
            )
    now = datetime.now(UTC).isoformat()
    api_waits = [
        payload
        for status_file in (path / ".webui" / "api_wait").glob("*.json")
        if (payload := read_json(status_file)).get("retry_at", "") > now
    ]
    return {
        "summary": workspace_summary(path),
        "nodes": nodes,
        "managed": (path / ".webui" / "run.json").is_file(),
        "control": read_json(path / ".webui" / "control.json"),
        "review": read_json(path / ".webui" / "review.json"),
        "review_submitted": (path / ".webui" / "review_response.json").exists(),
        "api_waits": sorted(api_waits, key=lambda item: item["retry_at"]),
        "weave": read_json(path / ".weave.json"),
        "conversations": conversations(path),
        "report": MARKDOWN.render(tail(path / "final_report.md", 200000)),
        "log": tail(path / ".webui" / "run.log"),
    }


class DashboardDataStore:
    def __init__(self, workspace_root: Path, executor_url: str = "http://127.0.0.1:8124") -> None:
        self.executor_url = executor_url.rstrip("/")
        self.workspace_root = workspace_root.resolve()
        self.review_lock = threading.Lock()
        self.events = WorkspaceEvents(self.workspace_root)

    def get_workspace(self, name: str) -> Path:
        path = contained(self.workspace_root, name)
        if path.parent != self.workspace_root or not path.is_dir():
            raise FileNotFoundError("Workspace not found")
        return path

    def build_index_payload(self) -> dict:
        paths = [
            p
            for p in self.workspace_root.iterdir()
            if p.is_dir() and ((p / ".dag_state.json").is_file() or (p / ".pipeline_context.json").is_file())
        ]
        return {
            "workspaces": [workspace_summary(p) for p in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)]
        }

    def build_workspace_payload(self, name: str) -> dict:
        return workspace_detail(self.get_workspace(name))

    def respond_to_review(self, name: str, payload: dict) -> dict:
        path = self.get_workspace(name) / ".webui"
        with self.review_lock:
            request = read_json(path / "review.json")
            if not request or payload.get("id") != request["id"]:
                raise ValueError("审核已结束或已更新，请刷新页面")
            if (path / "review_response.json").exists():
                raise ValueError("此审核已提交，请等待处理")
            decision = payload.get("decision")
            if decision not in {"APPROVE", "REVISE"}:
                raise ValueError("Invalid review decision")
            feedback = payload.get("feedback", "").strip()
            if decision == "REVISE" and not feedback:
                raise ValueError("请填写修改意见")
            write_json(
                path / "review_response.json",
                {
                    "id": request["id"],
                    "response": f"{decision} {feedback}".strip(),
                },
            )
        return {"submitted": True}


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, data_store: DashboardDataStore, **kwargs):
        self.data_store = data_store
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args) -> None:
        pass

    def send_json(self, data: dict, status: int = 200) -> None:
        self.send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def send(self, data: bytes, content_type: str, status: int = 200) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def send_file(self, path: Path) -> None:
        self.send(path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def send_events(self) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            for event in self.data_store.events.stream():
                message = f"data: {json.dumps(event)}\n\n" if event else ": keepalive\n\n"
                self.wfile.write(message.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/")]
        query = parse_qs(url.query)
        try:
            if url.path == "/api/events":
                return self.send_events()
            if url.path == "/api/workspaces":
                return self.send_json(self.data_store.build_index_payload())
            if parts[:2] == ["api", "workspaces"] and len(parts) >= 3:
                path = self.data_store.get_workspace(parts[2])
                if len(parts) == 3:
                    return self.send_json(workspace_detail(path))
                if parts[3] == "nodes" and len(parts) == 5:
                    return self.send_json(node_detail(path, parts[4]))
                if parts[3] == "nodes" and len(parts) == 6 and parts[5] == "progress":
                    node = DAGState.load(str(path)).get_node(parts[4])
                    return self.send_json(
                        calculation_progress(contained(path, node.workspace_subdir), query.get("subdir", ["."])[0])
                    )
                if parts[3] == "trace" and len(parts) == 4:
                    before = int(query["before"][0]) if "before" in query else None
                    return self.send_json(trace(path, query["conversation"][0], before))
            if parts[0] == "workspace-assets" and len(parts) >= 3:
                path = self.data_store.get_workspace(parts[1])
                relative = "/".join(parts[2:])
                target = contained(path, relative)
                if any(p.startswith(".") for p in target.relative_to(path).parts):
                    raise ValueError("Hidden runtime files are not downloadable")
                return self.send_file(target)
            if url.path in {"/", "/index.html"}:
                return self.send_file(STATIC_DIR / "index.html")
            if parts[0] == "static":
                return self.send_file(contained(STATIC_DIR, "/".join(parts[1:])))
            self.send_json({"error": "Not found"}, 404)
        except (FileNotFoundError, KeyError) as exc:
            self.send_json({"error": str(exc)}, 404)
        except (ValueError, TypeError) as exc:
            self.send_json({"error": str(exc)}, 400)

    def forward_command(self, payload: dict[str, Any]) -> None:
        request = Request(
            self.data_store.executor_url + self.path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Workspace-Root": str(self.data_store.workspace_root)},
        )
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=90) as response:
                self.send(response.read(), "application/json", response.status)
        except HTTPError as exc:
            self.send(exc.read(), "application/json", exc.code)
        except (URLError, TimeoutError, ConnectionError) as exc:
            self.send_json(
                {"error": f"执行服务不可用或响应中断，请检查服务及任务状态；不要重复提交：{exc}"},
                HTTPStatus.BAD_GATEWAY,
            )

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = 14 * 1024 * 1024 if self.path == "/api/runs" else 65536
            if not 0 < length <= limit:
                raise ValueError(f"Request body must be between 1 and {limit} bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]
            if parts == ["api", "runs"]:
                return self.forward_command(payload)
            if len(parts) == 6 and parts[:2] == ["api", "workspaces"] and parts[3] == "nodes" and parts[5] == "preview":
                from src.web.structure_preview import structure_snapshot

                path = self.data_store.get_workspace(parts[2])
                source = payload.get("path")
                if source not in node_detail(path, parts[4])["inspection"]["structure_files"]:
                    raise ValueError("请选择当前节点的 POSCAR 或 CONTCAR")
                output = contained(path, source + ".preview.json")
                write_json(output, structure_snapshot(contained(path, source)))
                return self.send_json({"path": source + ".preview.json"})
            if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "review":
                return self.send_json(self.data_store.respond_to_review(parts[2], payload))
            if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] in {"stop", "delete"}:
                return self.forward_command(payload)
            self.send_json({"error": "Not found"}, 404)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (OSError, RuntimeError) as exc:
            self.send_json({"error": str(exc)}, 409)
        except (ValueError, TypeError, AttributeError) as exc:
            self.send_json({"error": str(exc)}, 400)


def run_dashboard_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8123,
    workspace_root: str | Path | None = None,
    executor_url: str = "http://127.0.0.1:8124",
) -> None:
    from src.utils.project_env import workspace_root as default_root

    store = DashboardDataStore(Path(workspace_root) if workspace_root else default_root(), executor_url)
    server = ThreadingHTTPServer((host, port), partial(DashboardRequestHandler, data_store=store))
    store.events.start()
    print(f"CatPaw dashboard: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.events.close()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="CatPaw DAG and agent trace dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--executor-url", default="http://127.0.0.1:8124")
    args = parser.parse_args()
    run_dashboard_server(
        host=args.host, port=args.port, workspace_root=args.workspace_root, executor_url=args.executor_url,
    )


if __name__ == "__main__":
    main()
