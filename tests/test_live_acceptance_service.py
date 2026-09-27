from __future__ import annotations

from pathlib import Path

from backend.call_log_service import CallLogService
from backend.live_acceptance_service import LiveAcceptanceService


def test_live_acceptance_runs_persist_and_summarize(tmp_path: Path):
    path = tmp_path / "acceptance.sqlite3"
    service = LiveAcceptanceService(path)
    run_id = "SER-TEST"
    service.create_strategy_run({
        "strategy_effect_run_id": run_id,
        "environment_id": "china_uat",
        "git_commit": "0" * 40,
        "database_watermark": 0,
        "configuration_version": "cfg-v1",
        "price_version": "price-v1",
        "model_catalog_version": "catalog-v1",
        "log_sync_time": None,
        "uat_environment_enabled": True,
        "planned_requests": 1,
    })
    service.record_strategy_result(run_id, {
        "request_case_id": "case-1", "repetition": 1,
        "strategy": "current_actual", "strategy_version": "cfg-v1",
        "configuration_version": "cfg-v1", "metric_snapshot_id": "metric-v1",
        "selected_model": "deepseek-v4-flash", "candidates_json": "[]",
        "exclusions_json": "[]", "scores_json": "{}", "request_id": "REQ-1",
        "response_id": "RESP-1", "decision_id": "DEC-1",
        "actual_model": "deepseek-v4-flash", "http_status": 200,
        "success": 1, "first_token_latency_ms": None, "total_latency_ms": 120,
        "input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 5,
        "total_tokens": 15, "actual_provider_cost": None,
        "estimated_versioned_price": "0.001", "cost_status": "pending_provider_sync",
        "price_version": "price-v1", "retry_count": 0, "fallback_used": 0,
        "output_complete": 1, "assertion_passed": 1, "assertion_json": "{}",
        "error_category": None, "created_at": "2026-08-06T00:00:00+00:00",
    })
    summary = service.summarize_strategy(run_id)
    assert summary["completed_requests"] == 1
    assert summary["strategies"]["current_actual"]["request_count"] == 1
    assert LiveAcceptanceService(path).strategy_run(run_id)["items"][0]["request_id"] == "REQ-1"


def test_probe_traffic_is_excluded_from_business_analytics(tmp_path: Path):
    service = CallLogService(tmp_path / "logs.sqlite3")
    for request_id, traffic_class in (("REQ-BUSINESS", "business"), ("REQ-PROBE", "probe")):
        record = service.start_execution(
            request_id=request_id, decision_id=f"DEC-{request_id}",
            environment_id="china_uat", requested_model="deepseek-v4-flash",
            stream=False, traffic_class=traffic_class,
            probe_run_id="PRB-1" if traffic_class == "probe" else None,
        )
        service.finish_execution(
            record, status="SUCCESS", actual_model="deepseek-v4-flash",
            http_status=200, total_latency_ms=100, input_tokens=1, output_tokens=1,
        )
    assert service.analytics()["request_count"] == 1
    assert service.analytics(traffic_class="probe")["request_count"] == 1
    assert service.analytics(traffic_class="all")["request_count"] == 2


def test_strategy_actual_cost_is_reconciled_only_from_exact_provider_match(tmp_path: Path):
    path = tmp_path / "acceptance.sqlite3"
    service = LiveAcceptanceService(path)
    service.create_strategy_run({
        "strategy_effect_run_id": "SER-COST", "environment_id": "china_uat",
        "git_commit": "0" * 40, "database_watermark": 0,
        "configuration_version": "cfg-v1", "price_version": "price-v1",
        "model_catalog_version": "catalog-v1", "log_sync_time": None,
        "uat_environment_enabled": True, "planned_requests": 1})
    service.record_strategy_result("SER-COST", {
        "request_case_id": "case-1", "repetition": 1, "strategy": "cost_first",
        "strategy_version": "cost-v1", "configuration_version": "cfg-v1",
        "metric_snapshot_id": "metric-v1", "selected_model": "model-a",
        "candidates_json": "[]", "exclusions_json": "[]", "scores_json": "{}",
        "request_id": "REQ-COST", "response_id": "RESP-1", "decision_id": "DEC-1",
        "actual_model": "model-a", "http_status": 200, "success": 1,
        "first_token_latency_ms": None, "total_latency_ms": 100,
        "input_tokens": 2, "cached_input_tokens": 0, "output_tokens": 3,
        "total_tokens": 5, "actual_provider_cost": None,
        "estimated_versioned_price": "0.0001", "cost_status": "pending_provider_sync",
        "price_version": "price-v1", "retry_count": 0, "fallback_used": 0,
        "output_complete": 1, "assertion_passed": 1, "assertion_json": "{}",
        "error_category": None, "created_at": "2026-08-06T00:00:00+00:00"})
    logs = CallLogService(path)
    record = logs.start_execution(request_id="REQ-COST", decision_id="DEC-1",
        environment_id="china_uat", requested_model="model-a", stream=False)
    logs.finish_execution(record, status="SUCCESS", actual_model="model-a", http_status=200)
    assert service.reconcile_strategy_actual_costs()["updated_count"] == 0
    with logs.connect() as db:
        db.execute("""UPDATE standardized_call_logs SET match_confidence='exact',
          cost_type='provider_actual',provider_cost_amount_exact='0.000073',
          cost_amount='0.000073' WHERE record_id=?""", (record,))
    result = service.reconcile_strategy_actual_costs()
    assert result["updated_count"] == 1
    row = service.strategy_run("SER-COST")["items"][0]
    assert row["actual_provider_cost"] == "0.000073"
    assert row["cost_status"] == "actual_provider_cost"
