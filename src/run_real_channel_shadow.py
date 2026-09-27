"""Run deterministic real-channel shadow decisions without channel execution."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

import strategy_engine
from real_channel_shadow_adapter import adapt_shadow_catalog


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "real_shadow_experiment_v1.json"
CATALOG_PATH = ROOT / "data" / "channel_catalog_real_v1.csv"
REQUESTS_PATH = ROOT / "data" / "real_shadow_requests_v1.csv"
DEFAULT_OUTPUT_DIR = ROOT / "output"
JSON_NAME = "real_shadow_decisions_v1.json"
CSV_NAME = "real_shadow_summary_v1.csv"
JSONL_NAME = "real_shadow_decision_logs_v1.jsonl"
SUMMARY_COLUMNS = [
    "shadow_decision_id", "request_id", "strategy", "requested_model",
    "stream_required", "outcome", "recommended_candidate", "candidate_count",
    "eligible_count", "excluded_count", "execution_attempted", "routing_allowed",
    "catalog_version", "policy_version", "strategy_catalog_version",
    "input_catalog_sha256", "limitations",
]
LIMITATIONS = [
    "每渠道仅有5条人工UAT测量。",
    "observed_success_rate仅表示当前UAT样本，不是长期成功率。",
    "actual_model、HTTP状态和Request ID尚未确认。",
    "流式能力尚未确认。",
    "影子推荐不执行路由，也不表示线上最优渠道。",
]


class ShadowValidationError(ValueError):
    pass


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def as_bool(value: str) -> bool:
    if value not in {"TRUE", "FALSE"}:
        raise ShadowValidationError(f"invalid boolean: {value}")
    return value == "TRUE"


def validate_requests(rows: list[dict[str, str]]) -> None:
    expected = [f"SH{i:03d}" for i in range(1, 6)]
    if [row.get("request_id") for row in rows] != expected:
        raise ShadowValidationError("shadow requests must be ordered SH001-SH005")
    required = {"request_id", "requested_model", "stream_required", "input_tokens", "output_tokens", "currency", "strategy", "mode", "data_source"}
    for row in rows:
        if not required <= set(row) or any(row[field] == "" for field in required):
            raise ShadowValidationError(f"{row.get('request_id', '<unknown>')}: missing request field")
        if row["mode"] != "shadow" or row["data_source"] != "real_uat_measurement_shadow_request":
            raise ShadowValidationError(f"{row['request_id']}: invalid shadow provenance")
        as_bool(row["stream_required"])


def _selection_fields(strategy: str, item: dict[str, Any]) -> tuple[str, float | None]:
    if strategy == "fastest_first":
        return "latency_mean_ms", item.get("latency_ms")
    if strategy == "cheapest_first":
        return "estimated_cost", item.get("estimated_cost")
    if strategy == "reliability_first":
        return "observed_failure_risk", item.get("raw_failure_risk")
    return "confidence_aware_final_score", item.get("final_score")


def _ranking_row(item: dict[str, Any], source: dict[str, str], strategy: str) -> dict[str, Any]:
    metric, value = _selection_fields(strategy, item)
    return {
        "rank": item.get("rank"), "candidate_id": source["candidate_id"],
        "channel_id": source["channel_id"], "channel_name": source["channel_name"],
        "eligible": item["eligible"], "exclusion_reason": item.get("exclusion_reason"),
        "final_score": item.get("final_score"),
        "latency_mean_ms": float(source["latency_mean_ms"]),
        "latency_p50_ms": float(source["latency_p50_ms"]),
        "observed_success_rate": float(source["observed_success_rate"]),
        "measurement_count": int(source["sample_size"]),
        "estimated_cost": item.get("estimated_cost"),
        "confidence_level": source["confidence_level"],
        "routing_eligible": source["routing_eligible"] == "TRUE",
        "routing_block_reason": source["routing_block_reason"],
        "source_type": source["source_type"], "is_mock": source["is_mock"] == "TRUE",
        "selection_metric": metric, "selection_value": value,
    }


def run_shadow_decisions(
    requests: list[dict[str, str]],
    catalog_rows: list[dict[str, str]],
    config: dict[str, Any],
    *,
    decision_function: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    validate_requests(requests)
    decision_function = decision_function or strategy_engine.strategy_decision
    candidates = adapt_shadow_catalog(catalog_rows, compatibility_priority=int(config["compatibility_priority"]))
    if not candidates:
        raise ShadowValidationError("no shadow-eligible candidates")
    catalog_version = catalog_rows[0]["catalog_version"]
    catalog_sha = hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest().upper()
    strategy_catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    policy = strategy_engine.load_json(strategy_engine.POLICY_PATH)
    decision_time = max(row["metrics_updated_at"] for row in candidates)
    source_by_id = {row["candidate_id"]: row for row in candidates}
    results: list[dict[str, Any]] = []

    for request_index, request in enumerate(requests):
        engine_result = decision_function(
            request_id=request["request_id"], requested_model=request["requested_model"],
            stream_required=as_bool(request["stream_required"]),
            input_tokens=int(request["input_tokens"]), output_tokens=int(request["output_tokens"]),
            currency=request["currency"], decision_time=decision_time,
            candidates=candidates, strategy_name=request["strategy"], request_index=request_index,
            random_seed=42, catalog=strategy_catalog, policy=policy,
            experiment_metadata={
                "experiment_id": "REAL-SHADOW-V1", "run_id": f"real-shadow-v1-{request['request_id']}",
                "generated_at": decision_time, "data_source": "real_uat_measurement_shadow_decision",
            },
        )
        details = engine_result["candidate_details"]
        details.sort(key=lambda item: (item["rank"] is None, item["rank"] or 0, int(item["channel_id"])))
        ranking = [_ranking_row(item, source_by_id[item["candidate_id"]], request["strategy"]) for item in details]
        eligible = [row for row in ranking if row["eligible"]]
        costs = {round(float(row["estimated_cost"]), 12) for row in eligible if row["estimated_cost"] is not None}
        success_rates = {row["observed_success_rate"] for row in eligible}
        results.append({
            "shadow_decision_id": f"shadow-v1-{request['request_id']}",
            "shadow_version": config["shadow_version"], "request_id": request["request_id"],
            "mode": "shadow", "catalog_version": catalog_version,
            "policy_version": engine_result["policy_version"],
            "strategy_catalog_version": engine_result["catalog_version"],
            "strategy": request["strategy"], "requested_model": request["requested_model"],
            "stream_required": as_bool(request["stream_required"]),
            "candidate_count": len(catalog_rows), "shadow_eligible_count": len(candidates),
            "eligible_count": len(eligible), "excluded_count": len(ranking) - len(eligible),
            "outcome": engine_result["outcome"],
            "recommended_candidate": engine_result["selected_candidate"],
            "execution_attempted": False, "routing_allowed": False,
            "recommendation_scope": "offline_shadow_recommendation_only",
            "selection_reason": engine_result["selection_reason"],
            "price_tie": request["strategy"] == "cheapest_first" and len(eligible) > 1 and len(costs) == 1,
            "observed_success_tie": request["strategy"] == "reliability_first" and len(eligible) > 1 and len(success_rates) == 1,
            "ranking": ranking,
            "excluded_candidates": [row for row in ranking if not row["eligible"]],
            "limitations": LIMITATIONS,
            "input_catalog_sha256": catalog_sha,
        })
    return results


def summary_row(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "shadow_decision_id": decision["shadow_decision_id"], "request_id": decision["request_id"],
        "strategy": decision["strategy"], "requested_model": decision["requested_model"],
        "stream_required": "TRUE" if decision["stream_required"] else "FALSE",
        "outcome": decision["outcome"], "recommended_candidate": decision["recommended_candidate"] or "",
        "candidate_count": decision["candidate_count"], "eligible_count": decision["eligible_count"],
        "excluded_count": decision["excluded_count"], "execution_attempted": "FALSE",
        "routing_allowed": "FALSE", "catalog_version": decision["catalog_version"],
        "policy_version": decision["policy_version"], "strategy_catalog_version": decision["strategy_catalog_version"],
        "input_catalog_sha256": decision["input_catalog_sha256"],
        "limitations": " | ".join(decision["limitations"]),
    }


def write_outputs(decisions: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / JSON_NAME).open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(decisions, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with (output_dir / CSV_NAME).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(summary_row(decision) for decision in decisions)
    with (output_dir / JSONL_NAME).open("w", encoding="utf-8", newline="\n") as handle:
        for decision in decisions:
            handle.write(json.dumps(decision, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--request-id")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    try:
        config = load_json(CONFIG_PATH)
        requests = read_csv(REQUESTS_PATH)
        catalog = read_csv(CATALOG_PATH)
        if args.request_id:
            requests = [row for row in requests if row["request_id"] == args.request_id]
            if not requests:
                raise ShadowValidationError(f"unknown request_id: {args.request_id}")
            # Preserve validation contract while supporting a selected request.
            all_requests = read_csv(REQUESTS_PATH)
            validate_requests(all_requests)
        decisions = run_shadow_decisions(requests if not args.request_id else requests, catalog, config) if not args.request_id else run_shadow_decisions_selected(requests, catalog, config)
        if not args.dry_run:
            write_outputs(decisions, args.output_dir)
        print(json.dumps(decisions, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


def run_shadow_decisions_selected(requests: list[dict[str, str]], catalog_rows: list[dict[str, str]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a validated subset without weakening the five-request source contract."""
    all_requests = read_csv(REQUESTS_PATH)
    all_results = run_shadow_decisions(all_requests, catalog_rows, config)
    wanted = {row["request_id"] for row in requests}
    return [result for result in all_results if result["request_id"] in wanted]


if __name__ == "__main__":
    raise SystemExit(main())
