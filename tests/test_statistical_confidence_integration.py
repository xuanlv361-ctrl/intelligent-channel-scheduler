from __future__ import annotations

import sys
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.incremental_metrics_service import IncrementalMetricsService
from incremental_metrics_provider import IncrementalMetricProvider

NOW = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)


def evidence(identifier: str, *, success: bool = True, source="measured_unified_uat"):
    return {
        "evidence_id": identifier,
        "environment_id": "china_uat",
        "requested_model": "deepseek-v4-flash",
        "actual_channel": "19",
        "request_profile_id": "P01",
        "observed_at": (NOW - timedelta(seconds=1)).isoformat(),
        "success": success,
        "http_status": 200 if success else 500,
        "latency_ms": 100,
        "stream": False,
        "currency": "CNY",
        "source_type": source,
    }


def candidate():
    return {
        "candidate_id": "REAL-CHANNEL-19",
        "channel_id": "19",
        "availability_status": "available",
        "success_rate": "0.9",
        "observed_success_rate": "0.9",
        "sample_size": "20",
        "latency_ms": "100",
        "metrics_updated_at": "2026-07-30 04:00:00",
    }


def test_confidence_is_versioned_persisted_and_idempotently_rebuilt(tmp_path):
    service = IncrementalMetricsService(
        tmp_path / "metrics.sqlite3", development_mode=True
    )
    rows = [evidence(str(index), success=index < 4) for index in range(5)]
    service.rebuild(rows, as_of=NOW)
    first = service.confidence.list_snapshots()
    service.rebuild(list(reversed(rows)), as_of=NOW)
    second = service.confidence.list_snapshots()
    assert first["total"] == 3
    assert [item["input_fingerprint"] for item in first["items"]] == [
        item["input_fingerprint"] for item in second["items"]
    ]
    assert first["network_called"] is False
    assert first["items"][0]["policy_version"] == "statistical_confidence_policy_v1"


def test_provider_fail_closed_when_confidence_is_insufficient(tmp_path):
    database = tmp_path / "metrics.sqlite3"
    service = IncrementalMetricsService(database, development_mode=True)
    service.ingest([evidence("only")], as_of=NOW)
    provider = IncrementalMetricProvider(
        database, window="1h", confidence_version="weighted_wilson_v1",
        development_mode=True,
    )
    rows, context = provider.apply(
        [candidate()],
        environment_id="china_uat",
        requested_model="deepseek-v4-flash",
        stream=False,
        request_profile_id="P01",
    )
    assert rows[0]["availability_status"] == "unavailable"
    assert rows[0]["confidence_block_reason"] == (
        "statistical_confidence_insufficient"
    )
    assert context["confidence_version"] == "weighted_wilson_v1"
    assert context["confidence_snapshot_ids"]


def test_provider_exposes_ready_interval_and_provenance(tmp_path):
    database = tmp_path / "metrics.sqlite3"
    service = IncrementalMetricsService(database, development_mode=True)
    service.ingest([evidence(str(index)) for index in range(20)], as_of=NOW)
    provider = IncrementalMetricProvider(
        database, window="1h", confidence_version="weighted_wilson_v1",
        development_mode=True,
    )
    rows, _ = provider.apply(
        [candidate()],
        environment_id="china_uat",
        requested_model="deepseek-v4-flash",
        stream=False,
        request_profile_id="P01",
    )
    result = rows[0]
    assert result["availability_status"] == "available"
    assert result["statistical_confidence_state"] == "ready"
    assert float(result["confidence_interval_lower"]) < 1
    assert result["confidence_policy_version"] == "statistical_confidence_policy_v1"


def test_explicit_zero_reliability_is_not_replaced(tmp_path):
    service = IncrementalMetricsService(
        tmp_path / "metrics.sqlite3", development_mode=True
    )
    row = evidence("zero")
    row["source_reliability"] = 0.0
    normalized = service.normalize(row)
    assert normalized.source_reliability == 0.0


def test_input_reliability_is_capped_by_versioned_policy(tmp_path):
    service = IncrementalMetricsService(
        tmp_path / "metrics.sqlite3", development_mode=True
    )
    row = evidence("fixture", source="integration_test_fixture")
    row["source_reliability"] = 1.0
    assert service.normalize(row).source_reliability == 0.5


def test_cli_explicitly_wires_versioned_confidence_provider(tmp_path):
    database = tmp_path / "metrics.sqlite3"
    service = IncrementalMetricsService(database, development_mode=True)
    service.ingest([evidence(str(index)) for index in range(20)], as_of=NOW)
    command = [
        sys.executable,
        str(ROOT / "src" / "run_scheduler.py"),
        "--request-id", "CONF-CLI",
        "--model", "deepseek-v4-flash",
        "--stream", "false",
        "--input-tokens", "10",
        "--output-tokens", "10",
        "--currency", "CNY",
        "--strategy", "confidence_aware_v3",
        "--mode", "real_shadow",
        "--dry-run",
        "--metrics-database", str(database),
        "--environment-id", "china_uat",
        "--request-profile-id", "P01",
    ]
    completed = subprocess.run(
        command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "ICS_EXPLICIT_LOCAL_DEVELOPMENT": "1"},
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["metric_context"]["confidence_version"] == "weighted_wilson_v1"
    assert payload["candidate_ranking"][0][
        "statistical_confidence_version"
    ] == "weighted_wilson_v1"
    assert payload["candidate_ranking"][0][
        "confidence_adjusted_failure_risk"
    ] is not None
    assert payload["network_called"] is False
