"""Emit a credential-free summary of Provider correlation and cost coverage."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

DB = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"


def main() -> None:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    def count(where: str) -> int:
        return int(db.execute(
            f"SELECT COUNT(*) FROM standardized_call_logs WHERE {where}"
        ).fetchone()[0])

    amounts = [Decimal(str(row[0])) for row in db.execute(
        """SELECT provider_cost_amount_exact FROM standardized_call_logs
        WHERE source_type='realtime_execution' AND cost_type='provider_actual'
        AND match_confidence='exact'
        AND duplicate_status='canonical'
        AND provider_cost_amount_exact IS NOT NULL""")]
    historical_amounts = [Decimal(str(row[0])) for row in db.execute(
        """SELECT cost_amount FROM standardized_call_logs
        WHERE source_type='historical_uat_csv' AND duplicate_status='canonical'
        AND cost_amount IS NOT NULL""")]
    reasons = {str(row[0] or "unclassified"): int(row[1]) for row in db.execute(
        """SELECT provider_sync_failure_reason,COUNT(*)
        FROM standardized_call_logs WHERE cost_status='pending_provider_sync'
        AND source_type='realtime_execution'
        AND COALESCE(traffic_class,'business')='business'
        AND duplicate_status='canonical'
        GROUP BY provider_sync_failure_reason ORDER BY COUNT(*) DESC""")}
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "realtime_business": count(
            "source_type='realtime_execution' AND "
            "COALESCE(traffic_class,'business')='business' AND "
            "duplicate_status='canonical'"),
        "pending_business": count(
            "source_type='realtime_execution' AND "
            "COALESCE(traffic_class,'business')='business' AND "
            "cost_status='pending_provider_sync' AND duplicate_status='canonical'"),
        "pending_with_provider_identifier": count(
            "cost_status='pending_provider_sync' AND "
            "(provider_request_id IS NOT NULL OR provider_response_id IS NOT NULL "
            "OR provider_trace_id IS NOT NULL OR client_correlation_id IS NOT NULL)"),
        "exact_matches": count("match_confidence='exact'"),
        "manual_matches": count("match_method='manually_confirmed'"),
        "provider_actual_count": len(amounts),
        "provider_actual_sum_cny": format(sum(amounts, Decimal('0')), "f"),
        "historical_actual_count": len(historical_amounts),
        "historical_actual_sum_cny": format(
            sum(historical_amounts, Decimal('0')), "f"),
        "provider_log_records": int(db.execute(
            "SELECT COUNT(*) FROM realtime_log_records").fetchone()[0]),
        "pending_reasons": reasons,
        "exact_match_fields": {str(row[0]): int(row[1]) for row in db.execute(
            """SELECT matched_field,COUNT(*) FROM standardized_call_logs
            WHERE match_confidence='exact' GROUP BY matched_field""")},
    }
    evidence_dir = Path(__file__).resolve().parents[1] / "evidence/provider-correlation"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_dir / "provider-correlation-final-summary.json"
    evidence_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    result["evidence_path"] = str(evidence_path)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
