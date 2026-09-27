"""Offline historical analysis that emits recommendations without mutations."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping

from routing_recommendation import format_routing_suggestion


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FEEDBACK = ROOT / "data" / "user_feedback_v1.json"
DEFAULT_EVALUATIONS = ROOT / "output" / "model_evaluation_results_v1.json"
DEFAULT_OUTPUT = ROOT / "output" / "adaptive_learning_report_v1.json"


def _mean(values: list[float]) -> float | None:
    return round(fmean(values), 6) if values else None


def analyze_history(
    feedback_records: list[Mapping[str, Any]],
    evaluation_results: list[Mapping[str, Any]],
    task_history: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate model/task performance from the supplied offline histories."""
    groups: dict[tuple[str, str], dict[str, list[Any]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in feedback_records:
        key = (str(row["task_type"]), str(row["selected_model"]))
        groups[key]["success"].append(float(bool(row["success"])))
        groups[key]["latency"].append(float(row["latency_ms"]))
        groups[key]["cost"].append(float(row["cost"]))
        groups[key]["rating"].append(float(row["user_rating"]))
        groups[key]["observations"].append("feedback")
        groups[key]["selections"].append(1)
    for row in evaluation_results:
        key = (str(row["task_type"]), str(row["model"]))
        groups[key]["evaluation"].append(float(row["evaluation_score"]))
        groups[key]["observations"].append("evaluation")
    for row in task_history:
        model = row.get("selected_model") or row.get("model")
        if not model or not row.get("task_type"):
            continue
        key = (str(row["task_type"]), str(model))
        groups[key]["observations"].append("task_history")
        groups[key]["selections"].append(1)
        for source, target in (
            ("success", "success"), ("evaluation_score", "evaluation"),
            ("latency_ms", "latency"), ("cost", "cost"),
            ("user_rating", "rating"),
        ):
            if row.get(source) is not None:
                value = float(bool(row[source])) if source == "success" else float(row[source])
                groups[key][target].append(value)
    return [
        {
            "task_type": task,
            "model_id": model,
            "sample_count": len(values["observations"]),
            "selection_count": len(values["selections"]),
            "success_rate": _mean(values["success"]),
            "evaluation_score": _mean(values["evaluation"]),
            "average_latency": _mean(values["latency"]),
            "average_cost": _mean(values["cost"]),
            "average_user_rating": _mean(values["rating"]),
        }
        for (task, model), values in sorted(groups.items())
    ]


def _performance_score(row: Mapping[str, Any]) -> float:
    parts: list[tuple[float, float]] = []
    if row.get("success_rate") is not None:
        parts.append((0.3, float(row["success_rate"])))
    if row.get("evaluation_score") is not None:
        parts.append((0.3, float(row["evaluation_score"])))
    if row.get("average_user_rating") is not None:
        parts.append((0.2, float(row["average_user_rating"]) / 5.0))
    if row.get("average_latency") is not None:
        parts.append((0.1, 1.0 / (1.0 + float(row["average_latency"]) / 1000.0)))
    if row.get("average_cost") is not None:
        parts.append((0.1, 1.0 / (1.0 + float(row["average_cost"]) * 100.0)))
    total_weight = sum(weight for weight, _ in parts)
    return round(
        sum(weight * value for weight, value in parts) / total_weight
        if total_weight else 0.0,
        6,
    )


def compare_models(
    history_analysis: list[Mapping[str, Any]],
    *,
    minimum_sample_count: int = 5,
    advantage_threshold: float = 0.03,
) -> list[dict[str, Any]]:
    if minimum_sample_count <= 0:
        raise ValueError("minimum_sample_count must be positive")
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history_analysis:
        by_task[str(row["task_type"])].append({
            **dict(row), "performance_score": _performance_score(row)
        })
    comparisons = []
    for task, rows in sorted(by_task.items()):
        current = sorted(
            rows,
            key=lambda row: (
                -int(row["selection_count"]), -int(row["sample_count"]),
                row["model_id"],
            ),
        )[0]
        best = sorted(
            rows,
            key=lambda row: (-row["performance_score"], row["model_id"]),
        )[0]
        evidence_count = min(int(current["sample_count"]), int(best["sample_count"]))
        confidence = (
            "high" if evidence_count >= 20
            else "medium" if evidence_count >= minimum_sample_count
            else "low"
        )
        advantage = round(
            best["performance_score"] - current["performance_score"], 6
        )
        if evidence_count < minimum_sample_count:
            action = "insufficient_samples"
        elif best["model_id"] != current["model_id"] and advantage >= advantage_threshold:
            action = "increase_candidate_priority"
        else:
            action = "retain_current_preference"
        comparisons.append({
            "task_type": task,
            "current_preference": current["model_id"],
            "best_model": best["model_id"],
            "current_score": current["performance_score"],
            "best_score": best["performance_score"],
            "score_advantage": advantage,
            "confidence": confidence,
            "sample_count_for_confidence": evidence_count,
            "action": action,
        })
    return comparisons


def generate_recommendations(
    history_analysis: list[Mapping[str, Any]],
    *,
    minimum_sample_count: int = 5,
) -> list[dict[str, Any]]:
    return [
        {
            **comparison,
            "recommendation": format_routing_suggestion(comparison),
            "automatic_weight_update": False,
        }
        for comparison in compare_models(
            history_analysis, minimum_sample_count=minimum_sample_count
        )
    ]


def build_report(
    feedback_path: Path = DEFAULT_FEEDBACK,
    evaluation_path: Path = DEFAULT_EVALUATIONS,
) -> dict[str, Any]:
    with Path(feedback_path).open(encoding="utf-8-sig") as handle:
        feedback = json.load(handle).get("records", [])
    with Path(evaluation_path).open(encoding="utf-8-sig") as handle:
        evaluations = json.load(handle).get("records", [])
    analysis = analyze_history(feedback, evaluations, [])
    return {
        "analysis_type": "offline_adaptive_recommendations_only",
        "metrics": [
            "success_rate", "evaluation_score", "latency", "cost",
            "user_rating", "sample_count",
        ],
        "history_analysis": analysis,
        "recommendations": generate_recommendations(analysis),
        "automatic_routing_change_performed": False,
        "automatic_weight_update": False,
        "real_api_calls_performed": 0,
        "limitations": [
            "Synthetic offline feedback and evaluations are not production evidence.",
            "Recommendations require controlled validation before any manual change."
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feedback", type=Path, default=DEFAULT_FEEDBACK)
    parser.add_argument("--evaluations", type=Path, default=DEFAULT_EVALUATIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    report = build_report(args.feedback, args.evaluations)
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "status": "ok",
        "recommendation_count": len(report["recommendations"]),
        "automatic_weight_update": False,
        "real_api_calls_performed": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
