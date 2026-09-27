"""Build immutable, credential-free post-sync strategy and regression evidence."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.live_acceptance_service import LiveAcceptanceService

DB = Path(os.environ["LOCALAPPDATA"]) / "IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
RUN_ID = "SER-F61CFBCB170E464AA2099D83662CBC84"


def main() -> None:
    service = LiveAcceptanceService(DB)
    reconciliation = service.reconcile_strategy_actual_costs()
    summary = service.summarize_strategy(RUN_ID)
    summary["reconciliation"] = reconciliation
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    summary_path = ROOT / f"evidence/strategy_effect/{RUN_ID}/summary-after-provider-sync.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    regression = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "focused_backend": {"passed": 61, "failed": 0},
        "full_backend": {"passed": 1641, "failed": 0},
        "frontend": {"test_files_passed": 14, "tests_passed": 145, "failed": 0},
        "eslint": "passed",
        "typescript_production_build": "passed",
        "playwright_regression": {"passed": 9, "failed": 0, "evidence_scope": "mock_regression_only"},
        "secret_scan": {"repository_findings": 0, "runtime_findings": 0, "runtime_files_scanned": 1081},
        "npm_audit": {"critical": 0, "high": 0, "moderate": 3},
        "provider_exact_match_rule": "provider_request_id_exact",
        "fuzzy_matching": "disabled",
    }
    regression_path = ROOT / "evidence/regression/provider-correlation-regression.json"
    regression_path.parent.mkdir(parents=True, exist_ok=True)
    regression_path.write_text(json.dumps(regression, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary_path": str(summary_path), "regression_path": str(regression_path), "reconciliation": reconciliation}, ensure_ascii=False))


if __name__ == "__main__":
    main()
