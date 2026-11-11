#!/usr/bin/env python3
"""Run CatPaw benchmark cases that do not already have terminal runner state."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

BENCHMARK_RESULT_FILENAME = "benchmark_result.json"
TERMINAL_STATUSES = {"OK", "FAILED", "TIMEOUT", "KILLED"}
WORKSPACE_DISCOVERY_TIMEOUT_SECONDS = 300.0
DEFAULT_CATEGORIES = ["d_band_center", "band_gap", "adsorption_energy"]
DEFAULT_MAX_PARALLEL = "6"
DEFAULT_CASE_TIMEOUT = "5d"


def now_text() -> str:
    return datetime.now().strftime("%F %T")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_duration(value: str) -> float:
    text = value.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text)
    if not match:
        raise ValueError(f"invalid duration {value!r}; expected formats like 30s, 90m, 2h, 3d")
    number = float(match.group(1))
    unit = match.group(2) or "s"
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return number * multipliers[unit]


def env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser().resolve()


def sanitize_workspace_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return cleaned or "unnamed"


def safe_state_name(category: str, case_name: str) -> str:
    raw = f"{category}__{case_name}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw) + ".json"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in {path}")
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def process_group_alive(pgid: int | None) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def send_group_signal(pgid: int, sig: signal.Signals) -> bool:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


async def stop_process_group(pgid: int, grace_seconds: float) -> str:
    if not process_group_alive(pgid):
        return "not-running"
    send_group_signal(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not process_group_alive(pgid):
            return "terminated"
        await asyncio.sleep(0.5)
    if process_group_alive(pgid):
        send_group_signal(pgid, signal.SIGKILL)
        return "killed"
    return "terminated"


def resolve_python(catpaw_root: Path) -> Path | None:
    explicit = os.environ.get("PYTHON_BIN")
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None

    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        candidate = Path(conda_prefix) / "bin" / "python"
        if candidate.is_file():
            return candidate.resolve()

    conda_env = os.environ.get("CONDA_ENV", "catagent")
    if conda_env and shutil.which("conda"):
        result = subprocess.run(
            ["conda", "run", "-n", conda_env, "python", "-c", "import sys; print(sys.executable)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        conda_python = result.stdout.strip()
        if result.returncode == 0 and conda_python:
            candidate = Path(conda_python)
            if candidate.is_file():
                return candidate.resolve()

    venv_python = catpaw_root / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return venv_python

    for executable in ("python", "python3"):
        resolved = shutil.which(executable)
        if resolved:
            return Path(resolved).resolve()
    return None


@dataclass(frozen=True)
class Config:
    catpaw_root: Path
    benchmark_root: Path
    workspace_root: Path
    log_dir: Path
    state_dir: Path
    python_bin: Path
    max_parallel: int
    case_timeout: str
    case_timeout_seconds: float
    kill_grace: str
    kill_grace_seconds: float
    categories: list[str]


@dataclass(frozen=True)
class BenchmarkCase:
    category: str
    json_path: Path
    case_name: str
    user_prompt: str
    workspace: str
    benchmark_result: dict[str, Any] | None
    log_path: Path
    state_path: Path

    @property
    def label(self) -> str:
        return f"{self.category}/{self.case_name}"

    @property
    def state_label(self) -> str:
        return f"{self.category}__{self.case_name}"

    @property
    def workspace_prefix(self) -> str:
        return sanitize_workspace_name(self.workspace) + "_"


class Runner:
    def __init__(self, config: Config, *, rerun_all: bool, rerun_failed: bool) -> None:
        self.config = config
        self.rerun_all = rerun_all
        self.rerun_failed = rerun_failed
        self.running: dict[str, tuple[BenchmarkCase, asyncio.subprocess.Process]] = {}
        self.shutdown_requested = False

    async def launch_all(self) -> None:
        cases = discover_cases(self.config)
        runnable: list[BenchmarkCase] = []
        skipped = 0

        for case in cases:
            decision = self.skip_decision(case)
            if decision:
                print(f"  skip    {case.label}  ({decision})")
                skipped += 1
            else:
                runnable.append(case)

        print()
        print(f"Runnable: {len(runnable)}   Skipped: {skipped}")
        print(f"Project root: {self.config.catpaw_root}")
        print(f"Benchmark root: {self.config.benchmark_root}")
        print(f"Workspace root: {self.config.workspace_root}")
        print(f"Log dir : {self.config.log_dir}")
        print(f"State dir: {self.config.state_dir}")
        print(f"Python  : {self.config.python_bin}")
        print(f"Categories: {' '.join(self.config.categories)}")
        print(f"Concurrency: {self.config.max_parallel} cases  (override: MAX_PARALLEL=N)")
        print(f"Per-case timeout: {self.config.case_timeout}  (override: CASE_TIMEOUT=12h)")
        print()
        print("Status   : python benchmark/run_benchmarks.py --status")
        print("Kill all : python benchmark/run_benchmarks.py --kill")
        print()

        if not runnable:
            return

        semaphore = asyncio.Semaphore(self.config.max_parallel)
        tasks = [asyncio.create_task(self.run_case(case, semaphore)) for case in runnable]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            await self.shutdown_running()
            raise
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError(f"{len(failures)} benchmark case(s) failed") from failures[0]

    def skip_decision(self, case: BenchmarkCase) -> str | None:
        state = read_state(case.state_path)
        status = state.get("status")
        pgid = as_int(state.get("pgid"))

        if status == "RUNNING" and process_group_alive(pgid):
            return f"already running pgid={pgid}"
        if status == "RUNNING" and state:
            state["status"] = "STALE"
            state["stale_at"] = now_iso()
            write_json_atomic(case.state_path, state)

        if self.rerun_all:
            return None
        if self.rerun_failed and status == "OK":
            return "previous status=OK"
        if self.rerun_failed and status in TERMINAL_STATUSES - {"OK"}:
            return None
        if status in TERMINAL_STATUSES:
            return f"previous status={status}"
        return None

    async def run_case(self, case: BenchmarkCase, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            if self.shutdown_requested:
                raise asyncio.CancelledError
            await self.execute_case(case)

    async def execute_case(self, case: BenchmarkCase) -> None:
        env = os.environ.copy()
        gcc_lib_override = os.environ.get("CATPAW_GCC_LIBSTDCPP")
        if gcc_lib_override:
            gcc_lib = Path(gcc_lib_override).expanduser()
            if gcc_lib.is_dir():
                gcc_lib_text = str(gcc_lib)
                existing_ld = env.get("LD_LIBRARY_PATH", "")
                ld_parts = [part for part in existing_ld.split(":") if part and part != gcc_lib_text]
                env["LD_LIBRARY_PATH"] = ":".join([gcc_lib_text, *ld_parts])
        cmd = [
            str(self.config.python_bin),
            "-m",
            "src.main",
            case.user_prompt,
            "-w",
            case.workspace,
        ]

        case.log_path.parent.mkdir(parents=True, exist_ok=True)
        state = base_state(case, self.config)
        print(f"  launch  {case.label}")
        discovery_not_before = time.time()

        with case.log_path.open("a", encoding="utf-8") as log:
            write_log_header(log, case, self.config)
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.config.catpaw_root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            pgid = proc.pid
            state.update({"pid": proc.pid, "pgid": pgid, "status": "RUNNING"})
            write_json_atomic(case.state_path, state)
            self.running[case.state_label] = (case, proc)

            workspace_path = await self.inject_benchmark_result(case, proc, state, log, discovery_not_before)

            timed_out = False
            forced_kill = False
            try:
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout=self.config.case_timeout_seconds)
                except TimeoutError:
                    timed_out = True
                    send_group_signal(pgid, signal.SIGTERM)
                    try:
                        rc = await asyncio.wait_for(proc.wait(), timeout=self.config.kill_grace_seconds)
                    except TimeoutError:
                        forced_kill = True
                        send_group_signal(pgid, signal.SIGKILL)
                        rc = await proc.wait()
            finally:
                self.running.pop(case.state_label, None)

            status = status_from_returncode(rc, timed_out=timed_out)
            if forced_kill:
                status_note = "TIMEOUT+KILLED"
            elif timed_out:
                status_note = f"TIMEOUT (SIGTERM at {self.config.case_timeout})"
            else:
                status_note = status

            result_snapshot = read_benchmark_result_snapshot(workspace_path)
            state.update(
                {
                    "status": status,
                    "exit_code": rc,
                    "timed_out": timed_out,
                    "forced_kill": forced_kill,
                    "ended_at": now_iso(),
                }
            )
            if result_snapshot is not None:
                state["benchmark_result"] = result_snapshot
            write_json_atomic(case.state_path, state)
            write_log_footer(log, case, rc, status_note, result_snapshot)

        print(f"  done    {case.label}  exit={rc}  {status_note}")
        if rc != 0:
            raise RuntimeError(f"{case.label} failed: {status_note} (exit {rc})")

    async def inject_benchmark_result(
        self,
        case: BenchmarkCase,
        proc: asyncio.subprocess.Process,
        state: dict[str, Any],
        log,
        not_before: float,
    ) -> Path | None:
        workspace_path = await wait_for_case_workspace(
            self.config.workspace_root,
            case.workspace_prefix,
            not_before=not_before,
            proc=proc,
            timeout_seconds=WORKSPACE_DISCOVERY_TIMEOUT_SECONDS,
        )
        if workspace_path is None:
            message = (
                f"Could not locate workspace with prefix {case.workspace_prefix!r}; "
                f"{BENCHMARK_RESULT_FILENAME} was not injected."
            )
            state["benchmark_result_error"] = message
            write_json_atomic(case.state_path, state)
            log.write(f"[{now_text()}] WARN  {message}\n")
            log.flush()
            return None

        state["workspace_path"] = str(workspace_path)
        result_path = workspace_path / BENCHMARK_RESULT_FILENAME
        state["benchmark_result_path"] = str(result_path)

        if case.benchmark_result is None:
            state["benchmark_result_injected"] = False
            state["benchmark_result_error"] = "test JSON has no benchmark_result object"
            write_json_atomic(case.state_path, state)
            log.write(f"[{now_text()}] WARN  test JSON has no benchmark_result object\n")
            log.flush()
            return workspace_path

        write_json_atomic(result_path, case.benchmark_result)
        state["benchmark_result_injected"] = True
        state["benchmark_result_injected_at"] = now_iso()
        write_json_atomic(case.state_path, state)
        log.write(f"[{now_text()}] Injected benchmark result template: {result_path}\n")
        log.flush()
        return workspace_path

    async def shutdown_running(self) -> None:
        self.shutdown_requested = True
        if not self.running:
            return
        print(f"\n[{now_text()}] shutting down {len(self.running)} running case(s)")
        for label, (case, proc) in list(self.running.items()):
            pgid = proc.pid
            outcome = await stop_process_group(pgid, self.config.kill_grace_seconds)
            state = read_state(case.state_path)
            state.update(
                {
                    "status": "KILLED",
                    "exit_code": None,
                    "kill_outcome": outcome,
                    "ended_at": now_iso(),
                }
            )
            write_json_atomic(case.state_path, state)
            print(f"  killed  {label}  pgid={pgid}  {outcome}")


async def wait_for_case_workspace(
    workspace_root: Path,
    prefix: str,
    *,
    not_before: float,
    proc: asyncio.subprocess.Process | None,
    timeout_seconds: float,
) -> Path | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        workspace_path = newest_matching_workspace(workspace_root, prefix, not_before=not_before)
        if workspace_path is not None:
            return workspace_path
        if proc is not None and proc.returncode is not None:
            return None
        await asyncio.sleep(0.5)
    return None


def newest_matching_workspace(workspace_root: Path, prefix: str, *, not_before: float) -> Path | None:
    if not workspace_root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for path in workspace_root.iterdir():
        if not path.is_dir() or not path.name.startswith(prefix):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= not_before - 2.0:
            candidates.append((mtime, path))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def read_benchmark_result_snapshot(workspace_path: Path | None) -> dict[str, Any] | None:
    if workspace_path is None:
        return None
    result_path = workspace_path / BENCHMARK_RESULT_FILENAME
    if not result_path.is_file():
        return None
    try:
        return load_json(result_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"_error": f"could not read {result_path}"}


def status_from_returncode(rc: int, *, timed_out: bool) -> str:
    if timed_out:
        return "TIMEOUT"
    if rc == 0:
        return "OK"
    return "FAILED"


def base_state(case: BenchmarkCase, config: Config) -> dict[str, Any]:
    return {
        "case": case.state_label,
        "category": case.category,
        "case_name": case.case_name,
        "json_path": str(case.json_path),
        "workspace_arg": case.workspace,
        "workspace_path": None,
        "workspace_root": str(config.workspace_root),
        "log_path": str(case.log_path),
        "benchmark_result_path": None,
        "benchmark_result_injected": False,
        "pid": None,
        "pgid": None,
        "status": "STARTING",
        "exit_code": None,
        "started_at": now_iso(),
        "ended_at": None,
        "case_timeout": config.case_timeout,
        "kill_grace": config.kill_grace,
    }


def write_log_header(log, case: BenchmarkCase, config: Config) -> None:
    log.write("==========================================================\n")
    log.write(f"[{now_text()}] START  {case.category} / {case.case_name}\n")
    log.write(f"Project root   : {config.catpaw_root}\n")
    log.write(f"Benchmark JSON : {case.json_path}\n")
    log.write(f"Workspace arg  : {case.workspace}\n")
    log.write(f"Prompt         : {case.user_prompt}\n")
    log.write(f"Timeout        : {config.case_timeout}  (SIGKILL after +{config.kill_grace})\n")
    log.write("----------------------------------------------------------\n")
    log.flush()


def write_log_footer(
    log, case: BenchmarkCase, rc: int, status_note: str, result_snapshot: dict[str, Any] | None
) -> None:
    if result_snapshot is not None:
        log.write("----------------------------------------------------------\n")
        log.write("Final benchmark_result.json:\n")
        log.write(json.dumps(result_snapshot, indent=2, ensure_ascii=False) + "\n")
    log.write("----------------------------------------------------------\n")
    log.write(f"[{now_text()}] END    {case.category} / {case.case_name}  exit={rc}  {status_note}\n")
    log.write("==========================================================\n\n")
    log.flush()


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return load_json(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {"status": "STALE", "state_error": f"could not read {path}"}


def as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def build_config() -> Config:
    script_dir = Path(__file__).resolve().parent
    catpaw_root = env_path("CATPAW", script_dir.parent)
    benchmark_root = env_path("BENCHMARK_ROOT", catpaw_root / "benchmark")
    workspace_root = catpaw_root / "workspace"
    log_dir = env_path("LOG_DIR", workspace_root / "_benchmark_logs")
    state_dir = env_path("PID_DIR", log_dir / ".py_state")

    python_bin = resolve_python(catpaw_root)
    if python_bin is None:
        raise RuntimeError("No usable Python found; activate an environment or set PYTHON_BIN")

    max_parallel = int(os.environ.get("MAX_PARALLEL", DEFAULT_MAX_PARALLEL))
    case_timeout = os.environ.get("CASE_TIMEOUT", DEFAULT_CASE_TIMEOUT)
    kill_grace = os.environ.get("KILL_GRACE", "30s")
    categories_env = os.environ.get("BENCHMARK_CATEGORIES")
    categories = categories_env.split() if categories_env else DEFAULT_CATEGORIES

    return Config(
        catpaw_root=catpaw_root,
        benchmark_root=benchmark_root,
        workspace_root=workspace_root,
        log_dir=log_dir,
        state_dir=state_dir,
        python_bin=python_bin,
        max_parallel=max_parallel,
        case_timeout=case_timeout,
        case_timeout_seconds=parse_duration(case_timeout),
        kill_grace=kill_grace,
        kill_grace_seconds=parse_duration(kill_grace),
        categories=categories,
    )


def discover_benchmark_categories(benchmark_root: Path) -> list[str]:
    if not benchmark_root.is_dir():
        return []
    categories: list[str] = []
    for path in sorted(benchmark_root.iterdir()):
        if (path / "test").is_dir():
            categories.append(path.name)
    return categories


def preflight(config: Config) -> None:
    errors: list[str] = []
    if not config.catpaw_root.is_dir():
        errors.append(f"expected CatPaw directory not found: {config.catpaw_root}")
    if not config.benchmark_root.is_dir():
        errors.append(f"expected benchmark root not found: {config.benchmark_root}")
    if not (config.catpaw_root / "src" / "main.py").is_file():
        errors.append(f"{config.catpaw_root}/src/main.py not found -- CATPAW is invalid")
    if config.max_parallel < 1:
        errors.append("MAX_PARALLEL must be >= 1")
    if not python_is_supported(config.python_bin):
        errors.append(f"CatPaw requires Python >= 3.12, but selected interpreter is {config.python_bin}")
    if errors:
        raise RuntimeError("Benchmark preflight failed:\n- " + "\n- ".join(errors))
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)


def python_is_supported(python_bin: Path) -> bool:
    result = subprocess.run(
        [str(python_bin), "-c", "import sys; assert sys.version_info >= (3, 12)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def resolved_categories(config: Config) -> list[str]:
    categories: list[str] = []
    for requested in config.categories:
        category = requested
        if (config.benchmark_root / category / "test").is_dir():
            categories.append(category)
        else:
            print(
                f"WARN: skip missing benchmark category {requested!r} "
                f"(expected {config.benchmark_root / category / 'test'})",
                file=sys.stderr,
            )
    return categories


def discover_cases(config: Config) -> list[BenchmarkCase]:
    cases: list[BenchmarkCase] = []
    for category in resolved_categories(config):
        for json_path in sorted((config.benchmark_root / category / "test").glob("*.json")):
            payload = load_json(json_path)
            case_name = json_path.stem
            log_path = config.log_dir / f"{category}__{case_name}.log"
            state_path = config.state_dir / safe_state_name(category, case_name)
            benchmark_result = payload.get("benchmark_result")
            cases.append(
                BenchmarkCase(
                    category=category,
                    json_path=json_path,
                    case_name=case_name,
                    user_prompt=str(payload["user_prompt"]),
                    workspace=str(payload["workspace"]),
                    benchmark_result=benchmark_result if isinstance(benchmark_result, dict) else None,
                    log_path=log_path,
                    state_path=state_path,
                )
            )
    return cases


def print_status(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    state_files = sorted(config.state_dir.glob("*.json"))
    print("=== benchmark runner case state ===")
    if not state_files:
        print("(no benchmark runner state)")
        return

    running = 0
    stale = 0
    terminal = 0
    for path in state_files:
        state = read_state(path)
        status = state.get("status", "UNKNOWN")
        pgid = as_int(state.get("pgid"))
        if status == "RUNNING" and process_group_alive(pgid):
            running += 1
            live_status = "RUNNING"
        elif status == "RUNNING":
            stale += 1
            live_status = "STALE"
        elif status in TERMINAL_STATUSES:
            terminal += 1
            live_status = status
        else:
            live_status = status
        label = state.get("case", path.stem)
        log_path = state.get("log_path", "")
        workspace_path = state.get("workspace_path") or "-"
        print(f"{live_status:8} {label}  pgid={pgid or '-'}  workspace={workspace_path}  log={log_path}")

    print()
    print(f"Running: {running}   Terminal: {terminal}   Stale: {stale}")


async def kill_all(config: Config) -> None:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    state_files = sorted(config.state_dir.glob("*.json"))
    killed = 0
    found = 0
    for path in state_files:
        state = read_state(path)
        if state.get("status") != "RUNNING":
            continue
        pgid = as_int(state.get("pgid"))
        label = state.get("case", path.stem)
        if pgid and process_group_alive(pgid):
            found += 1
            outcome = await stop_process_group(pgid, config.kill_grace_seconds)
            state.update(
                {
                    "status": "KILLED",
                    "exit_code": None,
                    "kill_outcome": outcome,
                    "ended_at": now_iso(),
                }
            )
            write_json_atomic(path, state)
            killed += 1
            print(f"  killed  {label}  pgid={pgid}  {outcome}")
        else:
            state.update({"status": "STALE", "stale_at": now_iso()})
            write_json_atomic(path, state)
            print(f"  stale   {label}  pgid={pgid or '-'}")
    if found == 0:
        print("(no running benchmark case processes)")
    if killed != found:
        raise RuntimeError(f"Stopped {killed} of {found} running benchmark cases")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run CatPaw benchmark cases without terminal runner state.")
    parser.add_argument("command", nargs="?", default="launch", choices=["launch"], help="launch benchmark cases")
    parser.add_argument("--status", action="store_true", help="show benchmark runner case state")
    parser.add_argument("--kill", action="store_true", help="terminate all running benchmark process groups")
    parser.add_argument("--rerun-all", action="store_true", help="ignore terminal state and rerun every selected case")
    parser.add_argument(
        "--rerun-failed", action="store_true", help="rerun FAILED/TIMEOUT/KILLED cases but skip OK cases"
    )
    return parser


async def async_main(argv: list[str]) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.rerun_all and args.rerun_failed:
        raise ValueError("--rerun-all and --rerun-failed are mutually exclusive")

    config = build_config()
    if args.status:
        print_status(config)
    elif args.kill:
        await kill_all(config)
    else:
        preflight(config)
        runner = Runner(config, rerun_all=args.rerun_all, rerun_failed=args.rerun_failed)
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()

        def request_shutdown() -> None:
            runner.shutdown_requested = True
            if main_task is not None:
                main_task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, request_shutdown)
            except NotImplementedError:
                pass

        try:
            await runner.launch_all()
        except asyncio.CancelledError:
            await runner.shutdown_running()
            raise


def main() -> None:
    asyncio.run(async_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
