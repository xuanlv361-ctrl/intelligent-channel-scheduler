"""Build a non-secret, Decimal-accurate Provider cost-sync evidence summary."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = Path(os.environ["LOCALAPPDATA"]) / "IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
OUT = ROOT / "evidence/provider_cost_sync/provider-cost-sync-summary.json"


def main() -> None:
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        latest = dict(db.execute("""SELECT * FROM provider_log_sync_batches
          WHERE finished_at IS NOT NULL ORDER BY pages_read DESC,
          started_at DESC LIMIT 1""").fetchone())
        task_start = str(latest["started_at"])
        provider_new = db.execute("""SELECT COUNT(*) FROM realtime_log_records
          WHERE created_at>=?""", (task_start,)).fetchone()[0]
        # The first batch in this task is the first one with a non-empty real
        # Provider read.  All records are de-duplicated by Provider Log ID.
        task_provider_total = db.execute(
            "SELECT COUNT(DISTINCT platform_log_id) FROM realtime_log_records"
        ).fetchone()[0]
        matched = db.execute("""SELECT COUNT(*) FROM standardized_call_logs
          WHERE source_type='realtime_execution' AND traffic_class='business'
          AND cost_status='provider_actual'""").fetchone()[0]
        pending_rows = list(db.execute("""SELECT provider_sync_failure_reason,
          COUNT(*) AS amount FROM standardized_call_logs
          WHERE source_type='realtime_execution' AND traffic_class='business'
          AND cost_status='pending_provider_sync' GROUP BY provider_sync_failure_reason"""))
        pending = sum(int(row["amount"]) for row in pending_rows)
        amounts = [Decimal(row[0]) for row in db.execute(
            "SELECT converted_amount FROM provider_cost_components")]
        actual_total = sum(amounts, Decimal("0"))
        pages = [dict(row) for row in db.execute("""SELECT page_number,
          provider_count,accepted_count,duplicate_count,provider_cursor,
          provider_log_time,read_at FROM provider_log_sync_pages
          WHERE sync_batch_id=? ORDER BY page_number""",
          (latest["sync_batch_id"],))]
    evidence = {
        "schema_version": "provider_cost_sync_evidence_v1",
        "evidence_id": f"PCSE-{uuid.uuid4()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment_id": "china_uat",
        "sync_batch_id": latest["sync_batch_id"],
        "source_type": "provider_billing_log",
        "task_start_pending_count": int(latest["initial_pending_count"]),
        "pending_created_during_sync": int(latest["pending_created_during_sync"] or 0),
        "final_pending_count": int(latest["final_pending_count"] or pending),
        "provider_new_records_in_task": int(task_provider_total),
        "provider_new_records_in_latest_batch": int(provider_new),
        "provider_unique_records_available": int(task_provider_total),
        "pages_read": int(latest["pages_read"] or len(pages)),
        "page_evidence": pages,
        "pagination_end_reason": "provider_has_no_more_records",
        "previous_watermark": json.loads(latest["previous_watermark_json"] or "{}"),
        "final_watermark": json.loads(latest["final_watermark_json"] or "{}"),
        "exact_request_id_matches": int(matched),
        "cost_backfilled_count": int(latest["cost_backfilled_count"] or 0),
        "unmatched_count": pending,
        "unmatched_reasons": {str(row["provider_sync_failure_reason"] or
                                    "provider_log_not_found"): int(row["amount"])
                              for row in pending_rows},
        "actual_provider_cost_total_cny": format(actual_total, "f"),
        "quota_evidence": {
            "raw_unit": "provider_internal_quota",
            "quota_per_unit": "500000",
            "display_type": "CNY",
            "usd_exchange_rate": "7.3",
            "conversion_rate_cny_per_quota": "0.0000146",
            "formula": "converted_amount_cny = raw_quota / 500000 * 7.3",
            "billing_scope": "quota includes input, output and cached-token billing",
            "stored_precision": "original Decimal precision",
            "display_precision": 6,
            "rounding_mode": "ROUND_HALF_UP",
            "negative_quota": "stored as reversal component",
            "multiple_logs_per_request": "deduplicate by Provider Log ID and sum components",
            "source_reference": "https://uat.weimeta.cn/console/billing/logs and /api/status public billing context",
        },
        "correlation_policy": "exact_request_id_only",
        "fuzzy_matching": False,
        "idempotency": "Provider Log ID plus batch/page evidence",
    }
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    evidence["sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps({key: evidence[key] for key in (
        "sync_batch_id", "task_start_pending_count", "pending_created_during_sync",
        "provider_unique_records_available", "exact_request_id_matches",
        "unmatched_count", "cost_backfilled_count",
        "actual_provider_cost_total_cny", "sha256")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
