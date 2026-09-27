"""Deterministic offline channel health estimation from configured metrics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from metrics_store import DEFAULT_METRICS_PATH, MetricsStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "config" / "health_policy_v1.json"
DEFAULT_THRESHOLDS_PATH = ROOT / "config" / "channel_health_thresholds.json"
DEFAULT_OUTPUT_PATH = ROOT / "output" / "channel_health_snapshot_v1.json"
WEIGHT_KEYS = ("reliability", "latency", "cost", "confidence")
ESTIMATION_TYPE = "offline health estimation based on configured metrics"


class HealthPolicyError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def validate_policy(policy: Mapping[str, Any]) -> None:
    weights = policy.get("weights", {})
    if set(weights) != set(WEIGHT_KEYS):
        raise HealthPolicyError("health weights must define exactly four components")
    values = [float(weights[key]) for key in WEIGHT_KEYS]
    if any(value < 0 or value > 1 for value in values):
        raise HealthPolicyError("health weights must be in [0,1]")
    if abs(sum(values) - 1.0) > 1e-9:
        raise HealthPolicyError("health weights must sum to 1")
    if float(policy["confidence"]["sample_threshold"]) <= 0:
        raise HealthPolicyError("confidence sample_threshold must be positive")
    normalization = policy["normalization"]
    if (
        float(normalization["latency_reference_ms"]) <= 0
        or float(normalization["cost_reference"]) <= 0
    ):
        raise HealthPolicyError("normalization references must be positive")


def validate_thresholds(thresholds: Mapping[str, Any]) -> None:
    healthy = float(thresholds["healthy"])
    warning = float(thresholds["warning"])
    if not 0 <= warning < healthy <= 1:
        raise HealthPolicyError("thresholds must satisfy 0 <= warning < healthy <= 1")


def calculate_confidence(sample_count: int | float, threshold: int | float) -> float:
    count = float(sample_count)
    limit = float(threshold)
    if count < 0 or limit <= 0:
        raise HealthPolicyError("sample_count must be non-negative and threshold positive")
    return min(count / limit, 1.0)


def classify_status(score: float, thresholds: Mapping[str, Any]) -> str:
    validate_thresholds(thresholds)
    if score >= float(thresholds["healthy"]):
        return "healthy"
    if score >= float(thresholds["warning"]):
        return "warning"
    return "degraded"


def calculate_health_score(
    channel_metrics: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, float]:
    validate_policy(policy)
    metrics = channel_metrics["metrics"]
    reliability = float(metrics["success_rate"])
    latency_reference = float(policy["normalization"]["latency_reference_ms"])
    cost_reference = float(policy["normalization"]["cost_reference"])
    latency = max(0.0, 1.0 - float(metrics["avg_latency_ms"]) / latency_reference)
    cost = max(0.0, 1.0 - float(metrics["avg_cost"]) / cost_reference)
    confidence = calculate_confidence(
        channel_metrics["sample_count"], policy["confidence"]["sample_threshold"]
    )
    components = {
        "reliability_score": min(max(reliability, 0.0), 1.0),
        "latency_score": min(max(latency, 0.0), 1.0),
        "cost_score": min(max(cost, 0.0), 1.0),
        "confidence": confidence,
    }
    weights = policy["weights"]
    score = (
        float(weights["reliability"]) * components["reliability_score"]
        + float(weights["latency"]) * components["latency_score"]
        + float(weights["cost"]) * components["cost_score"]
        + float(weights["confidence"]) * components["confidence"]
    )
    return {**components, "health_score": min(max(score, 0.0), 1.0)}


def _reasons(scores: Mapping[str, float]) -> list[str]:
    reasons: list[str] = []
    low_confidence = scores["confidence"] < 0.1
    if low_confidence:
        reasons.append("very small configured sample; reliability remains uncertain")
    elif scores["reliability_score"] >= 0.95:
        reasons.append("high configured reliability")
    elif scores["reliability_score"] < 0.8:
        reasons.append("low configured reliability")
    if scores["latency_score"] >= 0.7:
        reasons.append("low normalized latency")
    elif scores["latency_score"] < 0.4:
        reasons.append("high normalized latency")
    if scores["confidence"] >= 1.0:
        reasons.append("configured sample threshold reached")
    return reasons or ["mixed configured metric signals"]


def build_health_snapshot(
    store: MetricsStore,
    policy: Mapping[str, Any],
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    validate_policy(policy)
    validate_thresholds(thresholds)
    channels: list[dict[str, Any]] = []
    for row in store.channels():
        scores = calculate_health_score(row, policy)
        channels.append({
            "channel_id": str(row["channel_id"]),
            "health_score": round(scores["health_score"], 6),
            "confidence": round(scores["confidence"], 6),
            "status": classify_status(scores["health_score"], thresholds),
            "reason": _reasons(scores),
            "components": {
                key: round(scores[key], 6)
                for key in ("reliability_score", "latency_score", "cost_score")
            },
            "sample_count": int(row["sample_count"]),
            "metrics_version": store.metrics_version,
        })
    return {
        "health_version": str(policy["health_version"]),
        "metrics_version": store.metrics_version,
        "metrics_updated_at": store.updated_at,
        "estimation_type": ESTIMATION_TYPE,
        "channels": channels,
        "limitations": [
            "Configured offline metrics are not production performance evidence.",
            "Thresholds, normalization references, and weights are policy choices.",
            "The score is deterministic but is not claimed to be scientifically optimal."
        ],
    }


def write_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, default=DEFAULT_METRICS_PATH)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--thresholds", type=Path, default=DEFAULT_THRESHOLDS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        snapshot = build_health_snapshot(
            MetricsStore.load(args.metrics),
            load_json(args.policy),
            load_json(args.thresholds),
        )
        if not args.dry_run:
            write_snapshot(args.output, snapshot)
        print(json.dumps({
            "status": "ok", "channel_count": len(snapshot["channels"]),
            "estimation_type": ESTIMATION_TYPE, "dry_run": args.dry_run,
            "real_api_calls_performed": 0,
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps(
            {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
            ensure_ascii=False,
        ), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
