"""Credential-free local worker heartbeat used by the Windows process supervisor.

Job execution remains owned by the existing backend supervisors.  This process
provides an independently observable worker lifecycle without contacting UAT.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(state_dir: Path, interval_seconds: float = 5.0) -> int:
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    heartbeat = state_dir / "run" / "worker-heartbeat.json"
    started = datetime.now(timezone.utc).isoformat()
    while not stopping:
        _atomic_json(heartbeat, {
            "schema_version": "local_worker_heartbeat_v1",
            "state": "ready",
            "pid": os.getpid(),
            "started_at": started,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "network_called": False,
            "credentials_exposed": False,
        })
        time.sleep(interval_seconds)
    _atomic_json(heartbeat, {
        "schema_version": "local_worker_heartbeat_v1", "state": "stopped",
        "pid": os.getpid(), "started_at": started,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "network_called": False, "credentials_exposed": False,
    })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if args.interval_seconds < 1 or args.interval_seconds > 20:
        parser.error("interval must be between 1 and 20 seconds")
    return run(args.state_dir.resolve(), args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
