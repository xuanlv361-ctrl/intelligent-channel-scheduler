"""Adapt measured real-channel catalog rows for offline shadow evaluation.

``latency_ms`` intentionally maps from ``latency_mean_ms``. The resulting
``success_rate`` is only an engine compatibility alias for the current UAT
sample's ``observed_success_rate``; it is not a long-term success estimate.
"""

from __future__ import annotations

from typing import Any, Mapping


CORE_FIELDS = {
    "candidate_id", "channel_id", "channel_name", "requested_model", "actual_model",
    "latency_mean_ms", "latency_p50_ms", "latency_min_ms", "latency_max_ms",
    "observed_success_rate", "measurement_count", "input_price_per_1m",
    "output_price_per_1m", "currency", "confidence_level", "supports_text",
    "supports_non_stream", "supports_stream", "shadow_eligible", "routing_eligible",
    "routing_block_reason", "source_type", "is_mock", "data_source",
    "metrics_observed_to",
}


class ShadowAdapterError(ValueError):
    """A real catalog row cannot safely enter shadow evaluation."""


def adapt_real_channel(row: Mapping[str, Any], *, compatibility_priority: int = 100) -> dict[str, str]:
    missing = sorted(field for field in CORE_FIELDS if field not in row or row[field] == "")
    if missing:
        raise ShadowAdapterError(f"missing required real-channel fields: {', '.join(missing)}")
    if row["source_type"] != "measured":
        raise ShadowAdapterError("source_type must remain measured")
    if row["is_mock"] != "FALSE":
        raise ShadowAdapterError("is_mock must remain FALSE")
    if row["actual_model"] != "pending_confirmation":
        raise ShadowAdapterError("actual_model must remain pending_confirmation")
    if row["shadow_eligible"] not in {"TRUE", "FALSE"} or row["routing_eligible"] not in {"TRUE", "FALSE"}:
        raise ShadowAdapterError("eligibility flags must be TRUE or FALSE")

    return {
        "candidate_id": str(row["candidate_id"]),
        "channel_id": str(row["channel_id"]),
        "channel_name": str(row["channel_name"]),
        "requested_model": str(row["requested_model"]),
        "actual_model": str(row["actual_model"]),
        "latency_ms": str(row["latency_mean_ms"]),
        "success_rate": str(row["observed_success_rate"]),
        "observed_success_rate": str(row["observed_success_rate"]),
        "sample_size": str(row["measurement_count"]),
        "input_price_per_1m": str(row["input_price_per_1m"]),
        "output_price_per_1m": str(row["output_price_per_1m"]),
        "currency": str(row["currency"]),
        "confidence_level": str(row["confidence_level"]),
        "supports_text": str(row["supports_text"]),
        "supports_non_stream": str(row["supports_non_stream"]),
        "supports_stream": str(row["supports_stream"]),
        "availability_status": "available",
        "metrics_updated_at": str(row["metrics_observed_to"]),
        "priority": str(compatibility_priority),
        "is_mock": str(row["is_mock"]),
        "data_source": str(row["data_source"]),
        "latency_mean_ms": str(row["latency_mean_ms"]),
        "latency_p50_ms": str(row["latency_p50_ms"]),
        "latency_min_ms": str(row["latency_min_ms"]),
        "latency_max_ms": str(row["latency_max_ms"]),
        "shadow_eligible": str(row["shadow_eligible"]),
        "routing_eligible": str(row["routing_eligible"]),
        "routing_block_reason": str(row["routing_block_reason"]),
        "source_type": str(row["source_type"]),
    }


def adapt_shadow_catalog(rows: list[Mapping[str, Any]], *, compatibility_priority: int = 100) -> list[dict[str, str]]:
    """Admit by shadow_eligible only; routing_eligible is preserved, not enforced."""
    return [
        adapt_real_channel(row, compatibility_priority=compatibility_priority)
        for row in rows
        if row.get("shadow_eligible") == "TRUE"
    ]
