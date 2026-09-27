"""Start and wait for one managed, bounded China-UAT continuous probe.

This remains a thin CLI compatibility entry point.  The same durable execution
service is used by the HTTP control plane, so CLI and browser runs have identical
pause/stop semantics and traffic isolation.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from backend.continuous_probe_execution_service import (  # noqa: E402
    ContinuousProbeExecutionService,
    DEFAULT_MODELS,
    TERMINAL_STATUSES,
)
from backend.live_acceptance_service import LiveAcceptanceService  # noqa: E402
from backend.tenant_security import TenantScope  # noqa: E402


DEFAULT_DB = (Path.home() / "AppData" / "Local" / "IntelligentChannelScheduler" /
              "data" / "routing_quality_console.sqlite3")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a real bounded China-UAT probe")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--name", default="5分钟国内UAT模型探测")
    parser.add_argument("--duration-seconds", type=int, default=300)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--max-requests", type=int, default=500)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--max-tokens", type=int, default=16)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    return parser.parse_args()


def database_watermark(path: Path) -> int:
    with sqlite3.connect(path) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='standardized_call_logs'"
        ).fetchone()
        if not table:
            return 0
        return int(db.execute(
            "SELECT COALESCE(MAX(cursor_id),0) FROM standardized_call_logs"
        ).fetchone()[0])


def main() -> None:
    args = parse_args()
    scope = TenantScope.local_development()
    config = {
        "environment_id": "china_uat",
        "endpoint": "/v1/chat/completions",
        "method": "POST",
        "models": args.models,
        "stream": args.stream,
        "prompt": "请只回答：OK",
        "duration_seconds": args.duration_seconds,
        "interval_seconds": args.interval_seconds,
        "request_interval_seconds": args.interval_seconds,
        "max_concurrency": args.max_concurrency,
        "max_requests": args.max_requests,
        "timeout_seconds": args.timeout_seconds,
        "max_tokens": args.max_tokens,
        "temperature": 0,
        "cooldown_seconds": 10,
        "stop_thresholds": {
            "rate_429": 0.10,
            "rate_5xx": 0.05,
            "timeout_rate": 0.05,
            "success_rate": 0.90,
            "consecutive_failures": 5,
            "p95_ms": 20_000,
        },
    }
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    repository = LiveAcceptanceService(args.database, scope)
    created = repository.create_probe_run({
        "task_name": args.name,
        "environment_id": "china_uat",
        "git_commit": commit,
        "database_watermark": database_watermark(args.database),
        "configuration": config,
        "created_by": "cli-local-operator",
    })
    run_id = str(created["probe_run_id"])
    executor = ContinuousProbeExecutionService(args.database, scope)
    try:
        executor.start(run_id, config, actor_id="cli-local-operator")
        while True:
            status = executor.status(run_id)
            print(json.dumps({
                "probe_run_id": run_id,
                "status": status["status"],
                "sent_count": status["sent_count"],
                "completed_count": status["completed_count"],
                "max_concurrency_observed": status["max_concurrency_observed"],
            }, ensure_ascii=True), flush=True)
            if status["status"] in TERMINAL_STATUSES:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        executor.stop(run_id, actor_id="cli-local-operator", wait_seconds=10)
    finally:
        executor.shutdown(timeout_seconds=max(args.timeout_seconds + 2, 10))
    final = repository.probe_run(run_id)
    evidence_dir = ROOT / "evidence" / "continuous_probe" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "results.json"
    evidence_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    repository.update_probe(run_id, evidence_path=str(evidence_path.relative_to(ROOT)))
    print(json.dumps({
        "probe_run_id": run_id,
        "status": final["status"],
        "summary": final["summary"],
        "evidence_path": str(evidence_path.relative_to(ROOT)),
    }, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
