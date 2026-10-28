"""Single-owner lock for one persistent journal on Linux/macOS deployments."""
from pathlib import Path
import os

class AlreadyRunning(RuntimeError):
    pass

class WorkerLease:
    def __init__(self, journal_path):
        self.path = str(journal_path) + ".worker.lock"
        self.handle = None

    def __enter__(self):
        try:
            import fcntl
        except ImportError as exc:
            raise AlreadyRunning("Paper worker requires POSIX flock; use Docker on Windows") from exc
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise AlreadyRunning("Another worker owns this journal") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *args):
        if self.handle:
            import fcntl
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None

