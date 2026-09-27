import sys
from datetime import datetime
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from channel_observation import (  # noqa: E402
    ChannelObservation,
    ObservationValidationError,
)
from health_window import HealthWindow, HealthWindowError  # noqa: E402


def observation(index, *, success=True, latency=100, channel="A"):
    return ChannelObservation.from_dict({
        "channel_id": channel,
        "timestamp": f"2026-07-24T10:00:{index:02d}+08:00",
        "success": success,
        "latency_ms": latency,
        "cost": 0.002,
        "error_type": None if success else "timeout",
    })


def test_window_size_respected_and_oldest_removed():
    window = HealthWindow(window_size=3)
    for index in range(4):
        window.append(observation(index))
    assert len(window) == 3
    assert [row.timestamp for row in window.observations()] == [
        "2026-07-24T10:00:01+08:00",
        "2026-07-24T10:00:02+08:00",
        "2026-07-24T10:00:03+08:00",
    ]


def test_expired_observations_removed_against_explicit_reference_time():
    window = HealthWindow(window_size=10, max_age_seconds=2)
    for index in range(4):
        window.append(observation(index))
    assert len(window) == 3
    removed = window.remove_expired(
        datetime.fromisoformat("2026-07-24T10:00:05+08:00")
    )
    assert removed == 2
    assert [row.timestamp for row in window.observations()] == [
        "2026-07-24T10:00:03+08:00"
    ]


def test_window_metrics_are_correct():
    window = HealthWindow(window_size=4)
    window.extend([
        observation(0, success=True, latency=100),
        observation(1, success=True, latency=200),
        observation(2, success=False, latency=400),
        observation(3, success=True, latency=300),
    ])
    assert window.metrics() == {
        "sample_count": 4,
        "success_rate": 0.75,
        "avg_latency_ms": 250.0,
        "p95_latency_ms": 400.0,
        "avg_cost": 0.002,
    }


def test_window_rejects_channel_mix_and_out_of_order():
    window = HealthWindow(window_size=3)
    window.append(observation(1))
    with pytest.raises(HealthWindowError, match="one channel"):
        window.append(observation(2, channel="B"))
    with pytest.raises(HealthWindowError, match="non-decreasing"):
        window.append(observation(0))


def test_observation_schema_validation_and_round_trip():
    value = observation(0).to_dict()
    assert ChannelObservation.from_dict(value).to_dict() == value
    invalid = dict(value)
    invalid.pop("cost")
    with pytest.raises(ObservationValidationError, match="missing"):
        ChannelObservation.from_dict(invalid)
    with pytest.raises(ObservationValidationError, match="successful"):
        ChannelObservation.from_dict({**value, "error_type": "timeout"})
