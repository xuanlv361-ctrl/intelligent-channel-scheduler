from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.incremental_metrics_service import (
    AGGREGATION_VERSION,
    IncrementalMetricsService,
    MetricsError,
)

NOW = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)


def event(
    evidence_id: str,
    *,
    seconds_ago: int = 10,
    status: int = 200,
    stream: bool = False,
    currency: str | None = "CNY",
    estimated_cost: float | None = 0.01,
    actual_cost: float | None = 0.012,
    **overrides,
):
    value = {
        "evidence_id": evidence_id,
        "environment_id": "china_uat",
        "requested_model": "model-a",
        "actual_model": "model-a",
        "actual_channel": "channel-a",
        "request_profile_id": "P01",
        "observed_at": (NOW - timedelta(seconds=seconds_ago)).isoformat(),
        "http_status": status,
        "latency_ms": 100 + seconds_ago,
        "ttft_ms": 25 if stream else None,
        "tpot_ms": 8 if stream else None,
        "stream": stream,
        "sse_complete": True if stream else None,
        "timeout": False,
        "estimated_cost": estimated_cost,
        "actual_cost": actual_cost,
        "currency": currency,
        "fallback": False,
        "schedulable": True,
        "source_type": "measured_unified_uat",
    }
    value.update(overrides)
    return value


def service(tmp_path, **kwargs):
    return IncrementalMetricsService(
        tmp_path / "metrics.sqlite3", development_mode=True, **kwargs
    )


def test_ingestion_is_idempotent_and_persists_watermark(tmp_path):
    metrics = service(tmp_path)
    first = metrics.ingest([event("e-1")], as_of=NOW)
    second = metrics.ingest([event("e-1")], as_of=NOW)

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["duplicates"] == 1
    result = metrics.list_snapshots(window="5m")
    assert result["aggregation_version"] == AGGREGATION_VERSION
    assert result["event_count"] == 1
    assert result["items"][0]["sample_count"] == 1


def test_same_evidence_id_with_changed_payload_is_rejected_atomically(tmp_path):
    metrics = service(tmp_path)
    metrics.ingest([event("e-1")], as_of=NOW)
    with pytest.raises(MetricsError, match="evidence_id_payload_conflict"):
        metrics.ingest([event("e-1", status=500)], as_of=NOW)
    assert metrics.list_snapshots(window="5m")["event_count"] == 1


def test_late_arrival_is_counted_and_recomputes_windows(tmp_path):
    metrics = service(tmp_path)
    metrics.ingest([event("new", seconds_ago=5)], as_of=NOW)
    result = metrics.ingest([event("late", seconds_ago=120)], as_of=NOW)
    assert result["late_arrivals"] == 1
    assert metrics.list_snapshots(window="5m")["items"][0]["sample_count"] == 2


def test_three_windows_and_percentile_metrics_are_materialized(tmp_path):
    metrics = service(tmp_path, minimum_healthy_samples=2)
    metrics.ingest(
        [
            event("e-1", seconds_ago=60, latency_ms=100),
            event("e-2", seconds_ago=240, latency_ms=200, status=429),
            event("e-3", seconds_ago=1800, latency_ms=300, status=503),
            event("e-4", seconds_ago=7200, latency_ms=400, timeout=True),
        ],
        as_of=NOW,
    )

    five = metrics.list_snapshots(window="5m")["items"][0]
    hour = metrics.list_snapshots(window="1h")["items"][0]
    day = metrics.list_snapshots(window="24h")["items"][0]
    assert (five["sample_count"], hour["sample_count"], day["sample_count"]) == (
        2,
        3,
        4,
    )
    assert five["metrics"]["latency_p50_ms"] == 150
    assert five["metrics"]["latency_p95_ms"] == 195
    assert five["metrics"]["http_429_rate"] == 0.5
    assert five["metrics"]["source_type_counts"] == {"measured_unified_uat": 2}
    assert five["metrics"]["contains_mock"] is False
    assert hour["metrics"]["http_5xx_rate"] == pytest.approx(1 / 3)
    assert day["metrics"]["timeout_rate"] == 0.25
    assert five["data_state"] == "healthy_evidence"


def test_non_empty_dynamic_metrics_values_and_window_expiry_are_exact(tmp_path):
    """Isolated tmp DB proves the API-facing snapshot math without polluting real data."""
    metrics = service(tmp_path, minimum_healthy_samples=1)
    metrics.ingest([
        event("accept-1", seconds_ago=60, latency_ms=100, actual_cost=0.10,
              fallback=False, status=200),
        event("accept-2", seconds_ago=120, latency_ms=200, actual_cost=0.20,
              fallback=True, status=500),
    ], as_of=NOW)
    snapshot = metrics.list_snapshots(window="5m")["items"][0]
    assert snapshot["sample_count"] == 2
    assert snapshot["metrics"]["success_rate"] == 0.5
    assert snapshot["metrics"]["latency_p95_ms"] == 195
    assert snapshot["metrics"]["average_actual_cost"] == pytest.approx(0.15)
    assert snapshot["metrics"]["fallback_rate"] == 0.5

    metrics.ingest([], as_of=NOW + timedelta(minutes=6))
    expired = metrics.list_snapshots(window="5m")
    assert expired["items"] == []
    assert expired["event_count"] == 2


def test_streaming_and_profile_are_separate_dimensions(tmp_path):
    metrics = service(tmp_path)
    metrics.ingest(
        [
            event("nonstream", stream=False),
            event("stream", stream=True, request_profile_id="P04"),
        ],
        as_of=NOW,
    )
    items = metrics.list_snapshots(window="5m")["items"]
    assert {(item["stream"], item["request_profile_id"]) for item in items} == {
        (False, "P01"),
        (True, "P04"),
    }
    streaming = next(item for item in items if item["stream"])
    assert streaming["metrics"]["ttft_p50_ms"] == 25
    assert streaming["metrics"]["incomplete_sse_rate"] == 0


def test_currency_isolated_and_unconfirmed_cost_is_not_aggregated(tmp_path):
    metrics = service(tmp_path)
    metrics.ingest(
        [
            event("cny", currency="CNY", actual_cost=1),
            event("usd", currency="USD", actual_cost=2),
            event("unknown", currency=None, actual_cost=99),
        ],
        as_of=NOW,
    )
    items = metrics.list_snapshots(window="5m")["items"]
    by_currency = {item["currency"]: item for item in items}
    assert set(by_currency) == {None, "CNY", "USD"}
    assert by_currency["CNY"]["metrics"]["average_actual_cost"] == 1
    assert by_currency["USD"]["metrics"]["average_actual_cost"] == 2
    assert by_currency[None]["metrics"]["average_actual_cost"] is None


def test_timezone_is_required_and_converted_to_utc(tmp_path):
    metrics = service(tmp_path)
    local = event("local")
    local["observed_at"] = "2026-07-30T11:59:50+08:00"
    metrics.ingest([local], as_of=NOW)
    assert metrics.list_snapshots(window="5m")["source_watermark"] == (
        "2026-07-30T03:59:50+00:00"
    )
    legacy = event("naive")
    legacy["observed_at"] = "2026-07-30T03:59:50"
    metrics.ingest([legacy], as_of=NOW)
    assert metrics.list_snapshots(window="5m")["source_watermark"] == (
        "2026-07-30T03:59:50+00:00"
    )


def test_data_states_do_not_invent_health(tmp_path):
    metrics = service(tmp_path, minimum_healthy_samples=5, freshness_seconds=30)
    assert metrics.list_snapshots()["status"] == "unknown"
    metrics.ingest([event("few", seconds_ago=10)], as_of=NOW)
    assert metrics.list_snapshots(window="5m")["items"][0]["data_state"] == (
        "insufficient_data"
    )
    metrics.rebuild([event("stale", seconds_ago=120)], as_of=NOW)
    stale = metrics.list_snapshots(window="5m")["items"][0]
    assert stale["data_state"] == "stale"
    assert stale["freshness_status"] == "stale"
    metrics.rebuild(
        [event("blocked", seconds_ago=10, schedulable=False)], as_of=NOW
    )
    assert metrics.list_snapshots(window="5m")["items"][0]["data_state"] == "blocked"


def test_rebuild_is_deterministic_and_records_audit(tmp_path):
    metrics = service(tmp_path)
    source = [event("e-2", seconds_ago=20), event("e-1", seconds_ago=10)]
    metrics.rebuild(source, as_of=NOW)
    first = metrics.list_snapshots()
    metrics.rebuild(list(reversed(source)), as_of=NOW)
    second = metrics.list_snapshots()

    assert [
        (item["window_name"], item["snapshot_id"], item["metrics"])
        for item in first["items"]
    ] == [
        (item["window_name"], item["snapshot_id"], item["metrics"])
        for item in second["items"]
    ]
    assert any(
        item["event_type"] == "metric_store_rebuilt"
        for item in metrics.audit_events()
    )


def test_pagination_and_filters_are_bounded(tmp_path):
    metrics = service(tmp_path)
    metrics.ingest(
        [
            event("a", actual_channel="channel-a"),
            event("b", actual_channel="channel-b"),
        ],
        as_of=NOW,
    )
    first = metrics.list_snapshots(window="5m", limit=1)
    assert first["total"] == 2
    assert first["has_more"] is True
    second = metrics.list_snapshots(window="5m", limit=1, offset=1)
    assert second["has_more"] is False
    assert metrics.list_snapshots(channel="channel-b")["total"] == 3


def test_get_path_reads_snapshots_not_evidence_rows(tmp_path):
    metrics = service(tmp_path)
    metrics.ingest([event("e-1")], as_of=NOW)
    with metrics.connect() as db:
        db.execute("DROP TABLE metric_evidence_events")
    # A read remains available from the materialized snapshot even if its source
    # projection is temporarily unavailable during recovery.
    assert metrics.list_snapshots(window="5m")["items"][0]["sample_count"] == 1


def test_no_network_or_secret_fields_are_persisted(tmp_path):
    metrics = service(tmp_path)
    raw = event("safe")
    raw["authorization"] = "synthetic-authorization-value"
    raw["api_key"] = "secret-key"
    result = metrics.ingest([raw], as_of=NOW)
    assert result["network_called"] is False
    database_bytes = (tmp_path / "metrics.sqlite3").read_bytes()
    assert b"synthetic-authorization-value" not in database_bytes
    assert b"secret-key" not in database_bytes
