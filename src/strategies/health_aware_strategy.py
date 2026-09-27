"""Configurable health-aware ranking; existing strategies remain untouched."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping


COMPONENTS = ("health", "reliability", "latency", "cost")


class HealthAwareStrategyError(ValueError):
    pass


def validate_policy(policy: Mapping[str, Any]) -> None:
    if policy.get("strategy") != "health_aware_v1":
        raise HealthAwareStrategyError("policy strategy must be health_aware_v1")
    weights = policy.get("weights", {})
    if set(weights) != set(COMPONENTS):
        raise HealthAwareStrategyError("weights must define health, reliability, latency, cost")
    values = [float(weights[key]) for key in COMPONENTS]
    if any(value < 0 or value > 1 for value in values):
        raise HealthAwareStrategyError("weights must be in [0,1]")
    if abs(sum(values) - 1.0) > 1e-9:
        raise HealthAwareStrategyError("weights must sum to 1")


def calculate_health_aware_score(
    candidate: Mapping[str, Any], policy: Mapping[str, Any]
) -> float:
    validate_policy(policy)
    required = ("health_score", "reliability", "latency_score", "cost_score")
    missing = [field for field in required if candidate.get(field) is None]
    if missing:
        raise HealthAwareStrategyError(f"missing score components: {','.join(missing)}")
    values = {field: float(candidate[field]) for field in required}
    if any(not 0 <= value <= 1 for value in values.values()):
        raise HealthAwareStrategyError("score components must be in [0,1]")
    weights = policy["weights"]
    return (
        float(weights["health"]) * values["health_score"]
        - float(weights["reliability"]) * values["reliability"]
        - float(weights["latency"]) * values["latency_score"]
        - float(weights["cost"]) * values["cost_score"]
    )


def _channel_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value)


def rank_candidates(
    candidates: Iterable[Mapping[str, Any]], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    validate_policy(policy)
    ranked = []
    for candidate in candidates:
        row = deepcopy(dict(candidate))
        row["health_aware_score"] = calculate_health_aware_score(row, policy)
        ranked.append(row)
    ranked.sort(key=lambda row: (
        -row["health_aware_score"],
        int(row.get("priority", 0)),
        _channel_key(str(row["channel_id"])),
    ))
    for rank, row in enumerate(ranked, 1):
        row["rank"] = rank
    return ranked


def select_candidate(
    candidates: Iterable[Mapping[str, Any]], policy: Mapping[str, Any]
) -> dict[str, Any] | None:
    ranked = rank_candidates(candidates, policy)
    return ranked[0] if ranked else None
