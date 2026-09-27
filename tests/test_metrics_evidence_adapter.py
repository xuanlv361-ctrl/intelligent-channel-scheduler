from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.incremental_metrics_service import IncrementalMetricsService
from backend.metrics_evidence_adapter import (
    MetricsEvidenceAdapter,
    MetricsSourceError,
    PROTECTED_V3_SHA256,
)

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = ROOT / "output" / "unified_uat_execution_v3.jsonl"


def source_schema(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE import_batches(
              batch_id TEXT PRIMARY KEY,sha256 TEXT,source_type TEXT,
              created_at TEXT,payload TEXT,tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL);
            CREATE TABLE uat_executions(
              execution_id TEXT PRIMARY KEY,created_at TEXT,
              environment_id TEXT,estimated_cost_cny REAL,payload TEXT,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            CREATE TABLE uat_response_evidence(
              execution_id TEXT PRIMARY KEY,payload TEXT,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            CREATE TABLE realtime_log_records(
              record_id TEXT PRIMARY KEY,created_at TEXT,normalized_json TEXT,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            """
        )


def setup(tmp_path: Path, protected: Path = PROTECTED):
    path = tmp_path / "metrics.sqlite3"
    source_schema(path)
    metrics = IncrementalMetricsService(path, development_mode=True)
    adapter = MetricsEvidenceAdapter(path, metrics, protected)
    return path, metrics, adapter


def protected_as_of() -> datetime:
    last = max(
        datetime.fromisoformat(json.loads(line)["completed_at"].replace("Z", "+00:00"))
        for line in PROTECTED.read_text(encoding="utf-8").splitlines()
    )
    return last.astimezone(timezone.utc) + timedelta(minutes=1)


def test_protected_v3_is_verified_projected_once_and_never_modified(tmp_path):
    before = PROTECTED.read_bytes()
    _, metrics, adapter = setup(tmp_path)
    first = adapter.synchronize(as_of=protected_as_of())
    second = adapter.synchronize(as_of=protected_as_of())

    assert first["source_counts"]["protected_v3"]["accepted"] == 60
    assert first["protected_evidence_modified"] is False
    assert first["real_external_access_count"] == 0
    assert second["source_counts"]["protected_v3"]["observed"] == 0
    assert metrics.list_snapshots(window="5m")["event_count"] == 60
    assert PROTECTED.read_bytes() == before


def test_cursor_loss_replays_idempotently_without_inflating_metrics(tmp_path):
    path, metrics, adapter = setup(tmp_path)
    adapter.synchronize(as_of=protected_as_of())
    with sqlite3.connect(path) as db:
        db.execute(
            "DELETE FROM metric_source_cursors WHERE source_name='protected_v3'"
        )
    replay = adapter.synchronize(as_of=protected_as_of())
    assert replay["duplicates"] == 60
    assert metrics.list_snapshots()["event_count"] == 60


def test_restart_recovers_persisted_cursors_and_snapshots(tmp_path):
    path, metrics, adapter = setup(tmp_path)
    adapter.synchronize(as_of=protected_as_of())
    restarted_metrics = IncrementalMetricsService(path, development_mode=True)
    restarted_adapter = MetricsEvidenceAdapter(path, restarted_metrics, PROTECTED)
    result = restarted_adapter.synchronize(as_of=protected_as_of())
    assert result["source_counts"]["protected_v3"]["observed"] == 0
    assert restarted_metrics.list_snapshots()["event_count"] == 60


def test_failed_ingestion_does_not_advance_source_cursors(tmp_path, monkeypatch):
    _, _, adapter = setup(tmp_path)
    monkeypatch.setattr(
        adapter.metrics,
        "ingest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic")),
    )
    with pytest.raises(RuntimeError, match="synthetic"):
        adapter.synchronize_safely(as_of=protected_as_of())
    assert adapter._cursor("protected_v3") == {}
    assert adapter.status()["last_error_code"] == "metric_refresh_failed"


def test_protected_integrity_failure_is_structured_and_persisted(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"execution_id":"changed"}\n', encoding="utf-8")
    _, _, adapter = setup(tmp_path, bad)
    with pytest.raises(MetricsSourceError, match="protected_v3_integrity_mismatch"):
        adapter.synchronize_safely()
    status = adapter.status()
    assert status["last_status"] == "failed"
    assert status["last_error_code"] == "protected_v3_integrity_mismatch"


def test_confirmed_import_and_uat_evidence_are_whitelisted(tmp_path):
    path, metrics, adapter = setup(tmp_path)
    observed = protected_as_of() - timedelta(seconds=30)
    batch = {
        "source_type": "measured_uat",
        "environment_id": "china_uat",
        "normalized_rows": [
            {
                "_row_number": 1,
                "_classification": "valid",
                "request_id": "import-request",
                "requested_model": "model-import",
                "channel_id": "channel-import",
                "timestamp": observed.isoformat(),
                "http_status": 200,
                "latency_ms": 55,
                "authorization": "synthetic-auth-value",
            }
        ],
    }
    execution = {
        "requested_model": "model-uat",
        "prompt_profile_id": "P01",
        "request_completed_at": observed.isoformat(),
        "stream": False,
        "source_type": "measured_uat",
    }
    response = {"actual_model": "model-uat", "http_status": 200, "elapsed_ms": 75}
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO import_batches VALUES(?,?,?,?,?,?,?)",
            ("batch-1", "digest", "measured_uat", observed.isoformat(),
             json.dumps(batch), "tenant_local_dev_v1", "workspace_local_dev_v1"),
        )
        db.execute(
            "INSERT INTO uat_executions VALUES(?,?,?,?,?,?,?)",
            ("execution-1", observed.isoformat(), "china_uat", 0.01,
             json.dumps(execution), "tenant_local_dev_v1",
             "workspace_local_dev_v1"),
        )
        db.execute(
            "INSERT INTO uat_response_evidence VALUES(?,?,?,?)",
            ("execution-1", json.dumps(response), "tenant_local_dev_v1",
             "workspace_local_dev_v1"),
        )
    result = adapter.synchronize(as_of=protected_as_of())
    assert result["source_counts"]["confirmed_imports"]["accepted"] == 1
    assert result["source_counts"]["uat_executions"]["accepted"] == 1
    with metrics.connect() as db:
        payload = b"".join(
            str(tuple(row)).encode()
            for row in db.execute("SELECT * FROM metric_evidence_events")
        )
    assert b"synthetic-auth-value" not in payload


def test_missing_realtime_timestamp_is_rejected_not_guessed(tmp_path):
    path, metrics, adapter = setup(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO realtime_log_records VALUES(?,?,?,?,?)",
            (
                "record-1",
                protected_as_of().isoformat(),
                json.dumps(
                    {
                        "environment_id": "china_uat",
                        "actual_model": "model-a",
                        "actual_channel_id": "channel-a",
                        "source_type": "measured_uat_browser_collector",
                    }
                ),
                "tenant_local_dev_v1",
                "workspace_local_dev_v1",
            ),
        )
    result = adapter.synchronize(as_of=protected_as_of())
    assert result["source_counts"]["realtime_logs"]["rejected"] == 1
    assert result["rejection_counts"]["missing_evidence_timestamp"] == 1
    with metrics.connect() as db:
        assert (
            db.execute(
                """SELECT COUNT(*) FROM metric_evidence_events
                   WHERE evidence_id='realtime:record-1'"""
            ).fetchone()[0]
            == 0
        )


def test_rebuild_is_safe_and_protected_baseline_constant_is_exact(tmp_path):
    _, metrics, adapter = setup(tmp_path)
    result = adapter.synchronize(rebuild=True, as_of=protected_as_of())
    assert result["rebuild"] is True
    assert result["event_count"] == 60
    assert any(
        item["event_type"] == "metric_store_rebuilt"
        for item in metrics.audit_events()
    )
    assert PROTECTED_V3_SHA256 == (
        "E61D6CDD596BB8963F4BE9A1C42724D31A2A54E33765CEB5EF60AC372EACB627"
    )
