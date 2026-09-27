"""Offline model selection combining capability, assumed cost, and latency."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from model_capability_router import (
    DEFAULT_CATALOG_PATH,
    compatibility_score,
    load_catalog,
)
from task_classifier import build_task_profile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COST_CATALOG = ROOT / "data" / "model_cost_catalog_v1.json"
DEFAULT_POLICY = ROOT / "config" / "model_cost_policy_v1.json"
DEFAULT_OUTPUT = ROOT / "output" / "model_cost_decision_demo_v1.json"
COMPONENTS = ("capability", "cost", "latency")


class CostAwareModelError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def validate_cost_catalog(catalog: Mapping[str, Any]) -> None:
    models = catalog.get("models", [])
    identifiers = []
    for model in models:
        required = {
            "model_id", "input_price", "output_price", "currency",
            "cost_level", "latency_level",
        }
        if required - set(model):
            raise CostAwareModelError("cost catalog model is missing required fields")
        if float(model["input_price"]) < 0 or float(model["output_price"]) < 0:
            raise CostAwareModelError("model prices must be non-negative")
        identifiers.append(model["model_id"])
    if not models or len(identifiers) != len(set(identifiers)):
        raise CostAwareModelError("cost catalog model_id must be non-empty and unique")


def validate_policy(policy: Mapping[str, Any]) -> None:
    objectives = policy.get("objectives", {})
    if set(objectives) != {"quality_first", "cost_first", "balanced"}:
        raise CostAwareModelError("policy must define three routing objectives")
    for objective, config in objectives.items():
        weights = config.get("weights", {})
        if set(weights) != set(COMPONENTS):
            raise CostAwareModelError(f"{objective} has invalid weight fields")
        if abs(sum(float(weights[key]) for key in COMPONENTS) - 1.0) > 1e-9:
            raise CostAwareModelError(f"{objective} weights must sum to 1")
    normalization = policy["price_normalization"]
    if (
        float(normalization["input_reference"]) <= 0
        or float(normalization["output_reference"]) <= 0
        or abs(
            float(normalization["input_share"])
            + float(normalization["output_share"]) - 1.0
        ) > 1e-9
    ):
        raise CostAwareModelError("invalid price normalization")


def _cost_score(model: Mapping[str, Any], policy: Mapping[str, Any]) -> float:
    config = policy["price_normalization"]
    penalty = (
        float(config["input_share"])
        * min(float(model["input_price"]) / float(config["input_reference"]), 1.0)
        + float(config["output_share"])
        * min(float(model["output_price"]) / float(config["output_reference"]), 1.0)
    )
    return max(0.0, 1.0 - penalty)


def select_model(
    task_profile: Mapping[str, Any],
    capability_catalog: Mapping[str, Any] | None = None,
    cost_catalog: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    *,
    objective: str | None = None,
) -> dict[str, Any]:
    capability_catalog = capability_catalog or load_catalog(DEFAULT_CATALOG_PATH)
    cost_catalog = cost_catalog or load_json(DEFAULT_COST_CATALOG)
    policy = policy or load_json(DEFAULT_POLICY)
    validate_cost_catalog(cost_catalog)
    validate_policy(policy)
    objective = objective or str(policy["default_objective"])
    if objective not in policy["objectives"]:
        raise CostAwareModelError(f"unknown objective: {objective}")
    requirements = task_profile.get("requirements", {})
    if not requirements:
        raise CostAwareModelError("task_profile requires requirements")
    cost_by_id = {row["model_id"]: row for row in cost_catalog["models"]}
    capability_ids = {row["model_id"] for row in capability_catalog["models"]}
    if capability_ids != set(cost_by_id):
        raise CostAwareModelError("capability and cost catalogs must cover identical models")
    weights = policy["objectives"][objective]["weights"]
    requirement_total = sum(float(value) for value in requirements.values())
    ranking = []
    for model in capability_catalog["models"]:
        raw_capability, capability_explanation = compatibility_score(
            requirements, model
        )
        capability = raw_capability / requirement_total
        cost_model = cost_by_id[model["model_id"]]
        cost = _cost_score(cost_model, policy)
        latency = float(
            policy["latency_level_scores"][cost_model["latency_level"]]
        )
        total = (
            float(weights["capability"]) * capability
            + float(weights["cost"]) * cost
            + float(weights["latency"]) * latency
        )
        ranking.append({
            "model": model["model_id"],
            "score": round(total, 6),
            "score_breakdown": {
                "capability_score": round(capability, 6),
                "cost_score": round(cost, 6),
                "latency_score": round(latency, 6),
                "weighted_capability": round(float(weights["capability"]) * capability, 6),
                "weighted_cost": round(float(weights["cost"]) * cost, 6),
                "weighted_latency": round(float(weights["latency"]) * latency, 6),
            },
            "capability_explanation": capability_explanation,
            "assumed_input_price": cost_model["input_price"],
            "assumed_output_price": cost_model["output_price"],
            "currency": cost_model["currency"],
        })
    ranking.sort(key=lambda row: (-row["score"], row["model"]))
    selected = ranking[0]
    return {
        "selected_model": selected["model"],
        "ranking": ranking,
        "score_breakdown": selected["score_breakdown"],
        "explanation": (
            f"{objective}: capability={selected['score_breakdown']['capability_score']}, "
            f"cost={selected['score_breakdown']['cost_score']}, "
            f"latency={selected['score_breakdown']['latency_score']}"
        ),
        "objective": objective,
        "policy_version": policy["policy_version"],
        "source_type": "offline_configured_model_selection",
    }


def build_demo() -> dict[str, Any]:
    scenarios = [
        ("coding_request", "Implement Python code and debug this function", "quality_first"),
        ("simple_question", "What is a transformer?", "balanced"),
        ("long_document_analysis", "Summarize this long document", "balanced"),
    ]
    rows = []
    for scenario_id, text, objective in scenarios:
        profile = build_task_profile(text)
        rows.append({
            "scenario_id": scenario_id,
            "request_text": text,
            "task": profile,
            **select_model(profile, objective=objective),
        })
    return {
        "demonstration_type": "offline_only",
        "real_api_calls_performed": 0,
        "scenarios": rows,
        "limitations": [
            "Capability, price, and latency values are engineering assumptions.",
            "Selections do not represent real benchmark or production results."
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        output = build_demo()
        if not args.dry_run:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps({
            "status": "ok", "scenario_count": len(output["scenarios"]),
            "dry_run": args.dry_run, "real_api_calls_performed": 0,
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
