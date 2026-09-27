"""Start one immutable final-acceptance run from the current local database."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from backend.acceptance_run_service import AcceptanceRunService
from backend.tenant_security import TenantScope


def main() -> None:
    path = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        watermark = dict(db.execute(
            """SELECT COUNT(*) total, MIN(occurred_at) oldest, MAX(occurred_at) latest
               FROM standardized_call_logs WHERE duplicate_status<>'exact_duplicate'"""
        ).fetchone())
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    run = AcceptanceRunService(path, TenantScope.local_development()).start_run(
        environment_id="china_uat",
        baseline_git={"commit": commit, "worktree_clean": False},
        baseline_config={
            "configuration_version": "decision-policy-v2.0.0-r5",
            "retry_policy_version": "retry-v2.0.0",
        },
        baseline_price={"status": "not_configured"},
        baseline_migration={"version": 2, "status": "ready"},
        baseline_time={"timezone": "Asia/Shanghai"},
        database_watermark={"path": str(path)},
        log_watermark=watermark,
        started_by="codex_acceptance_operator",
        metadata={"purpose": "final_real_uat_acceptance", "production_access": False},
    )
    print(json.dumps({
        "acceptance_run_id": run["acceptance_run_id"],
        "started_at": run["started_at"],
        "log_watermark": run["log_watermark"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
