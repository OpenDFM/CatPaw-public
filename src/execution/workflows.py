"""Launch, control and review independently running workflows."""

import base64
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil

from src.utils.config import load_config
from src.utils.project_env import project_root, setup_workspace
from src.utils.results import write_json, write_pipeline_context


def workflow_process(path: Path) -> psutil.Process | None:
    record = path / ".webui" / "run.json"
    if not record.is_file():
        raise ValueError("此任务由 CLI 启动，请在原终端管理运行进程")
    pid = json.loads(record.read_text())["pid"]
    try:
        process = psutil.Process(pid)
        if process.cmdline()[-2:] == ["--run-workspace", str(path)]:
            return process
    except psutil.NoSuchProcess:
        pass
    return None


def active_jobs(path: Path) -> list[str]:
    recorded = {file.read_text().strip() for file in path.rglob(".job_id")}
    result = subprocess.run(
        ["squeue", "--user", getpass.getuser(), "--noheader", "--format=%i|%Z"],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    jobs = []
    for line in result.stdout.splitlines():
        job_id, directory = line.strip().split("|", 1)
        if job_id in recorded or Path(directory).resolve().is_relative_to(path):
            jobs.append(job_id)
    return jobs


def stop_workflow(path: Path) -> dict[str, Any]:
    process = workflow_process(path)
    control = path / ".webui" / "control.json"
    write_json(control, {"status": "stopping"})
    try:
        if process:
            processes = [process, *process.children(recursive=True)]
            for item in processes:
                try:
                    item.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(processes, timeout=3)
            for item in alive:
                item.kill()
            _, alive = psutil.wait_procs(alive, timeout=3)
            if alive:
                raise RuntimeError("工作流进程尚未退出，请重试停止")
        jobs = active_jobs(path)
        if jobs:
            subprocess.run(["scancel", *jobs], capture_output=True, text=True, check=True, timeout=15)
            deadline = time.monotonic() + 10
            while active_jobs(path):
                if time.monotonic() >= deadline:
                    raise RuntimeError("已请求取消 Slurm 作业，仍在等待退出；请稍后重试停止")
                time.sleep(0.5)
        for name in ("review.json", "review_response.json"):
            (path / ".webui" / name).unlink(missing_ok=True)
        write_json(control, {"status": "stopped"})
        return {"stopped": True, "cancelled_jobs": jobs}
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        write_json(control, {"status": "stop_failed", "error": str(exc)})
        raise


def delete_workflow(path: Path, confirmation: str) -> dict[str, bool]:
    if confirmation != path.name:
        raise ValueError("请确认要删除的工作区")
    if workflow_process(path) is not None or active_jobs(path):
        raise ValueError("任务仍在运行，请先停止工作流及 Slurm 作业")
    shutil.rmtree(path)
    return {"deleted": True}


def create_run(workspace_root: Path, payload: dict[str, Any]) -> dict[str, str]:
    goal = payload.get("goal", "").strip()
    if not goal:
        raise ValueError("Research goal is required")
    review_mode = payload.get("review_mode", "disabled")
    if review_mode not in {"enabled", "disabled"}:
        raise ValueError("Invalid review mode")
    name = payload.get("workspace_name") or "web_run"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Workspace name must contain only letters, digits, underscores or hyphens")
    connections = payload.get("agent_connections", {})
    if not isinstance(connections, dict):
        raise ValueError("Invalid agent connections")
    allowed_roles = {"all", *load_config().get("agents", {})}
    llm = {}
    for role, connection in connections.items():
        if role not in allowed_roles or not isinstance(connection, dict):
            raise ValueError("Invalid agent connection")
        values = {}
        for key in ("base_url", "api_key", "model"):
            value = connection.get(key, "")
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{role}: 请完整填写 Base URL、API Key 和 Model")
            values[key] = value.strip()
        url = urlparse(values.pop("base_url"))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Base URL 必须是无内嵌凭据的 HTTP(S) 地址")
        llm[role] = {"url": url.geturl(), **values}
    upload = payload.get("structure")
    with tempfile.TemporaryDirectory(prefix="catpaw-upload-") as temporary:
        input_file = None
        if upload:
            filename = Path(upload["name"].replace("\\", "/")).name
            if not filename or filename.startswith("."):
                raise ValueError("Invalid structure filename")
            data = base64.b64decode(upload["content"], validate=True)
            if not 0 < len(data) <= 10 * 1024 * 1024:
                raise ValueError("结构文件应为 1 字节至 10 MB")
            input_file = Path(temporary) / filename
            input_file.write_bytes(data)
        workspace, _ = setup_workspace(
            input_file=str(input_file) if input_file else None,
            workspace_name=name,
            workspace_base=workspace_root,
        )
    path = Path(workspace)
    original = str(path / filename) if upload else None
    write_pipeline_context(workspace, user_command=goal, input_path=original, review_mode=review_mode)
    logs = path / ".webui"
    logs.mkdir()
    env = os.environ.copy()
    env["CATPAW_AGENT_CONNECTIONS"] = json.dumps(llm)
    with (logs / "run.log").open("a") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "src.execution.service", "--run-workspace", workspace],
            cwd=project_root(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    write_json(logs / "run.json", {"pid": process.pid})
    threading.Thread(target=process.wait, daemon=True).start()
    return {"workspace_name": path.name}


def wait_for_web_review(workspace: Path, kind: str, prompt: str, **context: Any) -> str:
    path = workspace / ".webui"
    request = {"id": uuid.uuid4().hex, "kind": kind, "prompt": prompt, **context}
    write_json(path / "review.json", request)
    response_path = path / "review_response.json"
    while not response_path.exists():
        time.sleep(1)
    response = json.loads(response_path.read_text())
    if response["id"] != request["id"]:
        raise ValueError("Review response does not match pending request")
    (path / "review.json").unlink()
    response_path.unlink()
    return response["response"]
