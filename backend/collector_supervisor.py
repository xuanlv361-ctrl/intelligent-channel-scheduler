from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable


class CollectorSupervisor:
    def __init__(self, database_path: Path, popen: Callable[..., Any] = subprocess.Popen):
        self.database_path = Path(database_path).resolve()
        self._popen = popen
        self._workers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def launch(self, run_id: str) -> int:
        if not run_id.startswith("CRUN-") or not run_id[5:].isalnum():
            raise ValueError("collector_run_id_invalid")
        args = [sys.executable, "-m", "collector.collector_worker", "--run-id", run_id]
        env = os.environ.copy()
        env["ROUTING_CONSOLE_DATABASE_PATH"] = str(self.database_path)
        kwargs: dict[str, Any] = {
            "shell": False, "env": env, "cwd": str(Path(__file__).resolve().parents[1]),
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = self._popen(args, **kwargs)
        with self._lock:
            self._workers[run_id] = process
        return int(process.pid)

    def is_alive(self, run_id: str) -> bool:
        with self._lock:
            process = self._workers.get(run_id)
        return bool(process and process.poll() is None)

    def stop(self, run_id: str, grace_seconds: float = 5.0) -> bool:
        with self._lock:
            process = self._workers.get(run_id)
        if not process:
            return True
        if process.poll() is None:
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        with self._lock:
            self._workers.pop(run_id, None)
        return True

    def cleanup(self) -> None:
        with self._lock:
            run_ids = list(self._workers)
        for run_id in run_ids:
            self.stop(run_id)
