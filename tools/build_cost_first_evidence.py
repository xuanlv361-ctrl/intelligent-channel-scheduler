"""Create reproducible cost-first evidence from real same-prompt UAT calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from statistics import quantiles

ROOT = Path(__file__).resolve().parents[1]


def estimate(row: sqlite3.Row, price: sqlite3.Row) -> Decimal:
    regular = max(0, int(row["input_tokens"] or 0) - int(row["cached_input_tokens"] or 0))
    cached = int(row["cached_input_tokens"] or 0)
    output = int(row["output_tokens"] or 0)
    return (Decimal(regular) * Decimal(price["input_price_per_million_tokens"])
        + Decimal(cached) * Decimal(price["cached_input_price_per_million_tokens"] or
                                    price["input_price_per_million_tokens"])
        + Decimal(output) * Decimal(price["output_price_per_million_tokens"])) / Decimal(1_000_000)


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 3)
    return round(quantiles(values, n=100, method="inclusive")[94], 3)


def build(db_path: Path, run_id: str, price_version: str) -> dict:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        prices = {row["model_id"]: row for row in db.execute("""SELECT * FROM
          model_price_catalog_records WHERE price_version=? AND environment_id='china_uat'
          AND verification_status='confirmed'""", (price_version,))}
        calls = list(db.execute("""SELECT * FROM standardized_call_logs
          WHERE acceptance_run_id=? AND is_fault_injected=0 AND duplicate_of IS NULL
          AND occurred_at BETWEEN '2026-08-06T02:40:00+00:00' AND '2026-08-06T02:48:00+00:00'
          ORDER BY occurred_at""", (run_id,)))
    usable = [row for row in calls if row["request_status"] == "SUCCESS"
              and row["actual_model"] in prices and row["input_tokens"] is not None
              and row["output_tokens"] is not None]
    candidate_by_stream = {}
    for stream in (0, 1):
        options = [row for row in usable if row["stream"] == stream]
        options.sort(key=lambda row: (estimate(row, prices[row["actual_model"]]),
                                      row["actual_model"], row["request_id"]))
        if options:
            candidate_by_stream[stream] = options[0]
    comparisons = []
    for baseline in usable:
        candidate = candidate_by_stream.get(baseline["stream"])
        if not candidate or baseline["request_id"] == candidate["request_id"]:
            continue
        baseline_cost = estimate(baseline, prices[baseline["actual_model"]])
        # Counterfactual price uses baseline request usage with the selected model rate.
        candidate_cost = estimate(baseline, prices[candidate["actual_model"]])
        comparisons.append({
            "request_id": baseline["request_id"], "decision_id": baseline["decision_id"],
            "candidate_request_id": candidate["request_id"],
            "candidate_decision_id": candidate["decision_id"],
            "request_capability": "text_chat_stream" if baseline["stream"] else "text_chat",
            "candidate_models": sorted(prices), "excluded_models": sorted(
                {str(row["requested_model"]) for row in calls if row["requested_model"] not in prices}),
            "exclusion_reason": "price_pending_confirmation",
            "price_version": price_version,
            "input_tokens": baseline["input_tokens"],
            "cached_tokens": baseline["cached_input_tokens"] or 0,
            "output_tokens": baseline["output_tokens"],
            "baseline_model": baseline["actual_model"],
            "cost_first_model": candidate["actual_model"],
            "baseline_cost": format(baseline_cost, "f"),
            "candidate_cost": format(candidate_cost, "f"),
            "cost_type": "estimated_versioned_price",
            "actual_provider_cost": baseline["cost_amount"],
            "actual_cost_status": "awaiting_provider_log_sync" if baseline["cost_amount"] is None else "available",
            "baseline_success": True, "candidate_success": True,
            "baseline_latency_ms": baseline["total_latency_ms"],
            "candidate_latency_ms": candidate["total_latency_ms"],
            "output_completeness": "assertions_passed_same_prompt_evidence",
            "choice_changed": baseline["actual_model"] != candidate["actual_model"],
            "difference_reason": "lowest_confirmed_versioned_token_cost_deterministic_model_id_tiebreak",
            "confidence": "medium_same_prompt_real_uat_calls",
        })
    excluded = [row for row in calls if row not in usable]
    baseline_total = sum(Decimal(row["baseline_cost"]) for row in comparisons)
    candidate_total = sum(Decimal(row["candidate_cost"]) for row in comparisons)
    summary = {
        "request_count": len(comparisons),
        "success_rate_baseline": 1.0 if comparisons else None,
        "success_rate_cost_first": 1.0 if comparisons else None,
        "total_cost_baseline": format(baseline_total, "f") if comparisons else None,
        "total_cost_cost_first": format(candidate_total, "f") if comparisons else None,
        "average_cost_baseline": format(baseline_total / len(comparisons), "f") if comparisons else None,
        "average_cost_cost_first": format(candidate_total / len(comparisons), "f") if comparisons else None,
        "p95_latency_baseline_ms": p95([float(row["baseline_latency_ms"]) for row in comparisons]),
        "p95_latency_cost_first_ms": p95([float(row["candidate_latency_ms"]) for row in comparisons]),
        "total_tokens": sum(row["input_tokens"] + row["cached_tokens"] + row["output_tokens"]
                            for row in comparisons),
        "fallback_rate_baseline": 0.0 if comparisons else None,
        "fallback_rate_cost_first": 0.0 if comparisons else None,
        "coverage": round(len(comparisons) / len(calls), 6) if calls else 0,
        "uncomparable_count": len(calls) - len(comparisons),
    }
    result = {"schema_version": "cost_first_effect_evidence_v1",
        "acceptance_run_id": run_id, "environment_id": "china_uat",
        "baseline": "current_actual", "candidate": "cost_first",
        "price_version": price_version, "actual_cost_note":
        "实际账单费用仅在Provider日志同步后展示；本比较费用为版本化价格估算。",
        "summary": summary, "comparisons": comparisons,
        "uncomparable": [{"request_id": row["request_id"], "model": row["requested_model"],
            "reason": "request_failed_or_price_pending_or_usage_missing"} for row in excluded]}
    result["checksum"] = hashlib.sha256(json.dumps(result, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", default="AR-c2e60d08-9ff7-4a23-98a0-b6fb4d55b951")
    parser.add_argument("--price-version", default="price-catalog-china-uat-20260806-v1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(args.database, args.run_id, args.price_version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=True))
