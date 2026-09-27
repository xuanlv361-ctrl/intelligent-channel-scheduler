from datetime import datetime, timedelta, timezone
from decimal import Decimal

from backend.call_log_service import CallLogService
from backend.observability_overview_service import ObservabilityOverviewService


def _record(service: CallLogService, request_id: str, *, traffic: str, status: str,
            stream: bool, error: str | None = None) -> None:
    record_id = service.start_execution(
        request_id=request_id, decision_id=f"DEC-{request_id}", environment_id="china_uat",
        requested_model="model-real", stream=stream, traffic_class=traffic,
    )
    service.finish_execution(
        record_id, status=status, actual_model="model-real", http_status=200 if status == "SUCCESS" else 504,
        error_category=error, total_latency_ms=120 if status == "SUCCESS" else 900,
        first_token_latency_ms=35 if stream else None, input_tokens=10,
        cached_input_tokens=2, output_tokens=5, cost_amount=Decimal("0.0012"), currency="CNY",
    )


def test_overview_aggregates_real_rows_and_excludes_probe_by_default(tmp_path):
    logs = CallLogService(tmp_path / "logs.sqlite3")
    _record(logs, "REQ-BUSINESS", traffic="business", status="SUCCESS", stream=True)
    _record(logs, "REQ-FAILED", traffic="business", status="FAILED", stream=False,
            error="network_timeout")
    _record(logs, "REQ-PROBE", traffic="probe", status="SUCCESS", stream=False)
    result = ObservabilityOverviewService(logs).overview(
        environment_id="china_uat", time_range="24h", traffic_class="business",
        circuits={"state_counts": {"OPEN": 1, "HALF_OPEN": 0}, "items": []},
    )
    assert result["kpis"]["requests"] == 2
    assert result["kpis"]["success_rate"] == .5
    assert result["kpis"]["p95_ms"] == 861.0
    assert result["kpis"]["ttft_p95_ms"] == 35
    assert result["kpis"]["tokens"] == 30
    assert result["error_distribution"] == [{"code": "network_timeout", "count": 1}]
    assert result["reliability"]["open_count"] == 1
    assert result["stream_distribution"]["stream"] == 1
    assert result["coverage"]["records"] == 2


def test_overview_filters_probe_source_and_custom_time(tmp_path):
    logs = CallLogService(tmp_path / "logs.sqlite3")
    _record(logs, "REQ-BUSINESS", traffic="business", status="SUCCESS", stream=False)
    _record(logs, "REQ-PROBE", traffic="probe", status="SUCCESS", stream=False)
    now = datetime.now(timezone.utc)
    result = ObservabilityOverviewService(logs).overview(
        environment_id="china_uat", time_range="custom",
        start=(now-timedelta(minutes=5)).isoformat(), end=(now+timedelta(minutes=1)).isoformat(),
        traffic_class="probe", source_type="realtime",
    )
    assert result["kpis"]["requests"] == 1
    assert result["recent_records"][0]["traffic_class"] == "probe"
    assert result["scope"]["source_type"] == "realtime"


def test_overview_empty_range_does_not_generate_demo_data(tmp_path):
    logs = CallLogService(tmp_path / "logs.sqlite3")
    result = ObservabilityOverviewService(logs).overview(time_range="5m")
    assert result["kpis"]["requests"] == 0
    assert result["request_series"] == []
    assert result["model_distribution"] == []
    assert result["kpis"]["actual_cost"] is None
