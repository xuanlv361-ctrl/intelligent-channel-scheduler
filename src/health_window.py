"""Per-channel sliding observation window with deterministic aggregation."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from math import ceil
from typing import Iterable

from channel_observation import ChannelObservation


class HealthWindowError(ValueError):
    pass


class HealthWindow:
    def __init__(
        self, *, window_size: int, max_age_seconds: int | float | None = None
    ):
        if isinstance(window_size, bool) or int(window_size) <= 0:
            raise HealthWindowError("window_size must be a positive integer")
        if max_age_seconds is not None and float(max_age_seconds) <= 0:
            raise HealthWindowError("max_age_seconds must be positive or null")
        self.window_size = int(window_size)
        self.max_age_seconds = (
            float(max_age_seconds) if max_age_seconds is not None else None
        )
        self._items: deque[ChannelObservation] = deque()
        self._channel_id: str | None = None

    def append(self, observation: ChannelObservation) -> None:
        if self._channel_id is None:
            self._channel_id = observation.channel_id
        elif observation.channel_id != self._channel_id:
            raise HealthWindowError("one HealthWindow may contain only one channel")
        if (
            self._items
            and observation.parsed_timestamp < self._items[-1].parsed_timestamp
        ):
            raise HealthWindowError("observations must use non-decreasing timestamps")
        self._items.append(observation)
        while len(self._items) > self.window_size:
            self._items.popleft()
        self.remove_expired(observation.parsed_timestamp)

    def remove_expired(self, reference_time: datetime) -> int:
        if self.max_age_seconds is None:
            return 0
        cutoff = reference_time - timedelta(seconds=self.max_age_seconds)
        removed = 0
        while self._items and self._items[0].parsed_timestamp < cutoff:
            self._items.popleft()
            removed += 1
        return removed

    def observations(self) -> tuple[ChannelObservation, ...]:
        return tuple(self._items)

    def metrics(self) -> dict[str, float | int]:
        if not self._items:
            return {
                "sample_count": 0, "success_rate": 0.0,
                "avg_latency_ms": 0.0, "p95_latency_ms": 0.0, "avg_cost": 0.0,
            }
        count = len(self._items)
        latencies = sorted(float(row.latency_ms) for row in self._items)
        p95_index = max(ceil(0.95 * count) - 1, 0)
        return {
            "sample_count": count,
            "success_rate": sum(row.success for row in self._items) / count,
            "avg_latency_ms": sum(latencies) / count,
            "p95_latency_ms": latencies[p95_index],
            "avg_cost": sum(float(row.cost) for row in self._items) / count,
        }

    def extend(self, observations: Iterable[ChannelObservation]) -> None:
        for observation in observations:
            self.append(observation)

    def __len__(self) -> int:
        return len(self._items)
