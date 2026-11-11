"""One filesystem watcher broadcasts workspace changes to dashboard clients."""

import threading
from pathlib import Path

from watchfiles import watch


class WorkspaceEvents:
    def __init__(self, root: Path):
        self.root = root
        self.condition = threading.Condition()
        self.stopped = threading.Event()
        self.revision = 0
        self.workspaces = {}
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def close(self):
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join()

    def watch(self):
        for changes in watch(self.root, stop_event=self.stopped, debounce=300, step=100):
            names = set()
            for _, filename in changes:
                path = Path(filename)
                parts = path.relative_to(self.root).parts
                if parts and (
                    path.suffix in {".json", ".jsonl", ".md", ".log", ".html", ".png"}
                    or path.name in {"POSCAR", "CONTCAR", ".job_id"}
                ):
                    names.add(parts[0])
            if names:
                with self.condition:
                    self.revision += 1
                    self.workspaces.update(dict.fromkeys(names, self.revision))
                    self.condition.notify_all()

    def stream(self):
        # A fresh connection always synchronizes, including after reconnecting.
        with self.condition:
            revision = self.revision
        yield {"workspaces": None}
        while not self.stopped.is_set():
            with self.condition:
                self.condition.wait_for(
                    lambda revision=revision: self.revision != revision or self.stopped.is_set(), timeout=15
                )
                names = [name for name, changed in self.workspaces.items() if changed > revision]
                revision = self.revision
            yield {"workspaces": names} if names else None
