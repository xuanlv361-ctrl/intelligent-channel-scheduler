from __future__ import annotations

import sqlite3
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backend.call_log_service import CallLogError, CallLogService
from backend.uat_service import UatSettings, UatStore, execute


CSV_PATH = Path(r"E:\lx\调用日志_20260730_172539.csv")
CSV_SHA256 = "BACBC4F5F4455E1A77969C741A1770ED8AACF75BC02D97DD6A8836769C80DA92"


def test_real_historical_csv_imports_646_once_and_is_idempotent(tmp_path: Path):
    assert CSV_PATH.is_file(), "required historical UAT CSV is unavailable"
    service = CallLogService(tmp_path / "logs.sqlite3")
    first = service.initialize_historical_csv(CSV_PATH)
    second = service.initialize_historical_csv(CSV_PATH)

    assert first["file_sha256"] == CSV_SHA256
    assert first["file_size"] == 118626
    assert first["source_row_count"] == 646
    assert first["imported_count"] == first["inserted_count"] == 646
    assert first["rejected_count"] == 0
    assert second["idempotent"] is True
    assert second["inserted_count"] == 0
    assert service.list_records(limit=500)["total"] == 646

    restarted = CallLogService(tmp_path / "logs.sqlite3")
    assert restarted.initialize_historical_csv(CSV_PATH)["inserted_count"] == 0
    assert restarted.list_records(limit=1)["total"] == 646


def test_historical_analytics_and_model_mapping_are_exact(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    service.initialize_historical_csv(CSV_PATH)
    result = service.analytics()
    assert result["request_count"] == 646
    assert result["success_count"] == 645
    assert result["failure_count"] == 1
    assert result["stream_count"] == 15
    assert result["nonstream_count"] == 631
    assert result["input_tokens"] == 9453
    assert result["cached_input_tokens"] == 3704
    assert result["output_tokens"] == 68318
    assert result["total_cost"] == "13.256422"
    assert result["model_mismatch_count"] == 80
    # Model usage aggregates by the actually executed model.  The requested
    # and billed aliases remain separately traceable in the ledger.
    assert result["model_counts"]["gpt-image-2-t"] == 80
    with service.connect() as db:
        paired = db.execute("""SELECT requested_model,billed_model,actual_model,
          pricing_detail,price_source,price_version,evidence_level
          FROM standardized_call_logs WHERE requested_model='gpt-image-2'
          AND actual_model='gpt-image-2-t' LIMIT 1""").fetchone()
        streamed = db.execute("""SELECT duration_type,first_token_latency_ms
          FROM standardized_call_logs WHERE stream=1 LIMIT 1""").fetchone()
    assert dict(paired) == {
        "requested_model": "gpt-image-2", "billed_model": "gpt-image-2",
        "actual_model": "gpt-image-2-t", "pricing_detail": paired["pricing_detail"],
        "price_source": "historical_log_detail",
        "price_version": "unversioned_historical_snapshot",
        "evidence_level": "historical_statistics",
    }
    assert paired["pricing_detail"]
    assert streamed["duration_type"] == "total_and_first_token"
    assert streamed["first_token_latency_ms"] is not None


def test_raw_api_key_and_ip_never_persist(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    service.initialize_historical_csv(CSV_PATH)
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        import csv
        source_rows = list(csv.DictReader(handle))
    first = source_rows[0]
    first_ip = next((row["IP"] for row in source_rows if row["IP"].strip() not in {"", "-"}), None)
    database_bytes = (tmp_path / "logs.sqlite3").read_bytes()
    assert first["API Key"].encode("utf-8") not in database_bytes
    if first_ip:
        assert first_ip.encode("utf-8") not in database_bytes
    with sqlite3.connect(tmp_path / "logs.sqlite3") as db:
        raw_ip_count = db.execute(
            "SELECT COUNT(*) FROM standardized_call_logs WHERE source_ip IS NOT NULL"
        ).fetchone()[0]
        aliases = db.execute(
            "SELECT api_key_alias,source_ip_digest FROM standardized_call_logs LIMIT 1"
        ).fetchone()
    assert raw_ip_count == 0
    assert aliases[0].startswith("AKA-")
    assert aliases[1].startswith("IPD-")


def test_historical_rows_are_excluded_from_channel_health(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    service.initialize_historical_csv(CSV_PATH)
    assert service.channel_health_rows() == []

    record = service.start_execution(
        request_id="REQ-CHANNEL", decision_id="DEC-CHANNEL",
        environment_id="china_uat", requested_model="deepseek-v4-flash",
        stream=False, channel_id="authoritative-channel-1",
    )
    service.finish_execution(
        record, status="SUCCESS", actual_model="deepseek-v4-flash",
        http_status=200, total_latency_ms=120, input_tokens=3,
        output_tokens=4, cost_amount=Decimal("0.01"), currency="CNY",
    )
    rows = service.channel_health_rows()
    assert len(rows) == 1
    assert rows[0]["channel_id"] == "authoritative-channel-1"
    assert rows[0]["source_type"] == "realtime_execution"


def test_provider_identifiers_are_separate_and_safe_header_ids_are_persisted(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    record = service.start_execution(
        request_id="REQ-LOCAL", decision_id="DEC-1",
        environment_id="china_uat", requested_model="model-a", stream=False)
    service.finish_execution(
        record, status="SUCCESS", actual_model="model-a", http_status=200,
        provider_request_id="PROVIDER-REQ-1",
        provider_response_id="PROVIDER-RESP-1",
        provider_trace_id="PROVIDER-TRACE-1",
        provider_identifiers={"x-request-id": "PROVIDER-REQ-1",
                              "response_body.id": "PROVIDER-RESP-1"})
    with service.connect() as db:
        row = db.execute("""SELECT request_id,local_request_id,
          provider_request_id,provider_response_id,provider_trace_id,
          provider_identifiers_json FROM standardized_call_logs
          WHERE record_id=?""", (record,)).fetchone()
    assert row["request_id"] == row["local_request_id"] == "REQ-LOCAL"
    assert row["provider_request_id"] == "PROVIDER-REQ-1"
    assert row["provider_response_id"] == "PROVIDER-RESP-1"
    assert row["provider_trace_id"] == "PROVIDER-TRACE-1"
    assert json.loads(row["provider_identifiers_json"]) == {
        "response_body.id": "PROVIDER-RESP-1",
        "x-request-id": "PROVIDER-REQ-1",
    }


def test_cost_records_use_complete_deduplicated_unified_ledger(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    first = service.start_execution(
        request_id="REQ-COST", decision_id="DEC-COST",
        environment_id="china_uat", requested_model="model-cost", stream=False,
        channel_id=None,
    )
    service.finish_execution(
        first, status="SUCCESS", actual_model="model-cost", http_status=200,
        total_latency_ms=321, input_tokens=10, cached_input_tokens=2,
        output_tokens=4, cost_amount="0.0123", currency="CNY",
    )
    duplicate = service.start_execution(
        request_id="REQ-DUP", decision_id="DEC-DUP",
        environment_id="china_uat", requested_model="model-cost", stream=False,
        channel_id=None,
    )
    service.finish_execution(
        duplicate, status="SUCCESS", actual_model="model-cost", http_status=200,
        total_latency_ms=321, input_tokens=10, cached_input_tokens=2,
        output_tokens=4, cost_amount="0.0123", currency="CNY",
    )
    with service.connect() as db:
        db.execute("""UPDATE standardized_call_logs SET duplicate_status='exact_duplicate',
          duplicate_of=? WHERE record_id=?""", (first, duplicate))

    rows = service.cost_records(environment_id="china_uat")
    assert len(rows) == 1
    assert rows[0]["request_id"] == "REQ-COST"
    assert rows[0]["cost_amount"] == "0.0123"
    assert rows[0]["input_tokens"] == 10


def test_realtime_success_failure_stream_and_attempt_lifecycle(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    success = service.start_execution(
        request_id="REQ-SUCCESS", decision_id="DEC-SUCCESS",
        environment_id="china_uat", requested_model="model-a", stream=True,
        channel_id="channel-a",
    )
    running = service.list_records(limit=10)["items"][0]
    assert running["request_status"] == "RUNNING"
    service.finish_execution(
        success, status="SUCCESS", response_id="RESP-SUCCESS",
        actual_model="model-a", http_status=200, total_latency_ms=900,
        first_token_latency_ms=110, input_tokens=10, cached_input_tokens=2,
        output_tokens=20, cost_amount="0.03", currency="CNY",
    )

    failed = service.start_execution(
        request_id="REQ-FAILED", decision_id="DEC-FAILED",
        environment_id="china_uat", requested_model="model-b", stream=False,
        channel_id="channel-b",
    )
    service.record_attempt(
        request_id="REQ-FAILED", attempt_number=2, requested_model="model-b",
        channel_id="channel-c", status="FAILED", http_status=429,
        error_code="rate_limited", error_category="rate_limited", retryable=True,
        total_latency_ms=45, started_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    service.finish_execution(
        failed, status="FAILED", http_status=429, error_code="rate_limited",
        error_category="rate_limited", retryable=True, total_latency_ms=50,
        total_attempts=2,
    )

    rows = {row["request_id"]: row for row in service.list_records(limit=10)["items"]}
    assert rows["REQ-SUCCESS"]["request_status"] == "SUCCESS"
    assert rows["REQ-SUCCESS"]["first_token_latency_ms"] == 110
    assert rows["REQ-FAILED"]["request_status"] == "FAILED"
    assert rows["REQ-FAILED"]["total_attempts"] == 2
    assert [row["attempt_number"] for row in service.attempts("REQ-FAILED")] == [1, 2]
    summary = service.analytics()
    assert summary["retry_count"] == 1
    assert summary["fallback_count"] == 1


def test_stale_running_is_terminalized_and_cannot_be_finished_twice(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    record = service.start_execution(
        request_id="REQ-STALE", decision_id=None, environment_id="china_uat",
        requested_model="model-a", stream=False,
    )
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with service.connect() as db:
        db.execute(
            "UPDATE standardized_call_logs SET occurred_at=? WHERE record_id=?", (stale, record)
        )
    assert service.expire_stale_running(timeout_seconds=60) == 1
    row = service.list_records(limit=1)["items"][0]
    assert row["request_status"] == "TIMEOUT"
    with pytest.raises(CallLogError, match="call_log_already_terminal"):
        service.finish_execution(record, status="SUCCESS")


def test_incremental_change_cursor_delivers_running_terminal_update_once(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    record = service.start_execution(
        request_id="REQ-CURSOR", decision_id="DEC-CURSOR", environment_id="china_uat",
        requested_model="model-a", stream=False,
    )
    first = service.list_records(after_cursor=0, limit=10)
    assert first["items"][0]["record_id"] == record
    assert first["items"][0]["request_status"] == "RUNNING"

    service.finish_execution(record, status="FAILED", error_category="transport_error")
    changed = service.list_records(after_cursor=first["next_cursor"], limit=10)
    assert len(changed["items"]) == 1
    assert changed["items"][0]["record_id"] == record
    assert changed["items"][0]["request_status"] == "FAILED"
    assert changed["next_cursor"] > first["next_cursor"]
    assert service.list_records(after_cursor=changed["next_cursor"], limit=10)["items"] == []


def test_analytics_provides_traceable_trend_and_model_costs(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    service.initialize_historical_csv(CSV_PATH)
    result = service.analytics()
    assert sum(item["request_count"] for item in result["trend"]) == 646
    assert sum(Decimal(value) for value in result["model_costs"].values()) == Decimal("13.256422")
    assert result["today_request_count"] == 0
    assert result["today_tokens"] == 0
    # No call occurred today, so cost is unknown/not observed rather than a
    # fabricated zero-cost measurement.
    assert result["today_cost"] is None


def test_realtime_analytics_does_not_invent_cost_or_double_count_cache_tokens(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    record_id = service.start_execution(
        request_id="REQ-REALTIME", decision_id="DEC-REALTIME",
        environment_id="china_uat", requested_model="model-a", stream=False,
    )
    service.finish_execution(
        record_id, status="SUCCESS", http_status=200,
        input_tokens=187, cached_input_tokens=114, output_tokens=112,
        cost_amount=None, currency=None,
    )

    result = service.analytics()

    assert result["today_tokens"] == 299
    assert result["today_cost"] is None
    assert result["trend"][-1]["total_tokens"] == 299
    assert result["trend"][-1]["total_cost"] is None
    assert result["trend"][-1]["currency"] is None


def test_dedup_fingerprint_never_merges_distinct_request_ids(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    occurred_at = "2026-08-05T01:02:03+00:00"
    for suffix in ("A", "B"):
        record_id = service.start_execution(
            request_id=f"REQ-{suffix}", decision_id=f"DEC-{suffix}",
            environment_id="china_uat", requested_model="model-a", stream=False,
        )
        service.finish_execution(
            record_id, status="SUCCESS", actual_model="model-a", http_status=200,
            total_latency_ms=100, input_tokens=3, output_tokens=4,
            cost_amount="0.01", currency="CNY",
        )
        with service.connect() as db:
            db.execute("""UPDATE standardized_call_logs SET occurred_at=?,created_at=?,updated_at=?
              WHERE record_id=?""", (occurred_at, occurred_at, occurred_at, record_id))

    dedup = service.reconcile_duplicates()
    assert dedup["raw_count"] == 2
    assert dedup["exact_duplicate_count"] == 0
    assert dedup["effective_count"] == 2
    assert service.analytics()["request_count"] == 2


def test_uat_execution_writes_running_then_terminal_without_network(tmp_path: Path):
    call_logs = CallLogService(tmp_path / "logs.sqlite3")
    uat_store = UatStore(tmp_path / "logs.sqlite3")
    observed_running = []

    def transport(_url, _key, _payload, _timeout):
        current = call_logs.list_records(limit=1)["items"][0]
        observed_running.append(current["request_status"])
        return {
            "status": 200, "headers": {"content-type": "application/json"},
            "body": (b'{"id":"RESP-MOCK","model":"model-a","choices":'
                     b'[{"message":{"content":"ok"},"finish_reason":"stop"}],'
                     b'"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}'),
            "elapsed_ms": 25,
        }

    settings = UatSettings(
        environment="uat", base_url="https://api-uat.weimeta.cn",
        api_key="-".join(("fixture", "key", "never", "sent")), enabled=True,
        allowed_hosts=("api-uat.weimeta.cn",), timeout_seconds=2,
        max_tokens=None, daily_request_limit=None, daily_budget_cny=None,
        max_request_cost_cny=.2,
    )
    payload = {
        "mode": "real_uat_execute",
        "confirmation": {"confirmed": True, "confirmation_text":
            "I understand this will call the Weimeta UAT API and may incur UAT cost."},
        "request": {"requested_model": "model-a", "channel_id": "channel-a",
            "messages": [{"role": "user", "content": "safe fixture"}],
            "stream": False, "max_tokens": 32},
        "measurement": {"plan_id": "LOCAL", "request_profile_id": "P01", "session_id": "TEST"},
        "shadow": {"run_before_execution": True, "strategy": "latency_first"},
    }
    catalog = {"model-a": {"confirmed_max_output_tokens": 64,
        "confirmed_channel_max_output_tokens": 64, "max_context_tokens": 1024,
        "max_input_tokens": 960, "evidence_source": "reviewed_test_contract",
        "evidence_version": "test-v1", "fresh_until": "2099-01-01T00:00:00+00:00"}}
    result = execute(
        payload, settings, uat_store,
        lambda _: {"catalog_version": "test", "catalog_sha256": "a" * 64,
                   "recommended_candidate": "channel-a", "fallback_order": []},
        transport, model_catalog=catalog,
        runtime_constraints={"remaining_budget_cny": "3.00"},
        call_logger=call_logs,
    )
    assert observed_running == ["RUNNING"]
    assert result["execution"]["status"] == "succeeded"
    row = call_logs.list_records(limit=1)["items"][0]
    assert row["request_status"] == "SUCCESS"
    assert row["response_id"] == "RESP-MOCK"
    assert row["decision_id"] == result["execution"]["decision_id"]
    assert row["channel_id"] == "channel-a"
    assert row["input_tokens"] == 3 and row["output_tokens"] == 4


def test_acceptance_fault_and_canary_evidence_round_trips_on_call_and_attempt(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    record_id = service.start_execution(
        request_id="REQ-EVIDENCE", decision_id="DEC-EVIDENCE",
        environment_id="china_uat", requested_model="model-a", stream=True,
        acceptance_run_id="AR-TEST", is_fault_injected=True, fault_id="FAULT-1",
        traffic_proposal_id="PROPOSAL-1", strategy_variant="canary-b",
    )
    service.finish_execution(
        record_id, status="FAILED", error_category="transport_error",
        error_source="injected_transport", fallback_used=True, output_started=False,
        cost_source="provider_usage", provider_log_id="PROVIDER-LOG-1",
    )
    service.record_attempt(
        request_id="REQ-EVIDENCE", attempt_number=2, requested_model="model-b",
        status="SUCCESS", acceptance_run_id="AR-TEST", fallback_used=True,
        output_started=True, traffic_proposal_id="PROPOSAL-1",
        strategy_variant="fallback-a", cost_source="provider_usage",
    )

    row = service.list_records(limit=1)["items"][0]
    assert row["acceptance_run_id"] == "AR-TEST"
    assert row["error_source"] == "injected_transport"
    assert row["is_fault_injected"] is True and row["fault_id"] == "FAULT-1"
    assert row["fallback_used"] is True and row["output_started"] is False
    assert row["traffic_proposal_id"] == "PROPOSAL-1"
    assert row["strategy_variant"] == "canary-b"
    assert row["cost_source"] == "provider_usage"
    attempts = service.attempts("REQ-EVIDENCE")
    assert attempts[0]["is_fault_injected"] is True
    assert attempts[1]["acceptance_run_id"] == "AR-TEST"
    assert attempts[1]["fallback_used"] is True
    assert service.acceptance_evidence_counts("AR-TEST") == {
        "historical": 0, "realtime": 1, "injected": 1,
        "provider_live": 0, "total": 1,
    }
