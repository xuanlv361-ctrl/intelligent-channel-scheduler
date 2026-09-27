from __future__ import annotations

import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from backend.security.service_identity import ServiceIdentityManager


@dataclass
class WorkerEntry:
    process: Any
    kind: str


class RealtimeSyncSupervisor:
    def __init__(self, database_path: Path, popen: Callable[..., Any] = subprocess.Popen,
                 service_identities: ServiceIdentityManager | None = None):
        self.database_path = Path(database_path).resolve()
        self.popen = popen
        self.service_identities = service_identities
        self.workers: dict[str, WorkerEntry] = {}
        self.lock = threading.Lock()

    def _launch(self, identifier: str, kind: str) -> int:
        prefixes = {"temporary": "LSYNC-", "persistent": "LSYNC-", "pairing": "PAIR-"}
        prefix = prefixes[kind]
        if not identifier.startswith(prefix) or not identifier[len(prefix):].isalnum():
            raise ValueError("log_sync_job_id_invalid")
        with self.lock:
            current = self.workers.get(identifier)
            if current is not None and current.process.poll() is None:
                raise RuntimeError("log_sync_worker_already_running")
            self.workers.pop(identifier, None)
        env = os.environ.copy()
        env["ROUTING_CONSOLE_DATABASE_PATH"] = str(self.database_path)
        if self.service_identities is not None:
            import sqlite3
            with sqlite3.connect(self.database_path) as db:
                db.row_factory = sqlite3.Row
                table = ("persistent_session_pairings" if kind == "pairing"
                         else "realtime_log_sync_jobs")
                key = "pairing_id" if kind == "pairing" else "sync_job_id"
                row = db.execute(
                    f"SELECT tenant_id,workspace_id FROM {table} WHERE {key}=?",
                    (identifier,)).fetchone()
                if not row:
                    raise ValueError("log_sync_job_not_found")
                token, reference = self.service_identities.issue(
                    "worker", row["tenant_id"], row["workspace_id"], "worker-api",
                    ttl_seconds=min(900,self.service_identities.maximum_ttl_seconds))
                credential_id = str(reference["credential_id"])
                db.execute(
                    f"UPDATE {table} SET service_credential_id=? WHERE {key}=? "
                    "AND tenant_id=? AND workspace_id=?",
                    (credential_id,identifier,row["tenant_id"],row["workspace_id"]))
                db.commit()
            env["ROUTING_SERVICE_TOKEN"] = token
            env["ROUTING_SERVICE_REQUEST_NONCE"] = uuid.uuid4().hex
            env["ROUTING_SERVICE_AUDIENCE"] = "worker-api"
            env["ROUTING_ENTERPRISE_SECURITY_REQUIRED"] = "1"
        process = self.popen(
            [sys.executable, "-m", "collector.persistent_browser_session",
             "--kind", kind, "--identifier", identifier]
            if kind != "temporary" else
            [sys.executable, "-m", "collector.realtime_browser_session",
             "--job-id", identifier],
            cwd=str(Path(__file__).resolve().parents[1]), env=env, shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        with self.lock:
            current = self.workers.get(identifier)
            if current is not None and current.process.poll() is None:
                process.terminate()
                raise RuntimeError("log_sync_worker_already_running")
            self.workers[identifier] = WorkerEntry(process, kind)
        return int(process.pid)

    def reap_finished(self) -> list[dict[str, Any]]:
        finished: list[dict[str, Any]] = []
        with self.lock:
            for identifier, entry in list(self.workers.items()):
                code = entry.process.poll()
                if code is not None:
                    finished.append({
                        "identifier": identifier,
                        "kind": entry.kind,
                        "exit_code": int(code),
                    })
                    self.workers.pop(identifier, None)
        return finished

    def launch(self, job_id: str) -> int:
        return self._launch(job_id, "temporary")

    def launch_persistent(self, job_id: str) -> int:
        return self._launch(job_id, "persistent")

    def launch_pairing(self, pairing_id: str) -> int:
        return self._launch(pairing_id, "pairing")

    def stop(self, job_id: str) -> bool:
        with self.lock:
            entry = self.workers.get(job_id)
        if entry is None:
            return False
        process = entry.process
        try:
            if process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            if process.poll() is None:
                return False
        except Exception:
            return False
        with self.lock:
            current = self.workers.get(job_id)
            if current is entry:
                self.workers.pop(job_id, None)
        return True

    def cleanup(self):
        with self.lock:
            ids = list(self.workers)
        for job_id in ids:
            self.stop(job_id)
