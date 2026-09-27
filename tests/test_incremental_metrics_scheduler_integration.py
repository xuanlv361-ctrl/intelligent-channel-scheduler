from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.incremental_metrics_service import IncrementalMetricsService
from decision_logger import DecisionLogger
from incremental_metrics_provider import IncrementalMetricProvider
from scheduler import Scheduler

NOW = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)
BASE = {
    "request_id": "METRIC-R1",
    "requested_model": "deepseek-v4-flash",
    "stream": False,
    "input_tokens": 100,
    "output_tokens": 50,
    "currency": "CNY",
    "strategy": "confidence_aware_v2",
    "mode": "real_shadow",
}


def evidence(evidence_id: str, channel: str, latency: int, success: bool):
    return {
        "evidence_id": evidence_id,
        "environment_id": "china_uat",
        "requested_model": "deepseek-v4-flash",
        "actual_channel": channel,
        "request_profile_id": "P01",
        "observed_at": (NOW - timedelta(seconds=30)).isoformat(),
        "success": success,
        "http_status": 200 if success else 500,
        "latency_ms": latency,
        "stream": False,
        "currency": "CNY",
        "source_type": "measured_unified_uat",
    }


def configured(tmp_path):
    database = tmp_path / "metrics.sqlite3"
    metrics = IncrementalMetricsService(database, development_mode=True)
    metrics.ingest(
        [
            *[evidence(f"19-{index}", "19", 100, True) for index in range(5)],
            *[
                evidence(f"27-{index}", "27", 900, index < 3)
                for index in range(5)
            ],
        ],
        as_of=NOW,
    )
    provider = IncrementalMetricProvider(
        database, window="1h", development_mode=True
    )
    scheduler = Scheduler(
        logger=DecisionLogger(tmp_path / "runtime.jsonl"),
        metric_provider=provider,
        runtime_log_enabled=False,
    )
    return database, scheduler


def test_scheduler_uses_snapshot_metrics_and_exposes_snapshot_ids(tmp_path):
    _, scheduler = configured(tmp_path)
    result = scheduler.route(
        {
            **BASE,
            "metadata": {
                "environment_id": "china_uat",
                "request_profile_id": "P01",
            },
        }
    )
    assert result["recommended_candidate"] == "REAL-CHANNEL-19"
    assert result["metric_context"]["provider"] == "incremental_metric_snapshot"
    assert result["metric_context"]["applied_count"] == 2
    assert len(result["metric_context"]["snapshot_ids"]) == 2
    assert result["metric_context"]["raw_evidence_scanned"] is False
    primary = result["candidate_ranking"][0]
    assert primary["dynamic_metrics_state"] == "healthy_evidence"
    assert primary["dynamic_metric_snapshot_id"].startswith("MS-")


def test_enabled_provider_blocks_missing_scope_instead_of_fixed_fallback(tmp_path):
    _, scheduler = configured(tmp_path)
    result = scheduler.route(BASE)
    assert result["recommended_candidate"] is None
    assert result["eligible_count"] == 0
    assert result["metric_context"]["block_reason"] == "dynamic_metric_scope_required"
    assert all(
        item["dynamic_metrics_block_reason"] == "dynamic_metric_scope_required"
        for item in result["candidate_ranking"]
    )


def test_provider_restart_reads_materialized_snapshots_only(tmp_path):
    database, _ = configured(tmp_path)
    provider = IncrementalMetricProvider(database, development_mode=True)
    scheduler = Scheduler(
        logger=DecisionLogger(tmp_path / "restart.jsonl"),
        metric_provider=provider,
        runtime_log_enabled=False,
    )
    result = scheduler.route(
        {
            **BASE,
            "metadata": {
                "environment_id": "china_uat",
                "request_profile_id": "P01",
            },
        }
    )
    assert result["metric_context"]["applied_count"] == 2
    assert result["network_called"] is False
