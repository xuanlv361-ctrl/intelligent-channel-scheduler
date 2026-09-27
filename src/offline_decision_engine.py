"""Deterministic, standard-library-only offline channel decision engine."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "decision_policy_v1.json"
SCENARIOS_PATH = ROOT / "data" / "scenario_catalog_v1.csv"
CANDIDATES_PATH = ROOT / "data" / "scenario_candidates_v1.csv"
DEFAULT_OUTPUT_DIR = ROOT / "output"
JSON_NAME = "offline_decisions_v1.json"
CSV_NAME = "offline_decision_summary_v1.csv"
SUMMARY_COLUMNS = [
    "decision_id", "policy_version", "scenario_id", "strategy", "outcome",
    "selected_candidate", "expected_selected_candidate", "decision_matches_expected",
    "candidate_count", "eligible_count", "excluded_count",
]
SCORE_FIELDS = [
    "estimated_cost", "latency_norm", "cost_norm", "failure_risk",
    "small_sample_penalty", "confidence_penalty", "evidence_penalty", "final_score",
]


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_inputs() -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    return load_json(POLICY_PATH), load_csv(SCENARIOS_PATH), load_csv(CANDIDATES_PATH)


def as_bool(value: str) -> bool:
    return value.upper() == "TRUE"


def exclusion_reason(candidate: dict[str, str], scenario: dict[str, str], policy: dict[str, Any]) -> str:
    """Return the first matching reason in the mandated filter order."""
    if candidate["requested_model"] != scenario["requested_model"]:
        return "model_mismatch"
    if candidate["availability_status"] != policy["filters"]["required_availability_status"]:
        return "availability_unavailable"
    if candidate["supports_text"].upper() != "TRUE":
        return "text_not_supported"
    if not as_bool(scenario["stream_required"]) and candidate["supports_non_stream"].upper() != "TRUE":
        return "non_stream_not_supported"
    if as_bool(scenario["stream_required"]):
        stream = candidate["supports_stream"].upper()
        if stream == "FALSE":
            return "stream_not_supported"
        if stream in {"", "PENDING_CONFIRMATION"}:
            return "stream_capability_unknown"
        if stream != "TRUE":
            return "stream_capability_unknown"
    if candidate["latency_ms"] == "":
        return "missing_latency"
    if candidate["input_price_per_1m"] == "" or candidate["output_price_per_1m"] == "":
        return "missing_price"
    if candidate["currency"] != scenario["currency"]:
        return "currency_mismatch"
    if candidate["metrics_updated_at"] == "":
        return "missing_metrics_updated_at"
    decision_time = datetime.strptime(scenario["decision_time"], "%Y-%m-%d %H:%M:%S")
    metrics_time = datetime.strptime(candidate["metrics_updated_at"], "%Y-%m-%d %H:%M:%S")
    if decision_time - metrics_time > timedelta(hours=policy["filters"]["max_metric_age_hours"]):
        return "stale_metrics"
    return ""


def calculate_score(candidate: dict[str, str], scenario: dict[str, str], policy: dict[str, Any]) -> dict[str, float]:
    estimated_cost = (
        float(scenario["input_tokens"]) * float(candidate["input_price_per_1m"])
        + float(scenario["output_tokens"]) * float(candidate["output_price_per_1m"])
    ) / 1_000_000
    normalization = policy["normalization"]
    latency_norm = min(float(candidate["latency_ms"]) / normalization["latency_reference_ms"], 1.0)
    cost_norm = min(estimated_cost / normalization["cost_reference_cny"], 1.0)
    failure_risk = 1.0 - float(candidate["success_rate"])
    penalties = policy["penalties"]
    small = penalties["small_sample_penalty"] if int(candidate["sample_size"]) < policy["filters"]["minimum_sample_size"] else 0.0
    confidence = penalties[f"{candidate['confidence_level'].lower()}_confidence_penalty"]
    evidence = small + confidence
    weights = policy["strategies"][scenario["strategy"]]["weights"]
    final = latency_norm * weights["latency"] + cost_norm * weights["cost"] + failure_risk * weights["failure_risk"] + evidence
    return {
        "estimated_cost": estimated_cost, "latency_norm": latency_norm,
        "cost_norm": cost_norm, "failure_risk": failure_risk,
        "small_sample_penalty": small, "confidence_penalty": confidence,
        "evidence_penalty": evidence, "final_score": final,
    }


def channel_sort_key(value: str) -> tuple[int, Any]:
    return (0, int(value)) if value.isdigit() else (1, value)


def decide_scenario(scenario: dict[str, str], candidates: list[dict[str, str]], policy: dict[str, Any]) -> dict[str, Any]:
    precision = int(policy["score_precision"])
    evaluated: list[dict[str, Any]] = []
    raw_scores: dict[str, float] = {}
    for source in candidates:
        reason = exclusion_reason(source, scenario, policy)
        item: dict[str, Any] = {
            "candidate_id": source["candidate_id"], "channel_id": source["channel_id"],
            "eligible": not reason, "exclusion_reason": reason or None,
        }
        if reason:
            item.update({field: None for field in SCORE_FIELDS})
        else:
            scores = calculate_score(source, scenario, policy)
            raw_scores[source["candidate_id"]] = scores["final_score"]
            item.update({key: round(value, precision) for key, value in scores.items()})
        item.update({
            "priority": int(source["priority"]), "rank": None,
            "is_mock": as_bool(source["is_mock"]), "data_source": source["data_source"],
        })
        evaluated.append(item)

    eligible = [item for item in evaluated if item["eligible"]]
    eligible.sort(key=lambda item: (raw_scores[item["candidate_id"]], item["priority"], channel_sort_key(item["channel_id"])))
    for rank, item in enumerate(eligible, 1):
        item["rank"] = rank
    selected = eligible[0]["candidate_id"] if eligible else None
    outcome = "selected" if eligible else "unroutable"
    expected_selected = scenario["expected_selected_candidate"] or None
    matches = outcome == scenario["expected_status"] and selected == expected_selected
    return {
        "decision_id": f"{policy['policy_version']}-{scenario['scenario_id']}",
        "policy_version": policy["policy_version"], "scenario_id": scenario["scenario_id"],
        "strategy": scenario["strategy"], "requested_model": scenario["requested_model"],
        "stream_required": as_bool(scenario["stream_required"]),
        "input_tokens": int(scenario["input_tokens"]), "output_tokens": int(scenario["output_tokens"]),
        "currency": scenario["currency"], "decision_time": scenario["decision_time"],
        "outcome": outcome, "selected_candidate": selected,
        "expected_status": scenario["expected_status"],
        "expected_selected_candidate": expected_selected, "decision_matches_expected": matches,
        "candidate_count": len(evaluated), "eligible_count": len(eligible),
        "excluded_count": len(evaluated) - len(eligible), "candidates": evaluated,
    }


def run(scenario_id: str | None = None) -> list[dict[str, Any]]:
    policy, scenarios, candidate_rows = load_inputs()
    if scenario_id:
        scenarios = [row for row in scenarios if row["scenario_id"] == scenario_id]
        if not scenarios:
            raise ValueError(f"unknown scenario: {scenario_id}")
    results = []
    for scenario in scenarios:
        rows = [row for row in candidate_rows if row["scenario_id"] == scenario["scenario_id"]]
        if not rows:
            raise ValueError(f"no candidates for {scenario['scenario_id']}")
        result = decide_scenario(scenario, rows, policy)
        if not result["decision_matches_expected"]:
            raise ValueError(f"decision differs from expected for {scenario['scenario_id']}")
        results.append(result)
    return results


def summary_row(result: dict[str, Any]) -> dict[str, Any]:
    return {column: result[column] for column in SUMMARY_COLUMNS}


def write_outputs(results: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / JSON_NAME).open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with (output_dir / CSV_NAME).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        writer.writerows(summary_row(result) for result in results)


def print_summary(results: list[dict[str, Any]]) -> None:
    selected = sum(r["outcome"] == "selected" for r in results)
    matched = sum(r["decision_matches_expected"] for r in results)
    print(f"scenarios={len(results)} selected={selected} unroutable={len(results)-selected} matched={matched}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scenario")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    try:
        results = run(args.scenario)
        print_summary(results)
        if not args.dry_run:
            write_outputs(results, args.output_dir)
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
