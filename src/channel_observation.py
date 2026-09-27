"""Validated, serializable model for one completed offline observation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Mapping


REQUIRED_FIELDS = (
    "channel_id", "timestamp", "success", "latency_ms", "cost", "error_type"
)


class ObservationValidationError(ValueError):
    pass


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ObservationValidationError("timestamp must be a non-empty ISO-8601 string")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ObservationValidationError("timestamp must be valid ISO-8601") from exc


@dataclass(frozen=True, slots=True)
class ChannelObservation:
    channel_id: str
    timestamp: str
    success: bool
    latency_ms: float
    cost: float
    error_type: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.channel_id, str) or not self.channel_id.strip():
            raise ObservationValidationError("channel_id must be a non-empty string")
        parse_timestamp(self.timestamp)
        if type(self.success) is not bool:
            raise ObservationValidationError("success must be boolean")
        for field, value in (("latency_ms", self.latency_ms), ("cost", self.cost)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ObservationValidationError(f"{field} must be numeric")
            if not isfinite(float(value)) or float(value) < 0:
                raise ObservationValidationError(f"{field} must be finite and non-negative")
        if self.error_type is not None and not isinstance(self.error_type, str):
            raise ObservationValidationError("error_type must be string or null")
        if self.success and self.error_type not in (None, ""):
            raise ObservationValidationError(
                "successful observation cannot contain an error_type"
            )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChannelObservation":
        missing = [field for field in REQUIRED_FIELDS if field not in value]
        if missing:
            raise ObservationValidationError(
                f"missing observation fields: {','.join(missing)}"
            )
        extra = set(value) - set(REQUIRED_FIELDS)
        if extra:
            raise ObservationValidationError(
                f"unknown observation fields: {','.join(sorted(extra))}"
            )
        return cls(
            channel_id=value["channel_id"],
            timestamp=value["timestamp"],
            success=value["success"],
            latency_ms=value["latency_ms"],
            cost=value["cost"],
            error_type=value["error_type"],
        )

    @property
    def parsed_timestamp(self) -> datetime:
        return parse_timestamp(self.timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "timestamp": self.timestamp,
            "success": self.success,
            "latency_ms": float(self.latency_ms),
            "cost": float(self.cost),
            "error_type": self.error_type,
        }
