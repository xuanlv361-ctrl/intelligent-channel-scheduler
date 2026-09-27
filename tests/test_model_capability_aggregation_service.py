from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backend.model_capability_aggregation_service import ModelCapabilityAggregationService
from backend.tenant_security import TenantScope


SCOPE = TenantScope.local_development()


def seed(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE standardized_call_logs(
          record_id TEXT,occurred_at TEXT,request_id TEXT,environment_id TEXT,
          requested_model TEXT,billed_model TEXT,actual_model TEXT,provider TEXT,
          channel_id TEXT,stream INTEGER,request_status TEXT,http_status INTEGER,
          error_category TEXT,total_latency_ms REAL,first_token_latency_ms REAL,
          input_tokens INTEGER,cached_input_tokens INTEGER,output_tokens INTEGER,
          cost_amount TEXT,currency TEXT,source_type TEXT,is_historical INTEGER,
          evidence_level TEXT,duplicate_of TEXT,duplicate_status TEXT,
          traffic_class TEXT,is_fault_injected INTEGER,error_source TEXT,
          tenant_id TEXT,workspace_id TEXT);
        CREATE TABLE capability_evidence(
          evidence_id TEXT,evidence_type TEXT,observed_at TEXT,environment_id TEXT,
          subject_id TEXT,source TEXT,requirement TEXT,state TEXT,details_json TEXT,
          valid_until TEXT,revoked_at TEXT,maximum_context_tokens INTEGER,
          tenant_id TEXT,workspace_id TEXT);
        """)
        historical = [(f"H-{index}", "2026-08-06T00:00:00+00:00", None, "国内UAT",
          "history-model", "history-billed", "history-actual", "provider-a", None,
          index % 2, "SUCCESS", 200, None, 100 + index % 3, None, 1, 0, 2,
          "0.01", "CNY", "historical_uat_csv", 1, "historical_statistics", None,
          "canonical", "business", 0, None, *SCOPE.sql_parameters()) for index in range(646)]
        db.executemany("INSERT INTO standardized_call_logs VALUES(" + ",".join("?" for _ in historical[0]) + ")", historical)
        rows = [
          ("R-stream", "2026-08-06T01:00:00Z", "REQ-1", "uat", "stream-model",
           "stream-billed", "stream-actual", "provider-b", None, 1, "completed", 200,
           None, 80, 20, 4, 1, 5, None, "CNY", "realtime_execution", 0, "exact", None,
           "canonical", "business", 0, "provider_live", *SCOPE.sql_parameters()),
          # A 503 is an availability observation, not unsupported capability evidence.
          ("R-503", "2026-08-06T01:01:00Z", "REQ-2", "china_uat", "pending-model",
           None, None, None, None, 0, "FAILED", 503, "upstream_unavailable", 40, None,
           2, 0, 0, None, None, "realtime_execution", 0, "exact", None, "canonical",
           "business", 0, "provider_live", *SCOPE.sql_parameters()),
          # Fault injection and probe traffic must not establish business capability.
          ("R-fault", "2026-08-06T01:02:00Z", "REQ-3", "china_uat", "fault-only",
           None, "fault-only", None, None, 1, "SUCCESS", 200, None, 1, 1, 1, 0, 1,
           None, None, "realtime_execution", 0, "exact", None, "canonical", "business",
           1, "uat_fault_injection", *SCOPE.sql_parameters()),
          ("R-probe", "2026-08-06T01:03:00Z", "REQ-4", "china_uat", "probe-only",
           None, "probe-only", None, None, 1, "SUCCESS", 200, None, 1, 1, 1, 0, 1,
           None, None, "realtime_execution", 0, "exact", None, "canonical", "probe",
           0, "provider_live", *SCOPE.sql_parameters()),
        ]
        db.executemany("INSERT INTO standardized_call_logs VALUES(" + ",".join("?" for _ in rows[0]) + ")", rows)
        evidence = [
          ("E-video", "live_test", "2026-08-06T01:04:00Z", "china_uat", "media-model",
           "live", "context", "supported", json.dumps({"output_modalities": ["video"]}),
           None, None, 8192, *SCOPE.sql_parameters()),
          ("E-image", "official", "2026-08-06T01:04:00Z", "uat", "media-model",
           "manual", "image_in", "supported", "{}", None, None, None,
           *SCOPE.sql_parameters()),
          ("E-audio", "official", "2026-08-06T01:04:00Z", "国内UAT", "media-model",
           "manual", "audio", "unsupported", json.dumps({"direction": ["input"]}),
           None, None, None, *SCOPE.sql_parameters()),
        ]
        db.executemany("INSERT INTO capability_evidence VALUES(" + ",".join("?" for _ in evidence[0]) + ")", evidence)


def catalog() -> dict:
    return {
      "status": "ready", "source_type": "uat_model_catalog", "updated_at": "2026-08-06T01:00:00Z",
      "data": [
        {"id": "history-actual", "owned_by": "provider-a", "context_window": 4096,
         "endpoints": ["/v1/chat/completions"]},
        {"id": "stream-actual", "owned_by": "provider-b", "capabilities": {"tool_calling": True}},
        {"id": "pending-model", "owned_by": "provider-c"},
        {"id": "media-model", "owned_by": "provider-d"},
      ],
    }


def test_real_logs_generate_observed_capabilities_without_request_or_channel(tmp_path: Path):
    path = tmp_path / "scheduler.db"
    seed(path)
    service = ModelCapabilityAggregationService(path, SCOPE)
    result = service.ensure(catalog(), "国内UAT")
    assert result["status"] == "ready"
    overview = service.overview("uat")
    assert overview["historical_log_count"] == 646
    assert overview["realtime_log_count"] == 2
    assert overview["catalog_model_count"] == 4
    assert overview["call_evidence_model_count"] == 3

    history = service.model_detail("history-actual", "china_uat")
    assert history["call_count"] == 646
    assert history["latest_request_id"] is None
    assert history["capabilities"]["text_input"]["status"] == "observed"
    assert history["capabilities"]["streaming"]["status"] == "observed"
    assert history["capabilities"]["non_streaming"]["status"] == "observed"

    stream = service.model_detail("stream-actual")
    assert stream["capabilities"]["streaming"]["status"] == "observed"
    assert stream["capabilities"]["tool_calling"]["status"] == "confirmed"
    assert stream["call_count"] == 1


def test_explicit_media_evidence_and_503_remain_conservative(tmp_path: Path):
    path = tmp_path / "scheduler.db"
    seed(path)
    service = ModelCapabilityAggregationService(path, SCOPE)
    service.ensure(catalog())
    media = service.model_detail("media-model")
    assert media["capabilities"]["image_understanding"]["status"] == "confirmed"
    assert media["capabilities"]["video_generation"]["status"] == "confirmed"
    assert media["capabilities"]["audio_input"]["status"] == "unsupported"

    pending = service.model_detail("pending-model")
    assert pending["call_count"] == 1
    assert pending["success_count"] == 0
    assert pending["capabilities"]["text_input"]["status"] == "pending"
    assert service.model_detail("fault-only")["status"] == "not_found"
    assert service.model_detail("probe-only")["status"] == "not_found"


def test_mapping_keeps_actual_null_and_distinguishes_exact_from_history(tmp_path: Path):
    path = tmp_path / "scheduler.db"
    seed(path)
    service = ModelCapabilityAggregationService(path, SCOPE)
    service.ensure(catalog())
    result = service.mappings("china-uat")
    history = next(item for item in result["items"] if item["requested_model"] == "history-model")
    assert history["actual_model"] == "history-actual"
    assert history["evidence_level"] == "historical_observation"
    assert history["call_count"] == 646
    exact = next(item for item in result["items"] if item["requested_model"] == "stream-model")
    assert exact["evidence_level"] == "exact_mapping"
    pending = next(item for item in result["items"] if item["requested_model"] == "pending-model")
    assert pending["actual_model"] is None
    assert pending["evidence_level"] == "pending"
    assert pending["missing_actual_count"] == 1


def test_ensure_is_content_addressed_and_idempotent(tmp_path: Path):
    path = tmp_path / "scheduler.db"
    seed(path)
    service = ModelCapabilityAggregationService(path, SCOPE)
    first = service.ensure(catalog())
    second = service.ensure(catalog())
    assert first["job_id"] == second["job_id"]
    assert second["already_current"] is True
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM model_catalog_snapshots").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM model_capability_aggregation_jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM model_capability_aggregates").fetchone()[0] == first["model_count"]
        assert db.execute("SELECT COUNT(*) FROM model_mapping_aggregates").fetchone()[0] == first["mapping_count"]

