import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.realtime_log_sync_routes import build_realtime_log_sync_router
from backend.log_schema_adapters import SchemaObservationError
from backend.realtime_log_sync_service import (
    RealtimeLogSyncError, RealtimeLogSyncService, safe_url, sanitize,
)
from backend.call_log_service import CallLogService

DATE_FROM = "2026-07-29T00:00:00Z"
DATE_TO = "2026-07-29T02:00:00Z"
TIMEZONE = "Asia/Shanghai"


class Runtime:
    def active(self, environment, key):
        if environment == "overseas" and key == "logs_page_url":
            return {
                "setting_value": "https://weimeta.ai/operator-confirmed/logs",
                "value_sha256": "a" * 64,
            }
        return None


def execution(execution_id="EXEC-1", request_id="REQ-1", response_id="RESP-1",
              decision_id="DEC-1"):
    return {
        "execution_id": execution_id, "request_id": request_id,
        "response_id": response_id, "decision_id": decision_id,
        "actual_model": "model-a",
        "total_tokens": 30, "estimated_cost": 0.01,
        "scheduler_recommendation": "CHANNEL-RECOMMENDED",
    }


def service(tmp_path, executions=None):
    path = tmp_path / "executions.jsonl"
    rows = executions if executions is not None else [execution()]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = RealtimeLogSyncService(
        tmp_path / "sync.sqlite3", path, Runtime())
    result.schema_registry = SyntheticReviewedRegistry()
    return result


def syncing(svc, environment="china_uat"):
    job = svc.create_job(environment, DATE_FROM, DATE_TO, TIMEZONE, 5, 20)
    svc.update_job(
        job["sync_job_id"], state="waiting_for_manual_login",
        browser_context_active=1,
        current_safe_url=job["safe_log_page_url"],
    )
    return svc.confirm_login(job["sync_job_id"], True)


def payload(**overrides):
    row = {
        "platform_log_id": "LOG-1", "request_id": "REQ-1",
        "response_id": "RESP-1", "model": "model-a", "http_status": 200,
        "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
        "cost": 0.012, "currency": "CNY", "channel_id": "CHANNEL-A",
        "created_at": "2026-07-29T01:00:00Z",
    }
    row.update(overrides)
    return {"data": [row]}


def test_single_response_cannot_overshoot_observed_or_accepted_limits(
        tmp_path):
    svc = service(tmp_path)
    job = svc.create_job(
        "china_uat", DATE_FROM, DATE_TO, TIMEZONE, 5, 2,
        maximum_http_reads=2, maximum_records_observed=2,
        maximum_records_accepted=2, maximum_elapsed_seconds=60)
    svc.update_job(
        job["sync_job_id"], state="waiting_for_manual_login",
        browser_context_active=1,
        current_safe_url=job["safe_log_page_url"])
    job = svc.confirm_login(job["sync_job_id"], True)
    rows = []
    for index in range(5):
        row = payload(
            platform_log_id=f"LOG-{index}",
            request_id=f"REQ-{index}",
            response_id=f"RESP-{index}")["data"][0]
        rows.append(row)
    result = svc.ingest(
        job["sync_job_id"], {"data": rows},
        "https://uat.weimeta.cn/api/log/self")
    assert result["collected_count"] == 2
    assert result["inserted_count"] == 2
    assert result["bounded_overflow_count"] == 3
    with svc.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM realtime_log_records").fetchone()[0] == 2


class SyntheticReviewedRegistry:
    """Explicit, test-only adapter for the historical synthetic fixture."""
    allowed_fields = {
        "platform_log_id", "request_id", "response_id", "decision_id", "model",
        "http_status", "prompt_tokens", "completion_tokens", "total_tokens",
        "cost", "currency", "channel_id", "created_at",
    }

    def select(self, observation):
        if observation.sensitive_field_count:
            raise SchemaObservationError("credential_like_field_present")
        try:
            item_shapes = observation.shape["fields"]["data"]["item_shapes"]
            fields = set(item_shapes[0]["fields"]) if item_shapes else set()
        except (KeyError, IndexError, TypeError):
            return None
        if not fields or not fields <= self.allowed_fields:
            return None
        return {
            "schema_adapter_id": "synthetic_test_records",
            "adapter_version": "test-only",
        }

    @staticmethod
    def extract(_adapter, source):
        return list(source["data"]), {}


@pytest.mark.parametrize("url", [
    "http://uat.weimeta.cn/console/billing/logs",
    "https://evil.uat.weimeta.cn/console/billing/logs",
    "https://uat.weimeta.cn.evil.test/console/billing/logs",
])
def test_exact_host_and_https_validation(url, tmp_path, monkeypatch):
    svc = service(tmp_path)
    monkeypatch.setattr(svc, "resolve_environment", lambda environment: {
        "environment_id": environment, "log_page_url": url,
        "console_url": "https://uat.weimeta.cn",
        "allowed_hosts": ["uat.weimeta.cn", "admin-uat.weimeta.cn"],
        "source_type": "measured_uat_realtime_browser_sync",
    } if url.startswith("https://uat.weimeta.cn/") else (_ for _ in ()).throw(
        RealtimeLogSyncError("log_sync_log_page_not_allowed")))
    if url.startswith("https://uat.weimeta.cn/"):
        assert svc.create_job("china_uat", DATE_FROM, DATE_TO, TIMEZONE)["safe_log_page_url"] == url
    else:
        with pytest.raises(RealtimeLogSyncError):
            svc.create_job("china_uat", DATE_FROM, DATE_TO, TIMEZONE)


def test_one_active_job_per_environment_and_environment_isolation(tmp_path):
    svc = service(tmp_path)
    china = svc.create_job("china_uat", DATE_FROM, DATE_TO, TIMEZONE)
    with pytest.raises(RealtimeLogSyncError, match="already_active"):
        svc.create_job("china_uat", DATE_FROM, DATE_TO, TIMEZONE)
    overseas = svc.create_job("overseas", DATE_FROM, DATE_TO, TIMEZONE)
    assert china["environment_id"] != overseas["environment_id"]
    assert china["source_type"] == "measured_uat_realtime_browser_sync"
    assert overseas["source_type"] == "measured_overseas_realtime_browser_sync"


def test_manual_login_and_confirmation_are_required(tmp_path):
    svc = service(tmp_path)
    job = svc.create_job("china_uat", DATE_FROM, DATE_TO, TIMEZONE)
    with pytest.raises(RealtimeLogSyncError, match="manual_login_required"):
        svc.ingest(job["sync_job_id"], payload(), "https://uat.weimeta.cn/api/log/self")
    svc.update_job(job["sync_job_id"], state="waiting_for_manual_login",
                   current_safe_url=job["safe_log_page_url"])
    with pytest.raises(RealtimeLogSyncError, match="explicit_confirmation"):
        svc.confirm_login(job["sync_job_id"], False)
    with pytest.raises(RealtimeLogSyncError, match="not_visible"):
        svc.confirm_login(job["sync_job_id"], True, "https://uat.weimeta.cn/login")


@pytest.mark.parametrize("source", [
    "https://user:password@uat.weimeta.cn/api/log/self",
    "https://uat.weimeta.cn:444/api/log/self",
    "https://uat.weimeta.cn/api/log/other",
])
def test_domestic_evidence_source_endpoint_is_exact_and_credential_free(
        tmp_path, source):
    svc = service(tmp_path)
    job = syncing(svc)
    with pytest.raises(
            RealtimeLogSyncError, match="log_sync_source_not_allowed"):
        svc.ingest(job["sync_job_id"], payload(), source)
    with svc.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM realtime_log_evidence").fetchone()[0] == 0
    assert "user:password" not in safe_url(source)


def test_stop_destroys_context_flags_and_preserves_records(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    svc.ingest(job["sync_job_id"], payload(), "https://uat.weimeta.cn/api/log/self")
    stopped = svc.stop(job["sync_job_id"])
    assert stopped["state"] == "stopped"
    assert stopped["browser_context_active"] is False
    with svc.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM realtime_log_records").fetchone()[0] == 1


def test_session_expiry_preserves_evidence_and_clears_context(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    svc.ingest(job["sync_job_id"], payload(), "https://uat.weimeta.cn/api/log/self")
    expired = svc.mark_session_expired(job["sync_job_id"])
    assert expired["state"] == "session_expired"
    assert expired["browser_context_active"] is False
    assert svc.health()["records"] == 1


def test_incremental_cursor_idempotency_does_not_match_local_request_id(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    first = svc.ingest(job["sync_job_id"], payload(), "https://uat.weimeta.cn/api/log/self")
    second = svc.ingest(job["sync_job_id"], payload(), "https://uat.weimeta.cn/api/log/self")
    assert first["inserted_count"] == 1 and first["correlated_count"] == 0
    assert second["inserted_count"] == 1 and second["duplicate_count"] == 1
    with svc.connect() as db:
        cursor = db.execute("SELECT * FROM realtime_log_sync_cursors").fetchone()
        correlation = db.execute("SELECT * FROM realtime_log_correlations").fetchone()
    assert cursor["cursor_kind"] == "platform_log_id" and cursor["cursor_value"] == "LOG-1"
    assert correlation["state"] == "unmatched"


def test_exact_provider_request_match_enriches_unified_log_with_provider_channel(tmp_path):
    svc = service(tmp_path)
    ledger = CallLogService(tmp_path / "sync.sqlite3")
    record_id = ledger.start_execution(
        request_id="REQ-1", decision_id="DEC-1", environment_id="china_uat",
        requested_model="model-a", stream=False,
    )
    ledger.finish_execution(
        record_id, status="SUCCESS", response_id="RESP-1",
        provider_request_id="REQ-1", actual_model="model-a", http_status=200,
    )

    job = syncing(svc)
    svc.ingest(job["sync_job_id"], payload(),
               "https://uat.weimeta.cn/api/log/self")

    with ledger.connect() as db:
        row = db.execute("""SELECT provider_log_id,channel_id,channel_source
          FROM standardized_call_logs WHERE record_id=?""", (record_id,)).fetchone()
    assert dict(row) == {
        "provider_log_id": "LOG-1",
        "channel_id": "CHANNEL-A",
        "channel_source": "provider_log",
    }


def test_response_id_is_not_used_when_request_id_absent(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    result = svc.ingest(
        job["sync_job_id"], payload(request_id=None),
        "https://uat.weimeta.cn/api/log/self")
    assert result["correlated_count"] == 0
    with svc.connect() as db:
        assert db.execute("SELECT state FROM realtime_log_correlations").fetchone()[0] == "unmatched"


def test_decision_id_is_not_used_when_request_and_response_absent(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    result = svc.ingest(
        job["sync_job_id"],
        payload(request_id=None, response_id=None, decision_id="DEC-1"),
        "https://uat.weimeta.cn/api/log/self")
    assert result["correlated_count"] == 0
    with svc.connect() as db:
        assert db.execute(
            "SELECT state FROM realtime_log_correlations").fetchone()[0] == \
            "unmatched"


def test_composite_fields_are_never_used_for_cost_correlation(tmp_path):
    rows = [execution("E1", "A", "B"), execution("E2", "C", "D")]
    svc = service(tmp_path, rows)
    job = syncing(svc)
    result = svc.ingest(
        job["sync_job_id"],
        payload(platform_log_id=None, request_id=None, response_id=None),
        "https://uat.weimeta.cn/api/log/self")
    assert result["correlated_count"] == 0 and result["ambiguous_count"] == 0
    with svc.connect() as db:
        row = db.execute("SELECT state,execution_id FROM realtime_log_correlations").fetchone()
    assert row["state"] == "unmatched" and row["execution_id"] is None


def test_cost_enrichment_and_actual_channel_are_platform_only(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    svc.ingest(job["sync_job_id"], payload(), "https://uat.weimeta.cn/api/log/self")
    item = svc.reconciliation("china_uat")[0]
    assert item["actual_cost"] == 0.012 and item["currency"] == "CNY"
    assert item["actual_channel_id"] == "CHANNEL-A"
    assert item["scheduler_recommendation"] is None
    assert item["reconciliation_status"] == "cost_confirmed"


def test_exact_provider_log_match_backfills_actual_cost_into_unified_log(tmp_path):
    svc = service(tmp_path)
    call_logs = CallLogService(svc.database_path)
    record_id = call_logs.start_execution(
        request_id="REQ-1", decision_id="DEC-1",
        environment_id="china_uat", requested_model="model-a",
        stream=False)
    call_logs.finish_execution(
        record_id, status="SUCCESS", response_id="RESP-1",
        provider_request_id="REQ-1", actual_model="model-a", http_status=200,
        input_tokens=10, output_tokens=20, total_latency_ms=1000)
    job = syncing(svc)
    svc.ingest(
        job["sync_job_id"], payload(),
        "https://uat.weimeta.cn/api/log/self")
    with call_logs.connect() as db:
        row = db.execute("""SELECT cost_amount,currency,cost_source,
          price_source,provider_log_id FROM standardized_call_logs
          WHERE record_id=?""", (record_id,)).fetchone()
        attempt = db.execute("""SELECT cost_amount,currency,cost_source
          FROM call_attempt_logs WHERE request_id='REQ-1'""").fetchone()
    assert dict(row) == {
        "cost_amount": "0.012", "currency": "CNY",
        "cost_source": "provider_log",
        "price_source": "provider_log_quota",
        "provider_log_id": "LOG-1",
    }
    assert dict(attempt) == {
        "cost_amount": "0.012", "currency": "CNY",
        "cost_source": "provider_log",
    }


def test_manual_confirmation_is_audited_and_not_counted_as_exact(tmp_path):
    svc = service(tmp_path)
    ledger = CallLogService(svc.database_path)
    record_id = ledger.start_execution(
        request_id="REQ-LOCAL", decision_id="DEC-1",
        environment_id="china_uat", requested_model="model-a", stream=False)
    ledger.finish_execution(
        record_id, status="SUCCESS", response_id="RESP-LOCAL",
        actual_model="model-a", http_status=200)
    job = syncing(svc)
    result = svc.ingest(job["sync_job_id"], payload(request_id="PROVIDER-1"),
                        "https://uat.weimeta.cn/api/log/self")
    assert result["correlated_count"] == 0
    with svc.connect() as db:
        provider_record_id = db.execute(
            "SELECT record_id FROM realtime_log_records").fetchone()[0]
    confirmed = svc.manually_confirm_correlation(
        provider_record_id, "REQ-LOCAL", "operator reviewed Provider evidence")
    assert confirmed["status"] == "manually_confirmed"
    assert confirmed["audit_id"].startswith("AUD-")
    with svc.connect() as db:
        correlation = db.execute("""SELECT state,method,match_confidence
          FROM realtime_log_correlations WHERE record_id=?""",
                                 (provider_record_id,)).fetchone()
        row = db.execute("""SELECT cost_status,match_method
          FROM standardized_call_logs WHERE local_request_id='REQ-LOCAL'""").fetchone()
    assert dict(correlation) == {
        "state": "manually_confirmed", "method": "manually_confirmed",
        "match_confidence": "manual",
    }
    assert dict(row) == {
        "cost_status": "provider_actual", "match_method": "manually_confirmed",
    }


def test_model_time_and_token_similarity_never_authorize_cost_match(tmp_path):
    svc = service(tmp_path)
    ledger = CallLogService(svc.database_path)
    record_id = ledger.start_execution(
        request_id="REQ-LOCAL", decision_id="DEC-1",
        environment_id="china_uat", requested_model="model-a", stream=False)
    ledger.finish_execution(
        record_id, status="SUCCESS", response_id="RESP-LOCAL",
        actual_model="model-a", http_status=200,
        input_tokens=10, output_tokens=20, total_latency_ms=1000)
    job = syncing(svc)
    result = svc.ingest(
        job["sync_job_id"], payload(request_id="UNRELATED-PROVIDER-ID"),
        "https://uat.weimeta.cn/api/log/self")
    assert result["correlated_count"] == 0
    with ledger.connect() as db:
        row = db.execute("""SELECT cost_amount,cost_status
          FROM standardized_call_logs WHERE record_id=?""", (record_id,)).fetchone()
    assert row["cost_amount"] is None
    assert row["cost_status"] != "provider_actual"


def test_missing_actual_channel_remains_null(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    svc.ingest(
        job["sync_job_id"], payload(channel_id=None),
        "https://uat.weimeta.cn/api/log/self")
    item = svc.reconciliation("china_uat")[0]
    assert item["actual_channel_id"] is None
    assert item["channel_evidence_status"] == "channel_evidence_unavailable"


def test_sanitized_evidence_contains_no_credentials_or_bodies(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    unsafe = payload()
    unsafe.update({
        "Authorization": "Bearer SECRET-VALUE", "Cookie": "session=secret",
        "password": "secret", "messages": [{"content": "private prompt"}],
        "request_body": "private request",
    })
    svc.ingest(job["sync_job_id"], unsafe, "https://uat.weimeta.cn/api/log/self")
    with svc.connect() as db:
        evidence = db.execute("SELECT sanitized_payload FROM realtime_log_evidence").fetchone()[0]
    assert "SECRET-VALUE" not in evidence
    assert "private prompt" not in evidence and "private request" not in evidence
    assert "authorization" not in evidence.casefold() and "cookie" not in evidence.casefold()


def test_unknown_schema_requires_mapping_and_does_not_guess(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    result = svc.ingest(
        job["sync_job_id"], {"data": [{"mystery": "value"}]},
        "https://uat.weimeta.cn/api/log/self")
    assert result["schema_mapping_required"] is True
    assert result["rejected_count"] == 1 and result["inserted_count"] == 0


def test_events_health_and_zero_completion_calls(tmp_path):
    svc = service(tmp_path)
    syncing(svc)
    events = svc.events()
    assert events and all("details" in event for event in events)
    assert svc.health()["completion_api_calls"] == 0
    assert svc.health()["cookies_persisted"] is False


def test_routes_start_confirm_stop_and_poll_events(tmp_path):
    svc = service(tmp_path)
    launched, stopped = [], []
    app = FastAPI()
    app.include_router(build_realtime_log_sync_router(
        svc, lambda job_id: launched.append(job_id) or 123,
        lambda job_id: stopped.append(job_id) or True, lambda request: None))
    client = TestClient(app)
    created = client.post("/api/v1/log-sync/jobs", json={
        "environment_id": "china_uat", "date_from": DATE_FROM,
        "date_to": DATE_TO, "timezone": TIMEZONE, "sync_interval_seconds": 5,
        "maximum_records": 20}).json()
    assert launched == [created["sync_job_id"]]
    svc.update_job(
        created["sync_job_id"], state="waiting_for_manual_login",
        current_safe_url=created["safe_log_page_url"], browser_context_active=1)
    confirmed = client.post(
        f"/api/v1/log-sync/jobs/{created['sync_job_id']}/confirm-login",
        json={"environment_id": "china_uat", "explicit_confirmation": True}).json()
    assert confirmed["state"] == "syncing"
    events = client.get("/api/v1/log-sync/events?environment_id=china_uat").json()
    assert events["transport"] == "short_polling"
    result = client.post(
        f"/api/v1/log-sync/jobs/{created['sync_job_id']}/stop",
        json={"environment_id": "china_uat", "explicit_confirmation": True}).json()
    assert result["state"] == "stopped" and stopped == [created["sync_job_id"]]


def test_pagination_and_poll_bounds(tmp_path):
    svc = service(tmp_path)
    for interval in (2, 31):
        with pytest.raises(RealtimeLogSyncError):
            svc.create_job("china_uat", DATE_FROM, DATE_TO, TIMEZONE, interval, 10)
    with pytest.raises(RealtimeLogSyncError):
        svc.create_job("china_uat", DATE_FROM, DATE_TO, TIMEZONE, 5, 1001)


def test_backend_persists_normalized_utc_range_and_timezone(tmp_path):
    svc = service(tmp_path)
    job = svc.create_job(
        "china_uat", "2026-07-29T08:00:00+08:00",
        "2026-07-29T09:00:00+08:00", "Asia/Shanghai", 7, 50)
    assert job["date_from_utc"] == "2026-07-29T00:00:00+00:00"
    assert job["date_to_utc"] == "2026-07-29T01:00:00+00:00"
    assert job["timezone"] == "Asia/Shanghai"


def test_invalid_missing_and_reversed_ranges_are_rejected(tmp_path):
    svc = service(tmp_path)
    with pytest.raises(RealtimeLogSyncError, match="range_order"):
        svc.create_job(
            "china_uat", DATE_TO, DATE_FROM, TIMEZONE)
    with pytest.raises(RealtimeLogSyncError, match="timezone_required"):
        svc.create_job(
            "china_uat", "2026-07-29T00:00", DATE_TO, TIMEZONE)
    with pytest.raises(RealtimeLogSyncError, match="date_from_required"):
        svc.create_job("china_uat", "", DATE_TO, TIMEZONE)


def test_out_of_range_and_missing_timestamps_are_excluded_without_guessing(tmp_path):
    svc = service(tmp_path)
    job = syncing(svc)
    result = svc.ingest(job["sync_job_id"], {"data": [
        payload()["data"][0],
        {**payload(platform_log_id="LOG-OUT")["data"][0],
         "created_at": "2026-07-29T03:00:00Z"},
        {**payload(platform_log_id="LOG-MISSING")["data"][0],
         "created_at": None},
        {**payload(platform_log_id="LOG-INVALID")["data"][0],
         "created_at": "not-a-time"},
    ]}, "https://uat.weimeta.cn/api/log/self")
    assert result["collected_count"] == 4
    assert result["inserted_count"] == 1
    assert result["out_of_range_count"] == 1
    assert result["missing_timestamp_count"] == 2
    with svc.connect() as db:
        rows = db.execute(
            "SELECT normalized_json FROM realtime_log_records").fetchall()
    assert len(rows) == 1
    normalized = json.loads(rows[0][0])
    assert normalized["original_safe_timestamp_text"] == "2026-07-29T01:00:00Z"
    assert normalized["normalized_utc_timestamp"] == "2026-07-29T01:00:00+00:00"


def test_sanitize_recursive_contract():
    cleaned = sanitize({
        "safe": {"value": 1}, "headers": {
            "Authorization": "Bearer x", "Set-Cookie": "x=y"},
        "prompt": "private", "text": "Bearer secret",
    })
    assert cleaned == {"safe": {"value": 1}, "headers": {}, "text": "[REDACTED]"}


def test_failed_job_exposes_safe_stop_reason_timestamp_and_next_action(tmp_path):
    svc = service(tmp_path)
    job = svc.create_job(
        "china_uat", DATE_FROM, DATE_TO, TIMEZONE, 7, 50)
    failed = svc.update_job(
        job["sync_job_id"], state="failed",
        stopped_at="2026-07-29T02:00:00+00:00",
        error_code="log_sync_browser_launch_failed",
        safe_error_message="无法启动本地可见 Chromium。")
    assert failed["stop_reason"] == "log_sync_browser_launch_failed"
    assert failed["error_at"].endswith("+00:00")
    assert "Chromium" in failed["suggested_next_action"]
    rendered = json.dumps(failed, ensure_ascii=False)
    assert "Authorization" not in rendered and "Cookie" not in rendered
