"""Independent workflow control service and worker entry point."""

import argparse
import fcntl
import json
import subprocess
import threading
from _thread import LockType
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from src.execution.workflows import create_run, delete_workflow, stop_workflow, wait_for_web_review
from src.utils.project_env import workspace_root as default_root
from src.utils.results import write_json


class ExecutionRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args: Any, workspace_root: Path, command_lock: LockType, **kwargs: Any) -> None:
        self.workspace_root = workspace_root
        self.command_lock = command_lock
        super().__init__(*args, **kwargs)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def do_POST(self) -> None:
        try:
            if self.headers.get("X-Workspace-Root") != str(self.workspace_root):
                raise ValueError("Web 与执行服务的 workspace-root 不一致")
            length = int(self.headers.get("Content-Length", "0"))
            limit = 14 * 1024 * 1024 if self.path == "/api/runs" else 65536
            if not 0 < length <= limit:
                raise ValueError(f"Request body must be between 1 and {limit} bytes")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/")]
            # Serialize mutations, including PID publication before stop/delete.
            with self.command_lock:
                if parts == ["api", "runs"]:
                    result = create_run(self.workspace_root, payload)
                    status = 201
                elif (
                    len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] in {"stop", "delete"}
                ):
                    path = (self.workspace_root / parts[2]).resolve()
                    if path.parent != self.workspace_root or not path.is_dir():
                        raise FileNotFoundError("Workspace not found")
                    result = (
                        stop_workflow(path)
                        if parts[3] == "stop"
                        else delete_workflow(path, payload.get("confirmation"))
                    )
                    status = 200
                else:
                    return self.send_json({"error": "Not found"}, 404)
            self.send_json(result, status)
        except FileNotFoundError as exc:
            self.send_json({"error": str(exc)}, 404)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.send_json({"error": str(exc)}, 409)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            self.send_json({"error": str(exc)}, 400)


def run_workspace(path: Path) -> None:
    # Keep the SDK and scientific dependencies out of the control service.
    from src.main import build_parser, run_pipeline_command

    context = json.loads((path / ".pipeline_context.json").read_text())
    command = build_parser().parse_args(
        [context["user_command"], "--review" if context["review_mode"] == "enabled" else "--no-review"]
    )
    try:
        run_pipeline_command(
            command,
            prepared_workspace=(str(path), context.get("input_path")),
            review_handler=partial(wait_for_web_review, path),
        )
    except Exception as exc:
        # Persist failure for the dashboard, then retain the original traceback.
        write_json(path / ".webui" / "control.json", {"status": "failed", "error": str(exc)})
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="CatPaw workflow execution service")
    parser.add_argument("--port", type=int, default=8124)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--run-workspace", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.run_workspace:
        run_workspace(args.run_workspace)
        return
    root = (args.workspace_root or default_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".execution.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error(f"An execution service already owns {root}")
        handler = partial(ExecutionRequestHandler, workspace_root=root, command_lock=threading.Lock())
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            print(f"CatPaw execution service: http://127.0.0.1:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
