"""Freeze revision-specific Provider correlation evidence without changing prior files."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ["LOCALAPPDATA"]) / "IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
TAG = "20260806T1910"
STRATEGY_RUN_ID = "SER-F61CFBCB170E464AA2099D83662CBC84"


def main() -> None:
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        amounts = [Decimal(str(row[0])) for row in db.execute(
            """SELECT provider_cost_amount_exact FROM standardized_call_logs
            WHERE source_type='realtime_execution' AND cost_type='provider_actual'
            AND match_confidence='exact' AND duplicate_status='canonical'
            AND provider_cost_amount_exact IS NOT NULL""")]
        reasons = {str(row[0] or "unclassified"): int(row[1]) for row in db.execute(
            """SELECT provider_sync_failure_reason,COUNT(*) FROM standardized_call_logs
            WHERE cost_status='pending_provider_sync' AND source_type='realtime_execution'
            AND COALESCE(traffic_class,'business')='business' AND duplicate_status='canonical'
            GROUP BY provider_sync_failure_reason""")}
        exact_fields = {str(row[0]): int(row[1]) for row in db.execute(
            """SELECT matched_field,COUNT(*) FROM standardized_call_logs
            WHERE match_confidence='exact' GROUP BY matched_field""")}
        counts = {
            "realtime_business": int(db.execute("SELECT COUNT(*) FROM standardized_call_logs WHERE source_type='realtime_execution' AND COALESCE(traffic_class,'business')='business' AND duplicate_status='canonical'").fetchone()[0]),
            "pending_business": int(db.execute("SELECT COUNT(*) FROM standardized_call_logs WHERE source_type='realtime_execution' AND COALESCE(traffic_class,'business')='business' AND cost_status='pending_provider_sync' AND duplicate_status='canonical'").fetchone()[0]),
            "pending_with_provider_identifier": int(db.execute("SELECT COUNT(*) FROM standardized_call_logs WHERE cost_status='pending_provider_sync' AND (provider_request_id IS NOT NULL OR provider_response_id IS NOT NULL OR provider_trace_id IS NOT NULL OR client_correlation_id IS NOT NULL)").fetchone()[0]),
            "provider_log_records": int(db.execute("SELECT COUNT(*) FROM realtime_log_records").fetchone()[0]),
        }
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **counts,
        "exact_matches": sum(exact_fields.values()),
        "exact_match_fields": exact_fields,
        "provider_actual_exact_count": len(amounts),
        "provider_actual_exact_sum_cny": format(sum(amounts, Decimal("0")), "f"),
        "pending_reasons": reasons,
        "automatic_fuzzy_matching_used": False,
    }
    summary_path = ROOT / f"evidence/provider-correlation/provider-correlation-exact-summary-{TAG}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    strategy_source = ROOT / f"evidence/strategy_effect/{STRATEGY_RUN_ID}/summary-after-provider-sync.json"
    strategy_target = ROOT / f"evidence/strategy_effect/{STRATEGY_RUN_ID}/summary-after-provider-sync-{TAG}.json"
    strategy_target.write_bytes(strategy_source.read_bytes())
    regression_source = ROOT / "evidence/regression/provider-correlation-regression.json"
    regression_target = ROOT / f"evidence/regression/provider-correlation-regression-{TAG}.json"
    regression_target.write_bytes(regression_source.read_bytes())
    print(json.dumps({"summary": str(summary_path), "strategy": str(strategy_target), "regression": str(regression_target), **summary}, ensure_ascii=False))


if __name__ == "__main__":
    main()
