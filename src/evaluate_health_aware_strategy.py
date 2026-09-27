"""Compare confidence_aware_v2 and health_aware_v1 on existing offline simulation."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import channel_health
import generate_simulation_workload as workload
import offline_decision_engine as legacy
import run_strategy_benchmark as benchmark
import strategy_engine
from strategies.health_aware_strategy import rank_candidates


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "health_aware_policy_v1.json"
DEFAULT_JSON = ROOT / "output" / "health_aware_strategy_comparison_v1.json"
DEFAULT_CSV = ROOT / "output" / "health_aware_strategy_comparison_v1.csv"
LABEL = "offline demonstration only"


def _health_candidates(
    candidates: list[dict[str, str]], request: dict[str, Any],
    decision_policy: dict[str, Any], health_policy: dict[str, Any],
) -> list[dict[str, Any]]:
    scenario = strategy_engine.request_as_scenario(request)
    eligible = []
    for source in candidates:
        if legacy.exclusion_reason(source, scenario, decision_policy):
            continue
        estimated_cost = strategy_engine.estimated_cost(source, request)
        success_rate = float(source.get("observed_success_rate", source["success_rate"]))
        health_input = {
            "sample_count": int(float(source["sample_size"])),
            "metrics": {
                "success_rate": success_rate,
                "avg_latency_ms": float(source["latency_ms"]),
                "p95_latency_ms": float(source["latency_ms"]),
                "avg_cost": estimated_cost,
            },
        }
        scores = channel_health.calculate_health_score(health_input, health_policy)
        eligible.append({
            **source,
            "health_score": scores["health_score"],
            "reliability": 1.0 - success_rate,
            "latency_score": min(
                float(source["latency_ms"])
                / float(decision_policy["normalization"]["latency_reference_ms"]), 1.0
            ),
            "cost_score": min(
                estimated_cost
                / float(decision_policy["normalization"]["cost_reference_cny"]), 1.0
            ),
        })
    return eligible


def evaluate() -> dict[str, Any]:
    requests, outcomes = benchmark.load_or_generate()
    config = strategy_engine.load_json(benchmark.CONFIG_PATH)
    catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    decision_policy = strategy_engine.load_json(strategy_engine.POLICY_PATH)
    health_policy = channel_health.load_json(channel_health.DEFAULT_POLICY_PATH)
    routing_policy = strategy_engine.load_json(POLICY_PATH)
    base = strategy_engine.load_csv(workload.CANDIDATES_PATH)
    outcome_index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in outcomes:
        outcome_index[row["request_id"]].append(row)
    snapshots = {
        name: workload.build_candidate_snapshot(base, regime)
        for name, regime in config["regimes"].items()
    }
    decisions = []
    for strategy_name in ("confidence_aware_v2", "health_aware_v1"):
        for request_row in requests:
            request = benchmark.request_dict(request_row)
            candidates = snapshots[request_row["environment_regime"]]
            if strategy_name == "confidence_aware_v2":
                result = strategy_engine.strategy_decision(
                    **request, candidates=candidates, strategy_name=strategy_name,
                    catalog=catalog, policy=decision_policy,
                    request_index=int(request_row["request_index"]),
                )
                selected_id = result["selected_candidate"]
            else:
                ranked = rank_candidates(
                    _health_candidates(candidates, request, decision_policy, health_policy),
                    routing_policy,
                )
                selected_id = ranked[0]["candidate_id"] if ranked else None
            potential = outcome_index[request_row["request_id"]]
            selected = next(
                (row for row in potential if row["candidate_id"] == selected_id), None
            )
            regime = config["regimes"][request_row["environment_regime"]]
            latency_regret, cost_regret, composite = benchmark.evaluate_regret(
                selected, potential, float(regime["sla_latency_ms"]),
                float(decision_policy["normalization"]["cost_reference_cny"]),
            )
            decisions.append({
                "strategy": strategy_name,
                "request_id": request_row["request_id"],
                "selected_candidate": selected_id,
                "success": selected["realized_success"] == "TRUE" if selected else False,
                "latency_ms": float(selected["realized_latency_ms"]) if selected else 0.0,
                "cost": float(selected["realized_cost"]) if selected else 0.0,
                "regret": composite,
                "latency_regret": latency_regret,
                "cost_regret": cost_regret,
            })
    summary = []
    for name in ("confidence_aware_v2", "health_aware_v1"):
        rows = [row for row in decisions if row["strategy"] == name]
        summary.append({
            "strategy": name,
            "request_count": len(rows),
            "success_rate": sum(row["success"] for row in rows) / len(rows),
            "average_latency_ms": statistics.fmean(row["latency_ms"] for row in rows),
            "average_cost": statistics.fmean(row["cost"] for row in rows),
            "average_regret": statistics.fmean(row["regret"] for row in rows),
        })
    return {
        "evaluation_type": LABEL,
        "is_mock": True,
        "real_api_calls_performed": 0,
        "summary": summary,
    }


def write_outputs(result: dict[str, Any], json_path: Path, csv_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result["summary"][0]))
        writer.writeheader()
        writer.writerows(result["summary"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--csv-output", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = evaluate()
        if not args.dry_run:
            write_outputs(result, args.json_output, args.csv_output)
        print(json.dumps({
            "status": "ok", "strategies": 2,
            "evaluation_type": LABEL, "real_api_calls_performed": 0,
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
