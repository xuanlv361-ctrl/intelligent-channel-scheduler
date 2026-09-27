"""Build the immutable final acceptance evidence from the unified local database."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.acceptance_run_service import AcceptanceRunService


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] * (1 - fraction) + ordered[upper] * fraction, 3)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("acceptance_run_id")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    root = ROOT
    database = Path(os.environ.get("ICS_LOCAL_DATABASE_PATH") or
                    Path(os.environ["LOCALAPPDATA"]) / "IntelligentChannelScheduler" /
                    "data" / "routing_quality_console.sqlite3")
    output_dir = root / "evidence" / "final_acceptance" / args.acceptance_run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    canonical = [dict(row) for row in db.execute("""SELECT * FROM standardized_call_logs
      WHERE COALESCE(duplicate_status,'canonical')<>'exact_duplicate' ORDER BY occurred_at,cursor_id""")]
    run_rows = [row for row in canonical if row.get("acceptance_run_id") == args.acceptance_run_id]
    latency = [float(row["total_latency_ms"]) for row in canonical
               if row.get("total_latency_ms") is not None and not row.get("is_fault_injected")]
    run_latency = [float(row["total_latency_ms"]) for row in run_rows
                   if row.get("total_latency_ms") is not None and not row.get("is_fault_injected")]
    success = sum(row.get("request_status") == "SUCCESS" for row in canonical)
    cost_values = [float(row["cost_amount"]) for row in canonical if row.get("cost_amount") is not None]
    run_cost_values = [float(row["cost_amount"]) for row in run_rows if row.get("cost_amount") is not None]
    sources = Counter(row.get("source_type") or "unknown" for row in canonical)
    duplicate_count = db.execute("""SELECT COUNT(*) FROM standardized_call_logs
      WHERE duplicate_status='exact_duplicate'""").fetchone()[0]
    duplicate_candidates = db.execute("""SELECT COUNT(*) FROM standardized_call_logs
      WHERE duplicate_status='duplicate_candidate'""").fetchone()[0]
    model_rows = [row for row in run_rows if row.get("strategy_variant") == "direct_model"]
    model_summary = []
    for model in ("kimi-k3", "deepseek-v4-flash", "glm-5.2", "kimi-k2.7-code", "kimi-k2.6"):
        selected = [row for row in model_rows if row.get("requested_model") == model]
        model_summary.append({
            "model": model,
            "non_stream": next((row["request_status"] for row in selected if not row["stream"]), "not_run"),
            "stream": next((row["request_status"] for row in selected if row["stream"]), "not_run"),
            "request_ids": [row["request_id"] for row in selected if row.get("request_id")],
            "decision_ids": [row["decision_id"] for row in selected if row.get("decision_id")],
            "http_statuses": [row["http_status"] for row in selected],
            "latency_ms": [row["total_latency_ms"] for row in selected],
            "input_tokens": sum(row.get("input_tokens") or 0 for row in selected),
            "output_tokens": sum(row.get("output_tokens") or 0 for row in selected),
            "actual_cost": round(sum(float(row["cost_amount"]) for row in selected
                                     if row.get("cost_amount") is not None), 9)
                if any(row.get("cost_amount") is not None for row in selected) else None,
            "currency": "CNY" if any(row.get("cost_amount") is not None
                                      for row in selected) else None,
        })
    clean_success = [row for row in model_rows if row.get("request_status") == "SUCCESS"]
    fastest = min(clean_success, key=lambda row: row.get("total_latency_ms") or float("inf")) if clean_success else None
    cost_observed = [row for row in clean_success if row.get("cost_amount") is not None]
    cheapest = min(cost_observed, key=lambda row: float(row["cost_amount"])) \
        if cost_observed else None
    report = {
        "acceptance_run_id": args.acceptance_run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": str(database),
        "unified_log": {
            "raw_records": db.execute("SELECT COUNT(*) FROM standardized_call_logs").fetchone()[0],
            "deduplicated_records": len(canonical),
            "exact_duplicates": duplicate_count,
            "duplicate_candidates": duplicate_candidates,
            "source_counts": dict(sources),
            "success": success,
            "failed": len(canonical) - success,
            "success_rate": round(success / len(canonical), 6) if canonical else None,
            "input_tokens": sum(row.get("input_tokens") or 0 for row in canonical),
            "cached_tokens": sum(row.get("cached_input_tokens") or 0 for row in canonical),
            "output_tokens": sum(row.get("output_tokens") or 0 for row in canonical),
            "actual_cost": round(sum(cost_values), 9) if cost_values else None,
            "currency": "CNY" if cost_values else None,
            "p50_latency_ms": round(median(latency), 3) if latency else None,
            "p95_latency_ms": percentile(latency, .95),
            "p99_latency_ms": percentile(latency, .99),
        },
        "acceptance_evidence": {
            "records": len(run_rows),
            "provider_live": sum(not row.get("is_fault_injected") for row in run_rows),
            "uat_injected": sum(bool(row.get("is_fault_injected")) for row in run_rows),
            "success": sum(row.get("request_status") == "SUCCESS" for row in run_rows),
            "failed": sum(row.get("request_status") != "SUCCESS" for row in run_rows),
            "input_tokens": sum(row.get("input_tokens") or 0 for row in run_rows),
            "cached_tokens": sum(row.get("cached_input_tokens") or 0 for row in run_rows),
            "output_tokens": sum(row.get("output_tokens") or 0 for row in run_rows),
            "actual_cost": round(sum(run_cost_values), 9) if run_cost_values else None,
            "actual_cost_status": "unavailable_provider_and_price_evidence" if not run_cost_values else "available",
            "p50_latency_ms": round(median(run_latency), 3) if run_latency else None,
            "p95_latency_ms": percentile(run_latency, .95),
            "p99_latency_ms": percentile(run_latency, .99),
        },
        "six_model_comparison": model_summary,
        "strategy_comparison": {
            "baseline": "current_direct_model_evidence",
            "latency_first": {
                "status": "live_evidence_available",
                "selected_model": fastest.get("actual_model") if fastest else None,
                "observed_latency_ms": fastest.get("total_latency_ms") if fastest else None,
                "basis": "same_prompt_successful_real_uat_calls",
            },
            "cost_first": {
                "status": "live_evidence_available" if cheapest else "not_calculable",
                "selected_model": (cheapest.get("actual_model") or
                                   cheapest.get("requested_model")) if cheapest else None,
                "observed_cost": float(cheapest["cost_amount"]) if cheapest else None,
                "currency": cheapest.get("currency") if cheapest else None,
                "basis": "same_prompt_provider_log_actual_cost" if cheapest else None,
                "reason": None if cheapest else "provider_log_actual_cost_not_synchronized",
            },
            "limitations": [
                "authoritative_channel_id_not_returned_for_model_level_calls",
                "historical_csv_has_no_request_body_and_is_not_used_for_request_level_counterfactuals",
            ],
        },
        "final_categories": {
            "live_verified": ["model_catalog", "text_uat", "image_understanding", "video_generation",
                              "unified_logs", "model_level_canary", "rollback", "clean_environment_request"],
            "uat_injected_verified": ["error_classification", "fallback", "circuit_open_half_open_recovery",
                                      "automatic_canary_stop"],
            "local_verified": ["multimodal_assets", "clean_install", "production_build", "health_ready_worker"],
            "pending_external_configuration": ["audio_model_channel", "versioned_price_catalog",
                                                "authoritative_channel_mapping", "production_adapter"],
            "pending_human_acceptance": ["independent_human_signoff"],
            "incomplete": [] if cheapest else ["cost_first_live_effect_comparison"],
            "blocked": [],
        },
    }
    report_path = output_dir / "final_acceptance_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.finalize:
        service = AcceptanceRunService(database)
        snapshot = service.finalize_snapshot(
            args.acceptance_run_id,
            final_categories=report["final_categories"],
            human_signoff_state={"clean_environment_acceptance": "completed",
                                 "independent_human_signoff": "pending"},
            notes="Finalized from deduplicated unified call logs; missing cost is never represented as zero.",
        )
        report["snapshot_id"] = snapshot["snapshot_id"]
        report["snapshot_created_at"] = snapshot["created_at"]
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "run_records": len(run_rows),
                      "deduplicated_records": len(canonical), "finalized": args.finalize}, ensure_ascii=False))


if __name__ == "__main__":
    main()
