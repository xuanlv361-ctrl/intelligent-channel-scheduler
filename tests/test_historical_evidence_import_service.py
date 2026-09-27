from datetime import datetime, timedelta, timezone

import pytest

from backend.historical_evidence_import_service import (
    HistoricalEvidenceImportError,
    HistoricalEvidenceImportService,
)
from backend.incremental_metrics_service import IncrementalMetricsService
from backend.metrics_evidence_adapter import MetricsEvidenceAdapter


def events():
    return [
        {
            "evidence_id": f"historical:{index:03d}",
            "environment_id": "china_uat",
            "requested_model": "deepseek-v4-flash",
            "actual_model": "deepseek-v4-flash",
            "actual_channel": None,
            "request_profile_id": "P01",
            "observed_at": (
                datetime(2026, 7, 29, 2, tzinfo=timezone.utc)
                + timedelta(minutes=index)
            ).isoformat(),
            "http_status": 200,
            "latency_ms": 1000 + index,
            "stream": False,
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
            "actual_cost": "0.000100",
            "currency": "CNY",
            "source_type": "measured_unified_uat",
            "source_reliability": 0.9,
            "reconciliation_provenance": {
                "plan_id": f"UR-V3-{index:03d}",
                "csv_row_number": index + 1,
            },
        }
        for index in range(1, 61)
    ]


def test_production_import_and_metrics_adapter_replay_are_idempotent(
        tmp_path):
    database = tmp_path / "metrics.sqlite3"
    imported_at = datetime(2026, 7, 29, 3, tzinfo=timezone.utc)
    importer = HistoricalEvidenceImportService(database, development_mode=True)
    first = importer.import_reconciliation(
        events(),
        evidence_manifest_sha256="A" * 64,
        reconciliation_result_sha256="B" * 64,
        imported_at=imported_at,
    )
    metrics = IncrementalMetricsService(database, development_mode=True)
    adapter = MetricsEvidenceAdapter(
        database,
        metrics,
        tmp_path / "not-read.jsonl",
        source_allowlist={"confirmed_imports"},
    )
    first_sync = adapter.synchronize(as_of=imported_at)
    replay = importer.import_reconciliation(
        events(),
        evidence_manifest_sha256="A" * 64,
        reconciliation_result_sha256="B" * 64,
        imported_at=imported_at,
    )
    second_sync = adapter.synchronize(as_of=imported_at)
    assert first["inserted_count"] == first_sync["inserted"] == 60
    assert replay["idempotent"] is True
    assert second_sync["inserted"] == 0
    with metrics.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM metric_evidence_events").fetchone()[0] == 60
        assert db.execute(
            "SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1


def test_import_rejects_incomplete_or_sensitive_projection(tmp_path):
    importer = HistoricalEvidenceImportService(
        tmp_path / "metrics.sqlite3", development_mode=True
    )
    with pytest.raises(
            HistoricalEvidenceImportError,
            match="historical_reconciliation_not_complete"):
        importer.import_reconciliation(
            events()[:59],
            evidence_manifest_sha256="A" * 64,
            reconciliation_result_sha256="B" * 64,
            imported_at=datetime.now(timezone.utc),
        )
    unsafe = events()
    unsafe[0]["api_key"] = "must-not-persist"
    with pytest.raises(
            HistoricalEvidenceImportError,
            match="historical_projection_contains_sensitive_field"):
        importer.import_reconciliation(
            unsafe,
            evidence_manifest_sha256="A" * 64,
            reconciliation_result_sha256="B" * 64,
            imported_at=datetime.now(timezone.utc),
        )
