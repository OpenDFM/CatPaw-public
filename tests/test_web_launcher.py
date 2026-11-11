"""Launcher lifecycle checks using lightweight service processes."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.web import launcher


class LauncherTests(unittest.TestCase):
    def test_arguments_use_same_python_and_workspace(self):
        with patch.object(launcher, "check_port"), patch.object(launcher, "run_services", return_value=0) as run:
            self.assertEqual(launcher.main([
                "--port", "9123", "--executor-port", "9124", "--workspace-root", "workspace/test",
            ]), 0)
        commands = run.call_args.args[0]
        for _, command in commands:
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(command[-2:], ["--workspace-root", str(Path("workspace/test").resolve())])
        self.assertIn("http://127.0.0.1:9124", commands[1][1])

    def test_occupied_port_does_not_start_services(self):
        with patch.object(launcher, "check_port", side_effect=OSError("Address already in use")):
            with patch.object(launcher, "run_services") as run:
                with self.assertRaises(SystemExit) as error:
                    launcher.main([])
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()

    def test_equal_ports_are_rejected(self):
        with patch.object(launcher, "run_services") as run:
            with self.assertRaises(SystemExit):
                launcher.main(["--port", "8124"])
            run.assert_not_called()

    def exercise_lifecycle(self, *, failure=False, stop_signal=signal.SIGINT):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # An independent worker stands in for a running VASP workflow.
            worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            service_code = (
                "import os,signal,sys,time; from pathlib import Path; "
                "signal.signal(signal.SIGINT, lambda *_: sys.exit(0)); "
                "Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )
            commands = [
                ("first", [sys.executable, "-c", service_code, str(root / "first")]),
                ("second", [sys.executable, "-c", service_code, str(root / "second")]),
            ]
            if failure:
                commands[1] = ("second", [sys.executable, "-c", "raise SystemExit(7)"])
            driver = (
                "import json,sys; from pathlib import Path; from src.web.launcher import run_services; "
                "raise SystemExit(run_services(json.loads(sys.argv[1]), cwd=Path(sys.argv[2])))"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", driver, json.dumps(commands), directory],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
            )
            try:
                if not failure:
                    deadline = time.monotonic() + 10
                    while not all((root / name).exists() for name in ("first", "second")):
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail("Services did not start")
                        time.sleep(0.02)
                    process.send_signal(stop_signal)
                output, errors = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 1 if failure else 0, output + errors)
                for name in ("first", "second"):
                    if (root / name).exists():
                        with self.assertRaises(ProcessLookupError):
                            os.kill(int((root / name).read_text()), 0)
                self.assertIsNone(worker.poll(), "Stopping services must preserve independent workers")
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=15)
                worker.terminate()
                worker.wait(timeout=5)

    def test_ctrl_c_stops_services(self):
        self.exercise_lifecycle()

    def test_sigterm_stops_services(self):
        self.exercise_lifecycle(stop_signal=signal.SIGTERM)

    def test_service_failure_stops_companion(self):
        self.exercise_lifecycle(failure=True)

    def test_spawn_failure_cleans_up_started_service(self):
        with tempfile.TemporaryDirectory() as directory:
            started = []
            popen = subprocess.Popen

            def track(*args, **kwargs):
                process = popen(*args, **kwargs)
                started.append(process)
                return process

            with patch.object(launcher.subprocess, "Popen", side_effect=track):
                code = launcher.run_services([
                    ("first", [sys.executable, "-c", "import time; time.sleep(60)"]),
                    ("missing", [str(Path(directory) / "missing-executable")]),
                ], cwd=Path(directory))
            self.assertEqual(code, 1)
            self.assertEqual(len(started), 1)
            self.assertIsNotNone(started[0].poll())


if __name__ == "__main__":
    unittest.main()
