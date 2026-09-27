"""Evaluate all 0.1-grid confidence-aware weights on the fixed Mock workload."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import generate_simulation_workload as workload
import run_strategy_benchmark as benchmark
import strategy_engine


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "output"
SUMMARY_NAME = "weight_sensitivity_summary_v2.csv"
BY_REGIME_NAME = "weight_sensitivity_by_regime_v2.csv"
JSON_NAME = "weight_sensitivity_v2.json"
TOP_OVERALL_NAME = "top_weights_overall_v3.csv"
TOP_REGIME_NAME = "top_weights_by_regime_v3.csv"
ROBUST_NAME = "robust_weight_sets_v3.csv"
COLUMNS = [
    "weight_set_id", "latency_weight", "cost_weight", "failure_risk_weight",
    "environment_regime", "success_rate", "average_latency_ms", "p95_latency_ms",
    "average_cost", "sla_violation_rate", "composite_regret", "rank_within_regime",
    "dense_rank", "rank_min", "rank_max", "tied_weight_count",
    "selection_signature", "pareto_optimal",
    "robustness_score", "experiment_id", "catalog_version", "policy_version",
    "simulation_config_version", "is_mock", "is_mock_evaluation", "data_source",
]


def weight_grid() -> list[dict[str, Any]]:
    result = []
    number = 1
    for latency_tenths in range(11):
        for cost_tenths in range(11 - latency_tenths):
            failure_tenths = 10 - latency_tenths - cost_tenths
            result.append({
                "weight_set_id": f"W{number:03d}", "latency": latency_tenths / 10,
                "cost": cost_tenths / 10, "failure_risk": failure_tenths / 10,
            })
            number += 1
    assert len(result) == 66
    return result


def evaluate_weights(requests=None, outcomes=None, weights=None):
    config = strategy_engine.load_json(ROOT / "config" / "simulation_experiment_v2.json")
    catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    policy = strategy_engine.load_json(strategy_engine.POLICY_PATH)
    base = strategy_engine.load_csv(workload.CANDIDATES_PATH)
    if requests is None or outcomes is None:
        requests, outcomes = benchmark.load_or_generate()
    weights = weights or weight_grid()
    outcome_index = defaultdict(list)
    for row in outcomes:
        outcome_index[row["request_id"]].append(row)
    # Precompute only decision-time features once per request. Potential outcomes
    # are deliberately absent from these candidate feature records.
    prepared = []
    for request_row in requests:
        regime_name = request_row["environment_regime"]
        regime = config["regimes"][regime_name]
        candidates = workload.build_candidate_snapshot(base, regime)
        base_result = strategy_engine.strategy_decision(
            **benchmark.request_dict(request_row), candidates=candidates,
            strategy_name="confidence_aware_v2", random_seed=int(config["strategy_random_seed"]),
            request_index=int(request_row["request_index"]), catalog=catalog, policy=policy,
        )
        features = [{key: item[key] for key in (
            "candidate_id", "channel_id", "priority", "latency_norm", "cost_norm", "bayesian_failure_risk"
        )} for item in base_result["ranked_candidates"]]
        prepared.append((request_row, regime_name, regime, features))
    by_regime_rows = []
    overall_rows = []
    for weight in weights:
        observations = []
        selections = []
        for request_row, regime_name, regime, features in prepared:
            def score(item):
                return (item["latency_norm"] * weight["latency"]
                        + item["cost_norm"] * weight["cost"]
                        + item["bayesian_failure_risk"] * weight["failure_risk"])
            chosen = min(features, key=lambda item: (score(item), item["priority"], strategy_engine.legacy.channel_sort_key(str(item["channel_id"])))) if features else None
            selected = next((row for row in outcome_index[request_row["request_id"]] if chosen and row["candidate_id"] == chosen["candidate_id"]), None)
            _, _, composite = benchmark.evaluate_regret(
                selected, outcome_index[request_row["request_id"]], float(regime["sla_latency_ms"]),
                float(policy["normalization"]["cost_reference_cny"]),
            )
            observations.append({
                "regime": regime_name, "success": selected is not None and selected["realized_success"] == "TRUE",
                "latency": float(selected["realized_latency_ms"]) if selected else 0.0,
                "cost": float(selected["realized_cost"]) if selected else 0.0,
                "sla": selected is None or selected["sla_violated"] == "TRUE", "composite": composite,
            })
            selections.append(chosen["candidate_id"] if chosen else "UNROUTABLE")
        for regime_name in config["regimes"]:
            rows = [r for r in observations if r["regime"] == regime_name]
            selected_ids = [s for s, r in zip(selections, observations) if r["regime"] == regime_name]
            signature = hashlib.sha256("\n".join(selected_ids).encode()).hexdigest()
            by_regime_rows.append(metric_row(weight, regime_name, rows, config, catalog, policy, signature))
        signature = hashlib.sha256("\n".join(selections).encode()).hexdigest()
        overall_rows.append(metric_row(weight, "overall", observations, config, catalog, policy, signature))
    apply_ranks(by_regime_rows, overall_rows, float(config.get("ranking_epsilon", 1e-9)))
    return overall_rows, by_regime_rows


def metric_row(weight, regime_name, rows, config, catalog, policy, signature):
    latencies = [r["latency"] for r in rows]
    costs = [r["cost"] for r in rows]
    count = len(rows)
    return {
        "weight_set_id": weight["weight_set_id"], "latency_weight": weight["latency"],
        "cost_weight": weight["cost"], "failure_risk_weight": weight["failure_risk"],
        "environment_regime": regime_name, "success_rate": sum(r["success"] for r in rows) / count if count else 0.0,
        "average_latency_ms": statistics.fmean(latencies) if latencies else 0.0, "p95_latency_ms": benchmark.percentile(latencies, .95),
        "average_cost": statistics.fmean(costs) if costs else 0.0, "sla_violation_rate": sum(r["sla"] for r in rows) / count if count else 0.0,
        "composite_regret": statistics.fmean(r["composite"] for r in rows) if rows else 0.0,
        "rank_within_regime": 0, "dense_rank": 0, "rank_min": 0, "rank_max": 0,
        "tied_weight_count": 0, "selection_signature": signature, "pareto_optimal": "FALSE",
        "robustness_score": 0.0,
        "experiment_id": config["experiment_id"], "catalog_version": catalog["catalog_version"],
        "policy_version": policy["policy_version"], "simulation_config_version": config["simulation_config_version"],
        "is_mock": "TRUE", "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3",
    }


def _rank_rows(rows, epsilon):
    ordered = sorted(rows, key=lambda r: (r["composite_regret"], r["weight_set_id"]))
    groups = []
    for row in ordered:
        if not groups or abs(row["composite_regret"] - groups[-1][0]["composite_regret"]) > epsilon:
            groups.append([row])
        else:
            groups[-1].append(row)
    position = 1
    for dense_rank, group in enumerate(groups, 1):
        rank_min, rank_max = position, position + len(group) - 1
        for row in group:
            row.update({"rank_within_regime": rank_min, "dense_rank": dense_rank,
                        "rank_min": rank_min, "rank_max": rank_max,
                        "tied_weight_count": len(group)})
        position = rank_max + 1


def _apply_pareto(rows, epsilon):
    metrics = ("average_latency_ms", "average_cost", "sla_violation_rate", "composite_regret")
    for row in rows:
        dominated = any(
            other is not row
            and all(other[m] <= row[m] + epsilon for m in metrics)
            and any(other[m] < row[m] - epsilon for m in metrics)
            for other in rows
        )
        row["pareto_optimal"] = "FALSE" if dominated else "TRUE"


def apply_ranks(by_regime_rows, overall_rows, epsilon=1e-9):
    ranks = defaultdict(list)
    for regime in sorted({r["environment_regime"] for r in by_regime_rows}):
        rows = [r for r in by_regime_rows if r["environment_regime"] == regime]
        _rank_rows(rows, epsilon)
        _apply_pareto(rows, epsilon)
        for row in rows:
            ranks[row["weight_set_id"]].append(row["rank_min"])
    for row in by_regime_rows:
        avg_rank = statistics.fmean(ranks[row["weight_set_id"]])
        row["robustness_score"] = 1.0 - (avg_rank - 1.0) / 65.0
    _rank_rows(overall_rows, epsilon)
    _apply_pareto(overall_rows, epsilon)
    for row in overall_rows:
        row["robustness_score"] = 1.0 - (statistics.fmean(ranks[row["weight_set_id"]]) - 1.0) / 65.0


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader(); writer.writerows(rows)


def write_rows(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def build(output_dir: Path = DEFAULT_OUTPUT_DIR, dry_run: bool = False):
    overall, by_regime = evaluate_weights()
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_csv(output_dir / SUMMARY_NAME, overall)
        write_csv(output_dir / BY_REGIME_NAME, by_regime)
        write_rows(output_dir / TOP_OVERALL_NAME, sorted(overall, key=lambda r: (r["rank_min"], r["weight_set_id"]))[:10])
        top_regime = []
        for regime in config_regimes():
            top_regime.extend(sorted((r for r in by_regime if r["environment_regime"] == regime),
                                     key=lambda r: (r["rank_min"], r["weight_set_id"]))[:5])
        write_rows(output_dir / TOP_REGIME_NAME, top_regime)
        robust = sorted(overall, key=lambda r: (-r["robustness_score"], r["rank_min"], r["weight_set_id"]))
        write_rows(output_dir / ROBUST_NAME, robust)
        with (output_dir / JSON_NAME).open("w", encoding="utf-8", newline="\n") as handle:
            json.dump({"evaluation_type": "offline_mock_weight_sensitivity", "is_mock_evaluation": True,
                       "weight_set_count": len(overall), "overall": overall, "by_regime": by_regime}, handle, ensure_ascii=False)
            handle.write("\n")
    return overall, by_regime


def config_regimes():
    return strategy_engine.load_json(ROOT / "config" / "simulation_experiment_v2.json")["regimes"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    overall, by_regime = build(args.output_dir, args.dry_run)
    print(f"weight_sets={len(overall)} regime_rows={len(by_regime)}" + (" dry_run=TRUE" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
