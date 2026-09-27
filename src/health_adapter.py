"""Append health-monitor fields to candidates without changing strategy inputs."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping


HEALTH_FIELDS = ("health_score", "confidence", "status", "health_version")


class HealthAdapterError(ValueError):
    pass


def health_index(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    channels = snapshot.get("channels")
    if not isinstance(channels, list):
        raise HealthAdapterError("health snapshot channels must be a list")
    indexed: dict[str, dict[str, Any]] = {}
    for row in channels:
        channel_id = str(row.get("channel_id", ""))
        if not channel_id or channel_id in indexed:
            raise HealthAdapterError("health snapshot channel_id must be non-empty and unique")
        indexed[channel_id] = dict(row)
    return indexed


def adapt_candidate(
    candidate: Mapping[str, Any],
    health: Mapping[str, Any] | None,
    *,
    health_version: str,
) -> dict[str, Any]:
    output = deepcopy(dict(candidate))
    if health is None:
        output.update({
            "health_score": None,
            "confidence": None,
            "status": "unknown",
            "health_version": health_version,
        })
        return output
    output.update({
        "health_score": float(health["health_score"]),
        "confidence": float(health["confidence"]),
        "status": str(health["status"]),
        "health_version": health_version,
    })
    return output


def adapt_candidates(
    candidates: Iterable[Mapping[str, Any]],
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    indexed = health_index(snapshot)
    version = str(snapshot.get("health_version", "unknown"))
    return [
        adapt_candidate(row, indexed.get(str(row.get("channel_id", ""))),
                        health_version=version)
        for row in candidates
    ]
