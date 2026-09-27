"""Run seven strategies against one shared deterministic offline Mock workload."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import generate_simulation_workload as workload
import strategy_engine


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "simulation_experiment_v2.json"
DEFAULT_OUTPUT_DIR = ROOT / "output"
DECISIONS_NAME = "strategy_benchmark_decisions_v3.csv"
SUMMARY_NAME = "strategy_benchmark_summary_v3.csv"
JSON_NAME = "strategy_benchmark_v3.json"
DECISION_COLUMNS = [
    "experiment_id", "run_id", "catalog_version", "policy_version", "strategy_name",
    "strategy_definition_version", "weight_set_id", "candidate_snapshot_version",
    "simulation_config_version", "random_seed", "generated_at", "request_id",
    "request_index", "environment_regime", "outcome", "selected_candidate",
    "realized_success", "realized_latency_ms", "realized_cost", "sla_violated",
    "selection_reason", "is_mock", "is_mock_evaluation", "data_source",
]
SUMMARY_COLUMNS = [
    "experiment_id", "catalog_version", "policy_version", "strategy_name", "environment_regime",
    "request_count", "selected_count", "unroutable_count", "success_rate",
    "average_latency_ms", "p50_latency_ms", "p95_latency_ms", "p99_latency_ms",
    "average_cost", "cost_per_success", "sla_violation_rate", "selection_share",
    "selection_entropy", "latency_regret", "cost_regret", "composite_regret",
    "candidate_selection_distribution",
    "simulation_config_version", "is_mock", "is_mock_evaluation", "data_source",
]


def percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position)); upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def selection_entropy(counts: Counter[str]) -> float:
    total = sum(counts.values())
    if not total:
        return 0.0
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def request_dict(row: dict[str, str]) -> dict[str, Any]:
    return {
        "request_id": row["request_id"], "requested_model": row["requested_model"],
        "stream_required": row["stream_required"] == "TRUE", "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]), "currency": row["currency"],
        "decision_time": row["decision_time"],
    }


def load_or_generate() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    requests_path = ROOT / "data" / workload.REQUESTS_NAME
    outcomes_path = ROOT / "data" / workload.OUTCOMES_NAME
    if requests_path.exists() and outcomes_path.exists():
        return strategy_engine.load_csv(requests_path), strategy_engine.load_csv(outcomes_path)
    requests, outcomes = workload.generate()
    return [{k: str(v) for k, v in row.items()} for row in requests], [{k: str(v) for k, v in row.items()} for row in outcomes]


def evaluate_regret(selected: dict[str, str] | None, potential: list[dict[str, str]], sla_ms: float, cost_ref: float) -> tuple[float, float, float]:
    oracle = [row for row in potential if row["availability"] == "TRUE" and row["realized_success"] == "TRUE"]
    if selected is None or not oracle:
        return (0.0, 0.0, 0.0 if selected is None else 1.0)
    latency = float(selected["realized_latency_ms"]); cost = float(selected["realized_cost"])
    latency_regret = max(0.0, latency - min(float(row["realized_latency_ms"]) for row in oracle))
    cost_regret = max(0.0, cost - min(float(row["realized_cost"]) for row in oracle))
    failure = 0.0 if selected["realized_success"] == "TRUE" else 1.0
    return latency_regret, cost_regret, latency_regret / sla_ms + cost_regret / cost_ref + failure


def run_benchmark(
    requests: list[dict[str, str]] | None = None,
    outcomes: list[dict[str, str]] | None = None,
    strategies: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    config = strategy_engine.load_json(CONFIG_PATH)
    catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    policy = strategy_engine.load_json(strategy_engine.POLICY_PATH)
    base = strategy_engine.load_csv(workload.CANDIDATES_PATH)
    if requests is None or outcomes is None:
        requests, outcomes = load_or_generate()
    strategies = strategies or catalog["supported_strategies"]
    outcome_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in outcomes:
        outcome_index[row["request_id"]].append(row)
    decisions: list[dict[str, Any]] = []
    snapshot_cache = {name: workload.build_candidate_snapshot(base, regime) for name, regime in config["regimes"].items()}
    for strategy_name in strategies:
        for request_row in requests:
            index = int(request_row["request_index"])
            regime_name = request_row["environment_regime"]
            regime = config["regimes"][regime_name]
            candidates = snapshot_cache[regime_name]
            random_seed = int(config["strategy_random_seed"]) + index
            metadata = {
                "experiment_id": config["experiment_id"],
                "run_id": f"{config['experiment_id']}-{strategy_name}-{index:05d}",
                "strategy_definition_version": catalog["catalog_version"],
                "candidate_snapshot_version": config["candidate_snapshot_version"],
                "simulation_config_version": config["simulation_config_version"],
                "generated_at": config["generated_at"], "data_source": "offline_simulation_v3",
            }
            # Selection happens before potential outcomes are consulted.
            result = strategy_engine.strategy_decision(
                **request_dict(request_row), candidates=candidates, strategy_name=strategy_name,
                random_seed=random_seed, request_index=index, catalog=catalog, policy=policy,
                experiment_metadata=metadata,
            )
            selected_id = result["selected_candidate"]
            selected_outcome = next((row for row in outcome_index[request_row["request_id"]] if row["candidate_id"] == selected_id), None)
            latency_regret, cost_regret, composite = evaluate_regret(
                selected_outcome, outcome_index[request_row["request_id"]], float(regime["sla_latency_ms"]),
                float(policy["normalization"]["cost_reference_cny"]),
            )
            decisions.append({
                "experiment_id": config["experiment_id"], "run_id": metadata["run_id"],
                "catalog_version": catalog["catalog_version"], "policy_version": policy["policy_version"],
                "strategy_name": strategy_name, "strategy_definition_version": catalog["catalog_version"],
                "weight_set_id": result["weight_set_id"],
                "candidate_snapshot_version": config["candidate_snapshot_version"],
                "simulation_config_version": config["simulation_config_version"], "random_seed": random_seed,
                "generated_at": config["generated_at"], "request_id": request_row["request_id"],
                "request_index": index, "environment_regime": regime_name, "outcome": result["outcome"],
                "selected_candidate": selected_id or "", "realized_success": selected_outcome["realized_success"] if selected_outcome else "",
                "realized_latency_ms": selected_outcome["realized_latency_ms"] if selected_outcome else "",
                "realized_cost": selected_outcome["realized_cost"] if selected_outcome else "",
                "sla_violated": selected_outcome["sla_violated"] if selected_outcome else "",
                "selection_reason": result["selection_reason"], "latency_regret_value": latency_regret,
                "cost_regret_value": cost_regret, "composite_regret_value": composite,
                "is_mock": "TRUE", "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3",
            })
    return decisions, summarize(decisions, config, catalog, policy)


def summarize(decisions, config, catalog, policy):
    summaries = []
    for strategy_name in catalog["supported_strategies"]:
        strategy_rows = [row for row in decisions if row["strategy_name"] == strategy_name]
        if not strategy_rows:
            continue
        for regime_name in ["overall", *config["regimes"].keys()]:
            rows = strategy_rows if regime_name == "overall" else [r for r in strategy_rows if r["environment_regime"] == regime_name]
            selected = [r for r in rows if r["outcome"] == "selected"]
            latencies = [float(r["realized_latency_ms"]) for r in selected]
            costs = [float(r["realized_cost"]) for r in selected]
            successes = sum(r["realized_success"] == "TRUE" for r in selected)
            violations = sum(r["sla_violated"] == "TRUE" for r in selected)
            shares = Counter(r["selected_candidate"] for r in selected)
            share_text = json.dumps({key: round(value / len(selected), 6) for key, value in sorted(shares.items())}, separators=(",", ":")) if selected else "{}"
            summaries.append({
                "experiment_id": config["experiment_id"], "catalog_version": catalog["catalog_version"],
                "policy_version": policy["policy_version"], "strategy_name": strategy_name,
                "environment_regime": regime_name, "request_count": len(rows), "selected_count": len(selected),
                "unroutable_count": len(rows) - len(selected), "success_rate": successes / len(selected) if selected else 0.0,
                "average_latency_ms": statistics.fmean(latencies) if latencies else 0.0,
                "p50_latency_ms": percentile(latencies, .50), "p95_latency_ms": percentile(latencies, .95),
                "p99_latency_ms": percentile(latencies, .99), "average_cost": statistics.fmean(costs) if costs else 0.0,
                "cost_per_success": sum(costs) / successes if successes else 0.0,
                "sla_violation_rate": violations / len(selected) if selected else 0.0,
                "selection_share": share_text, "selection_entropy": selection_entropy(shares),
                "candidate_selection_distribution": share_text,
                "latency_regret": statistics.fmean(r["latency_regret_value"] for r in rows) if rows else 0.0,
                "cost_regret": statistics.fmean(r["cost_regret_value"] for r in rows) if rows else 0.0,
                "composite_regret": statistics.fmean(r["composite_regret_value"] for r in rows) if rows else 0.0,
                "simulation_config_version": config["simulation_config_version"], "is_mock": "TRUE",
                "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3",
            })
    return summaries


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def build(output_dir: Path = DEFAULT_OUTPUT_DIR, dry_run: bool = False):
    decisions, summaries = run_benchmark()
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_csv(output_dir / DECISIONS_NAME, DECISION_COLUMNS, decisions)
        write_csv(output_dir / SUMMARY_NAME, SUMMARY_COLUMNS, summaries)
        with (output_dir / JSON_NAME).open("w", encoding="utf-8", newline="\n") as handle:
            json.dump({"evaluation_type": "offline_mock_strategy_benchmark", "is_mock": True, "is_mock_evaluation": True,
                       "data_source": "offline_simulation_v3",
                       "decision_count": len(decisions), "summary": summaries, "decisions": decisions}, handle, ensure_ascii=False)
            handle.write("\n")
    return decisions, summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    decisions, summaries = build(args.output_dir, args.dry_run)
    print(f"decisions={len(decisions)} summaries={len(summaries)}" + (" dry_run=TRUE" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
