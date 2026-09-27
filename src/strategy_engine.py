"""Offline multi-strategy channel evaluator with Bayesian confidence scoring.

The module uses local files only. Sample comparison outputs are explicitly
offline Mock strategy evaluations, not real API performance observations.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import offline_decision_engine as legacy


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "config" / "strategy_catalog_v3.json"
POLICY_PATH = ROOT / "config" / "decision_policy_v2.json"
CANDIDATES_PATH = ROOT / "data" / "candidate_channels_v1.csv"
SCENARIOS_PATH = ROOT / "data" / "scenario_catalog_v1.csv"
DEFAULT_OUTPUT_DIR = ROOT / "output"
CSV_NAME = "strategy_comparison_sample_v2.csv"
JSON_NAME = "strategy_comparison_sample_v2.json"
CSV_COLUMNS = [
    "experiment_id", "run_id", "catalog_version", "policy_version", "strategy_name",
    "strategy_definition_version", "weight_set_id", "candidate_snapshot_version",
    "simulation_config_version", "random_seed", "generated_at", "is_mock_evaluation", "data_source",
    "outcome", "selected_candidate", "selected_rank",
    "selected_latency_ms", "estimated_cost", "raw_failure_risk",
    "bayesian_failure_risk", "final_score", "selection_reason",
    "request_index",
]


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def beta_risks(sample_size: float, success_rate: float, alpha: float = 1.0, beta: float = 1.0) -> dict[str, float]:
    success_count = sample_size * success_rate
    failure_count = sample_size - success_count
    return {
        "sample_size": sample_size,
        "success_rate": success_rate,
        "success_count": success_count,
        "failure_count": failure_count,
        "raw_failure_risk": 1.0 - success_rate,
        "bayesian_failure_risk": (failure_count + beta) / (sample_size + alpha + beta),
    }


def estimated_cost(candidate: dict[str, str], request: dict[str, Any]) -> float:
    return (
        float(request["input_tokens"]) * float(candidate["input_price_per_1m"])
        + float(request["output_tokens"]) * float(candidate["output_price_per_1m"])
    ) / 1_000_000


def request_as_scenario(request: dict[str, Any], strategy: str = "latency_first") -> dict[str, str]:
    return {
        "requested_model": str(request["requested_model"]),
        "stream_required": "TRUE" if bool(request["stream_required"]) else "FALSE",
        "input_tokens": str(request["input_tokens"]), "output_tokens": str(request["output_tokens"]),
        "currency": str(request["currency"]), "decision_time": str(request["decision_time"]),
        "strategy": strategy,
    }


def stable_candidate_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    return (int(candidate["priority"]), legacy.channel_sort_key(str(candidate["channel_id"])))


def strategy_decision(
    *, request_id: str, requested_model: str, stream_required: bool,
    input_tokens: int, output_tokens: int, currency: str, decision_time: str,
    candidates: list[dict[str, str]], strategy_name: str,
    strategy_config: dict[str, Any] | None = None, random_seed: int = 42,
    request_index: int = 0, catalog: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None, experiment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalog = catalog or load_json(CATALOG_PATH)
    policy = policy or load_json(POLICY_PATH)
    if strategy_name not in catalog["supported_strategies"]:
        raise ValueError(f"unknown strategy: {strategy_name}")
    metadata = catalog["strategies"][strategy_name]
    config = {**metadata, **(strategy_config or {})}
    trace = {
        "experiment_id": "SAMPLE-W3-V2",
        "run_id": f"SAMPLE-W3-V2-{strategy_name}",
        "strategy_definition_version": catalog["catalog_version"],
        "weight_set_id": config.get("weight_set_id", "not_applicable"),
        "candidate_snapshot_version": "candidate_channels_v1",
        "simulation_config_version": "not_applicable",
        "generated_at": "2026-07-22T00:00:00Z",
        **(experiment_metadata or {}),
    }
    request = {
        "request_id": request_id, "requested_model": requested_model,
        "stream_required": stream_required, "input_tokens": input_tokens,
        "output_tokens": output_tokens, "currency": currency, "decision_time": decision_time,
    }
    weighted_mode = config.get("weighted_substrategy", config.get("default_substrategy", "latency_first"))
    scenario = request_as_scenario(request, weighted_mode)
    details: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    prior = config.get("beta_prior", {"alpha": 1.0, "beta": 1.0})

    for source in deepcopy(candidates):
        reason = legacy.exclusion_reason(source, scenario, policy)
        if strategy_name == "confidence_aware_v3" and not reason:
            required_version = str(config["required_confidence_version"])
            if source.get("statistical_confidence_state") != "ready":
                reason = "statistical_confidence_not_ready"
            elif source.get("statistical_confidence_version") != required_version:
                reason = "statistical_confidence_version_mismatch"
            elif source.get("confidence_adjusted_success_score") in (None, ""):
                reason = "statistical_confidence_score_missing"
            elif source.get("confidence_effective_sample_size") in (None, ""):
                reason = "statistical_confidence_effective_sample_missing"
            else:
                adjusted = float(source["confidence_adjusted_success_score"])
                raw_observed = float(
                    source.get("observed_success_rate", source["success_rate"])
                )
                effective = float(source["confidence_effective_sample_size"])
                if (
                    not all(math.isfinite(value) for value in (
                        adjusted, raw_observed, effective))
                    or not 0 <= adjusted <= raw_observed <= 1
                    or effective <= 0
                ):
                    reason = "statistical_confidence_values_invalid"
        item: dict[str, Any] = {
            "candidate_id": source["candidate_id"], "channel_id": source["channel_id"],
            "eligible": not reason, "exclusion_reason": reason or None,
            "priority": int(source["priority"]), "rank": None,
            "is_mock": source["is_mock"].upper() == "TRUE", "data_source": source["data_source"],
            "latency_ms": float(source["latency_ms"]) if source["latency_ms"] else None,
            "estimated_cost": None, "sample_size": None, "success_rate": None,
            "success_count": None, "failure_count": None, "raw_failure_risk": None,
            "bayesian_failure_risk": None, "latency_norm": None, "cost_norm": None,
            "small_sample_penalty": None, "confidence_penalty": None,
            "evidence_penalty": None, "final_score": None,
        }
        if strategy_name == "confidence_aware_v3":
            item.update({
                "confidence_adjusted_success_score": None,
                "confidence_adjusted_failure_risk": None,
                "confidence_effective_sample_size": None,
                "raw_observed_success_rate": None,
                "statistical_confidence_version": source.get(
                    "statistical_confidence_version"),
            })
        if not reason:
            cost = estimated_cost(source, request)
            observed_success_rate = float(source.get("observed_success_rate", source["success_rate"]))
            if strategy_name == "confidence_aware_v3":
                item["raw_observed_success_rate"] = observed_success_rate
                adjusted_success = float(source["confidence_adjusted_success_score"])
                effective_sample = float(source["confidence_effective_sample_size"])
                risks = beta_risks(
                    effective_sample, adjusted_success, prior["alpha"], prior["beta"])
                item["confidence_adjusted_success_score"] = adjusted_success
                item["confidence_adjusted_failure_risk"] = 1.0 - adjusted_success
                item["confidence_effective_sample_size"] = effective_sample
            else:
                risks = beta_risks(float(source["sample_size"]), observed_success_rate, prior["alpha"], prior["beta"])
            latency_norm = min(float(source["latency_ms"]) / policy["normalization"]["latency_reference_ms"], 1.0)
            cost_norm = min(cost / policy["normalization"]["cost_reference_cny"], 1.0)
            item.update(risks)
            item.update({"estimated_cost": cost, "latency_norm": latency_norm, "cost_norm": cost_norm})
            eligible.append(item)
        else:
            excluded.append(item)
        details.append(item)

    selection_reason = "no_eligible_candidates"
    if eligible:
        tie = lambda item: (item["priority"], legacy.channel_sort_key(str(item["channel_id"])))
        if strategy_name == "random_baseline":
            eligible.sort(key=stable_candidate_key)
            random.Random(random_seed).shuffle(eligible)
            selection_reason = f"seeded_random_choice(seed={random_seed})"
        elif strategy_name == "round_robin":
            eligible.sort(key=stable_candidate_key)
            position = request_index % len(eligible)
            eligible = eligible[position:] + eligible[:position]
            selection_reason = f"round_robin(request_index={request_index},position={position})"
        elif strategy_name in {"latency_first", "cost_first"}:
            policy_strategy_id = str(config.get("policy_strategy_id") or strategy_name)
            if policy_strategy_id != strategy_name or config.get("legacy_alias") is not False:
                raise ValueError(f"invalid first-class strategy metadata: {strategy_name}")
            for item, source in (
                (item, next(c for c in candidates if c["candidate_id"] == item["candidate_id"]))
                for item in eligible
            ):
                scores = legacy.calculate_score(
                    source, request_as_scenario(request, policy_strategy_id), policy)
                item.update(scores)
            eligible.sort(key=lambda item: (item["final_score"], *tie(item)))
            selection_reason = f"{strategy_name}_versioned_weighted_minimum_score"
        elif strategy_name == "fastest_first":
            eligible.sort(key=lambda item: (item["latency_ms"], *tie(item)))
            selection_reason = "minimum_latency_then_tie_breakers"
        elif strategy_name == "cheapest_first":
            eligible.sort(key=lambda item: (item["estimated_cost"], *tie(item)))
            selection_reason = "minimum_estimated_cost_then_tie_breakers"
        elif strategy_name == "reliability_first":
            eligible.sort(key=lambda item: (item["raw_failure_risk"], -item["sample_size"], *tie(item)))
            selection_reason = "minimum_raw_failure_risk_then_sample_size_and_tie_breakers"
        elif strategy_name == "weighted_v1":
            for item, source in ((item, next(c for c in candidates if c["candidate_id"] == item["candidate_id"])) for item in eligible):
                scores = legacy.calculate_score(source, scenario, policy)
                item.update(scores)
            eligible.sort(key=lambda item: (item["final_score"], *tie(item)))
            selection_reason = f"weighted_v1_{weighted_mode}_minimum_score"
        elif strategy_name == "confidence_aware_v2":
            weights = config["weights"]
            for item in eligible:
                item["small_sample_penalty"] = 0.0
                item["confidence_penalty"] = 0.0
                item["evidence_penalty"] = 0.0
                item["final_score"] = (
                    item["latency_norm"] * weights["latency"]
                    + item["cost_norm"] * weights["cost"]
                    + item["bayesian_failure_risk"] * weights["failure_risk"]
                )
            eligible.sort(key=lambda item: (item["final_score"], *tie(item)))
            selection_reason = "minimum_bayesian_confidence_aware_score"
        elif strategy_name == "confidence_aware_v3":
            weights = config["weights"]
            for item in eligible:
                item["small_sample_penalty"] = (
                    item["raw_observed_success_rate"]
                    - item["confidence_adjusted_success_score"]
                )
                item["confidence_penalty"] = item["small_sample_penalty"]
                item["evidence_penalty"] = 0.0
                item["final_score"] = (
                    item["latency_norm"] * weights["latency"]
                    + item["cost_norm"] * weights["cost"]
                    + item["confidence_adjusted_failure_risk"]
                    * weights["failure_risk"]
                )
            eligible.sort(key=lambda item: (item["final_score"], *tie(item)))
            selection_reason = (
                "minimum_weighted_wilson_confidence_aware_score"
            )
        for rank, item in enumerate(eligible, 1):
            item["rank"] = rank

    selected = eligible[0] if eligible else None
    return {
        "request_id": request_id, "strategy_name": strategy_name,
        "outcome": "selected" if selected else "unroutable",
        "selected_candidate": selected["candidate_id"] if selected else None,
        "ranked_candidates": eligible, "excluded_candidates": excluded,
        "candidate_details": details, "selection_reason": selection_reason,
        "random_seed": random_seed if metadata["requires_seed"] else None,
        "request_index": request_index if metadata["requires_request_index"] else request_index,
        "experiment_id": trace["experiment_id"], "run_id": trace["run_id"],
        "policy_version": policy["policy_version"], "catalog_version": catalog["catalog_version"],
        "strategy_definition_version": trace["strategy_definition_version"],
        "weight_set_id": trace["weight_set_id"],
        "candidate_snapshot_version": trace["candidate_snapshot_version"],
        "simulation_config_version": trace["simulation_config_version"],
        "generated_at": trace["generated_at"],
        "is_mock": True, "is_mock_evaluation": True,
        "data_source": trace.get("data_source", "offline_strategy_evaluation_v2"),
    }


def sample_request_and_candidates() -> tuple[dict[str, Any], list[dict[str, str]]]:
    scenarios = load_csv(SCENARIOS_PATH)
    base = load_csv(CANDIDATES_PATH)
    s001 = next(row for row in scenarios if row["scenario_id"] == "S001")
    request = {
        "request_id": "STRATEGY-SAMPLE-V2", "requested_model": "deepseek-v4-flash",
        "stream_required": False, "input_tokens": 1000, "output_tokens": 500,
        "currency": "CNY", "decision_time": s001["decision_time"],
    }
    return request, base


def run_comparison(strategy_name: str | None = None, seed: int = 42, request_index: int = 0) -> list[dict[str, Any]]:
    catalog = load_json(CATALOG_PATH)
    policy = load_json(POLICY_PATH)
    request, candidates = sample_request_and_candidates()
    names = [strategy_name] if strategy_name else catalog["supported_strategies"]
    return [strategy_decision(**request, candidates=candidates, strategy_name=name, random_seed=seed,
                              request_index=request_index, catalog=catalog, policy=policy) for name in names]


def comparison_row(result: dict[str, Any]) -> dict[str, Any]:
    selected = result["ranked_candidates"][0] if result["ranked_candidates"] else None
    return {
        "experiment_id": result["experiment_id"], "run_id": result["run_id"],
        "catalog_version": result["catalog_version"], "policy_version": result["policy_version"],
        "strategy_name": result["strategy_name"], "outcome": result["outcome"],
        "strategy_definition_version": result["strategy_definition_version"],
        "weight_set_id": result["weight_set_id"],
        "candidate_snapshot_version": result["candidate_snapshot_version"],
        "simulation_config_version": result["simulation_config_version"],
        "random_seed": result["random_seed"] if result["random_seed"] is not None else "",
        "generated_at": result["generated_at"], "is_mock_evaluation": "TRUE",
        "data_source": result["data_source"],
        "selected_candidate": result["selected_candidate"] or "",
        "selected_rank": selected["rank"] if selected else "",
        "selected_latency_ms": selected["latency_ms"] if selected else "",
        "estimated_cost": selected["estimated_cost"] if selected else "",
        "raw_failure_risk": selected["raw_failure_risk"] if selected else "",
        "bayesian_failure_risk": selected["bayesian_failure_risk"] if selected else "",
        "final_score": selected["final_score"] if selected and selected["final_score"] is not None else "",
        "selection_reason": result["selection_reason"],
        "request_index": result["request_index"],
    }


def write_outputs(results: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / JSON_NAME).open("w", encoding="utf-8", newline="\n") as handle:
        json.dump({"evaluation_type": "offline_mock_strategy_comparison", "is_mock_evaluation": True,
                   "results": results}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with (output_dir / CSV_NAME).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(comparison_row(result) for result in results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-strategies", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strategy")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--request-index", type=int, default=0)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    try:
        catalog = load_json(CATALOG_PATH)
        if args.list_strategies:
            print("\n".join(catalog["supported_strategies"]))
            return 0
        results = run_comparison(args.strategy, args.seed, args.request_index)
        for result in results:
            print(f"{result['strategy_name']}: {result['outcome']} -> {result['selected_candidate']}")
        if not args.dry_run:
            write_outputs(results, args.output_dir)
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
