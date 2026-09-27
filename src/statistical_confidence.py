"""Versioned, deterministic weighted-Wilson confidence calculations."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "config" / "statistical_confidence_policy_v1.json"
CONFIDENCE_VERSION = "weighted_wilson_v1"


class ConfidencePolicyError(ValueError):
    """Stable statistical-policy error."""


def load_confidence_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8-sig"))
    level = float(policy.get("confidence_level", 0))
    if not 0 < level < 1:
        raise ConfidencePolicyError("invalid_confidence_level")
    if policy.get("method") != CONFIDENCE_VERSION:
        raise ConfidencePolicyError("unsupported_confidence_method")
    if float(policy.get("minimum_effective_sample_size", 0)) <= 0:
        raise ConfidencePolicyError("invalid_minimum_effective_sample_size")
    required = policy.get("required_fields")
    if not isinstance(required, list) or not required or len(set(required)) != len(required):
        raise ConfidencePolicyError("invalid_required_fields")
    half_lives = policy.get("freshness_half_life_seconds_by_window")
    if (
        not isinstance(half_lives, dict)
        or set(half_lives) != {"5m", "1h", "24h"}
        or any(float(value) <= 0 for value in half_lives.values())
    ):
        raise ConfidencePolicyError("invalid_freshness_half_lives")
    reliability = policy.get("source_reliability")
    if (
        not isinstance(reliability, dict)
        or not reliability
        or any(not 0 <= float(value) <= 1 for value in reliability.values())
    ):
        raise ConfidencePolicyError("invalid_source_reliability")
    thresholds = policy.get("confidence_width_thresholds")
    if (
        not isinstance(thresholds, dict)
        or not 0 <= float(thresholds.get("high", -1))
        <= float(thresholds.get("medium", -1))
        <= 1
    ):
        raise ConfidencePolicyError("invalid_confidence_width_thresholds")
    if policy.get("unknown_source_action") != "block":
        raise ConfidencePolicyError("invalid_unknown_source_action")
    canonical = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    policy["_policy_sha256"] = hashlib.sha256(canonical).hexdigest()
    return policy


def wilson_interval(
    success_rate: float, effective_sample_size: float, confidence_level: float
) -> tuple[float, float]:
    if not 0 <= success_rate <= 1:
        raise ConfidencePolicyError("invalid_success_rate")
    if effective_sample_size <= 0:
        raise ConfidencePolicyError("invalid_effective_sample_size")
    if not 0 < confidence_level < 1:
        raise ConfidencePolicyError("invalid_confidence_level")
    z = NormalDist().inv_cdf(1 - (1 - confidence_level) / 2)
    z2 = z * z
    denominator = 1 + z2 / effective_sample_size
    center = (success_rate + z2 / (2 * effective_sample_size)) / denominator
    half = (z / denominator) * math.sqrt(
        success_rate * (1 - success_rate) / effective_sample_size
        + z2 / (4 * effective_sample_size * effective_sample_size)
    )
    return max(0.0, center - half), min(1.0, center + half)


def _utc(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        raise ConfidencePolicyError("timezone_required")
    return parsed.astimezone(timezone.utc)


def calculate_weighted_wilson(
    events: Iterable[dict[str, Any]],
    *,
    window_name: str,
    window_end: str | datetime,
    metric_snapshot_id: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or load_confidence_policy()
    rows = list(events)
    required = tuple(str(item) for item in policy["required_fields"])
    half_lives = policy["freshness_half_life_seconds_by_window"]
    if window_name not in half_lives:
        raise ConfidencePolicyError("invalid_metric_window")
    half_life = float(half_lives[window_name])
    end = _utc(window_end)
    source_policy = policy["source_reliability"]
    unknown_sources: set[str] = set()
    valid: list[tuple[float, float, float, float, float]] = []
    valid_outcomes = 0
    successes = 0
    for row in rows:
        outcome = row.get("success")
        if outcome is None:
            continue
        valid_outcomes += 1
        successes += int(bool(outcome))
        source_type = str(row.get("source_type") or "unknown")
        if source_type not in source_policy:
            unknown_sources.add(source_type)
        governed_reliability = float(source_policy.get(source_type, 0.0))
        if row.get("source_reliability") is not None:
            reliability = min(float(row["source_reliability"]), governed_reliability)
        else:
            reliability = governed_reliability
        missing = set(row.get("missing_fields") or ())
        completeness = max(0.0, 1 - len(missing.intersection(required)) / len(required))
        age = max(0.0, (end - _utc(row["observed_at"])).total_seconds())
        freshness = 2 ** (-age / half_life)
        valid.append((float(bool(outcome)), reliability, freshness, completeness, age))
    coverage = valid_outcomes / len(rows) if rows else 0.0
    weights = [
        reliability * freshness * completeness * coverage
        for _, reliability, freshness, completeness, _ in valid
    ]
    weight_sum = sum(weights)
    weight_square_sum = sum(weight * weight for weight in weights)
    weighted_success_sum = sum(
        outcome * weight for (outcome, *_), weight in zip(valid, weights)
    )
    raw_rate = successes / valid_outcomes if valid_outcomes else None
    weighted_rate = weighted_success_sum / weight_sum if weight_sum > 0 else None
    kish = (
        weight_sum * weight_sum / weight_square_sum if weight_square_sum > 0 else 0.0
    )
    effective = min(float(valid_outcomes), weight_sum, kish)
    blocked_unknown = (
        bool(unknown_sources) and policy.get("unknown_source_action") == "block"
    )
    minimum = float(policy["minimum_effective_sample_size"])
    if weighted_rate is None or effective <= 0:
        lower = upper = width = adjusted = None
        label = "insufficient"
        state = "blocked" if blocked_unknown else "insufficient"
    else:
        lower, upper = wilson_interval(
            weighted_rate, effective, float(policy["confidence_level"])
        )
        width = upper - lower
        adjusted = min(float(raw_rate), lower)
        if blocked_unknown:
            label, state = "insufficient", "blocked"
        elif effective < minimum:
            label, state = "insufficient", "insufficient"
        else:
            thresholds = policy["confidence_width_thresholds"]
            label = (
                "high"
                if width <= float(thresholds["high"])
                else "medium"
                if width <= float(thresholds["medium"])
                else "low"
            )
            state = "ready"
    reliability_factor = (
        sum(item[1] for item in valid) / len(valid) if valid else 0.0
    )
    freshness_factor = (
        sum(item[2] for item in valid) / len(valid) if valid else 0.0
    )
    completeness_factor = (
        sum(item[3] for item in valid) / len(valid) if valid else 0.0
    )
    boundary = (
        "no_valid_outcomes"
        if not valid_outcomes
        else "all_failure"
        if successes == 0
        else "all_success"
        if successes == valid_outcomes
        else "mixed"
    )
    fingerprint_payload = {
        "snapshot": metric_snapshot_id,
        "policy": policy["policy_version"],
        "policy_sha256": policy["_policy_sha256"],
        "rows": [
            {
                "id": str(row.get("evidence_id") or ""),
                "observed_at": str(row.get("observed_at") or ""),
                "success": row.get("success"),
                "source_type": str(row.get("source_type") or ""),
                "source_reliability": row.get("source_reliability"),
                "missing_fields": sorted(row.get("missing_fields") or ()),
            }
            for row in sorted(rows, key=lambda item: str(item.get("evidence_id") or ""))
        ],
    }
    input_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    evidence_ids = sorted(
        {str(row.get("evidence_id") or "") for row in rows if row.get("evidence_id")}
    )
    evidence_manifest_sha256 = hashlib.sha256(
        "\n".join(evidence_ids).encode("utf-8")
    ).hexdigest()
    return {
        "confidence_version": CONFIDENCE_VERSION,
        "policy_version": policy["policy_version"],
        "policy_sha256": policy["_policy_sha256"],
        "method": CONFIDENCE_VERSION,
        "metric_snapshot_id": metric_snapshot_id,
        "window_name": window_name,
        "confidence_level": float(policy["confidence_level"]),
        "total_event_count": len(rows),
        "valid_outcome_count": valid_outcomes,
        "success_count": successes,
        "failure_count": valid_outcomes - successes,
        "raw_success_rate": raw_rate,
        "weighted_success_rate": weighted_rate,
        "weight_sum": weight_sum,
        "weight_square_sum": weight_square_sum,
        "kish_effective_sample_size": kish,
        "effective_sample_size": effective,
        "minimum_effective_sample_size": minimum,
        "interval_lower": lower,
        "interval_upper": upper,
        "interval_width": width,
        "adjusted_success_score": adjusted,
        "adjusted_failure_risk": None if adjusted is None else 1 - adjusted,
        "small_sample_adjustment": (
            None if raw_rate is None or adjusted is None else raw_rate - adjusted
        ),
        "outcome_coverage_factor": coverage,
        "source_reliability_factor": reliability_factor,
        "freshness_factor": freshness_factor,
        "completeness_factor": completeness_factor,
        "confidence_label": label,
        "confidence_state": state,
        "boundary_case": boundary,
        "unknown_source_types": sorted(unknown_sources),
        "evidence_id_count": len(evidence_ids),
        "evidence_ids": evidence_ids[:100],
        "evidence_ids_truncated": len(evidence_ids) > 100,
        "evidence_manifest_sha256": evidence_manifest_sha256,
        "input_fingerprint": input_fingerprint,
        "assumptions": list(policy.get("assumptions") or ()),
    }
