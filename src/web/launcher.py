"""Start the dashboard and execution service from one terminal."""

from __future__ import annotations

import argparse
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path


def check_port(host: str, port: int) -> None:
    """Fail before launching if a requested listening address is unavailable."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))


def stop_services(processes: list[subprocess.Popen]) -> None:
    # Signal only the services we started, never their independent workflow workers.
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_services(commands: list[tuple[str, list[str]]], *, cwd: Path) -> int:
    processes: list[subprocess.Popen] = []
    stopping = threading.Event()
    previous_handlers = {}

    def request_stop(signum, frame):
        stopping.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_stop)
        for name, command in commands:
            if stopping.is_set():
                return 0
            print(f"Starting {name}...", flush=True)
            processes.append(subprocess.Popen(command, cwd=cwd, start_new_session=True))
        while not stopping.wait(0.2):
            for (name, _), process in zip(commands, processes, strict=True):
                code = process.poll()
                if code is not None:
                    print(f"{name} exited (code {code}); stopping companion services.", file=sys.stderr)
                    return 1
        return 0
    except OSError as exc:
        print(f"Could not start CatPaw services: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            stop_services(processes)
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the CatPaw dashboard and execution service")
    parser.add_argument("--host", default="127.0.0.1", help="dashboard listening address")
    parser.add_argument("--port", type=int, default=8123, help="dashboard port")
    parser.add_argument("--executor-port", type=int, default=8124, help="local execution-service port")
    parser.add_argument("--workspace-root", type=Path)
    args = parser.parse_args(argv)
    if not all(1 <= port <= 65535 for port in (args.port, args.executor_port)):
        parser.error("ports must be between 1 and 65535")
    if args.port == args.executor_port:
        parser.error("dashboard and execution service need different ports")
    try:
        check_port(args.host, args.port)
        check_port("127.0.0.1", args.executor_port)
    except OSError as exc:
        parser.error(f"service address unavailable: {exc}; stop the existing service or choose another port")

    root = Path(__file__).resolve().parents[2]
    workspace_args = []
    if args.workspace_root is not None:
        workspace_args = ["--workspace-root", str(args.workspace_root.expanduser().resolve())]
    commands = [
        ("execution service", [
            sys.executable, "-m", "src.execution.service", "--port", str(args.executor_port), *workspace_args,
        ]),
        ("dashboard", [
            sys.executable, "-m", "src.web.dashboard", "--host", args.host, "--port", str(args.port),
            "--executor-url", f"http://127.0.0.1:{args.executor_port}", *workspace_args,
        ]),
    ]
    print("Press Ctrl+C to stop these services. Running calculations continue independently.", flush=True)
    return run_services(commands, cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
