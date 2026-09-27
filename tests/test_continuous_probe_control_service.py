from __future__ import annotations

import json
import sqlite3

import pytest

from backend.live_acceptance_service import LiveAcceptanceService


def _service(tmp_path) -> LiveAcceptanceService:
    service = LiveAcceptanceService(tmp_path / "probe.sqlite3")
    with service.connect() as db:
        db.execute("""CREATE TABLE standardized_call_logs(
          cursor_id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
          occurred_at TEXT NOT NULL, request_id TEXT, local_request_id TEXT,
          provider_request_id TEXT, provider_response_id TEXT, provider_trace_id TEXT,
          response_id TEXT, decision_id TEXT, requested_model TEXT, actual_model TEXT,
          provider TEXT, stream INTEGER NOT NULL, request_status TEXT NOT NULL,
          http_status INTEGER, error_category TEXT, total_latency_ms REAL,
          first_token_latency_ms REAL, input_tokens INTEGER, cached_input_tokens INTEGER,
          output_tokens INTEGER, cost_amount TEXT, currency TEXT, cost_status TEXT,
          cost_type TEXT, provider_cost_amount_exact TEXT, fallback_used INTEGER,
          is_fault_injected INTEGER, error_source TEXT, fault_id TEXT,
          traffic_class TEXT, probe_run_id TEXT, duplicate_status TEXT DEFAULT 'canonical',
          tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL)""")
    return service


def _create(service: LiveAcceptanceService, name: str = "五分钟探测") -> dict:
    return service.create_probe_run({
        "task_name": name,
        "environment_id": "china_uat",
        "git_commit": "abc123",
        "database_watermark": 17,
        "created_by": "tester",
        "configuration": {
            "duration_seconds": 300,
            "request_interval_seconds": 2.5,
            "max_concurrency": 2,
            "models": ["kimi-k3", "glm-5.2"],
        },
    })


def test_canonical_create_generates_unique_id_and_scrubs_secrets(tmp_path):
    service = _service(tmp_path)
    payload = {
        "task_name": "安全探测", "environment_id": "china_uat",
        "configuration": {"duration_seconds": 60, "api_key": "must-not-persist",
                          "headers": {"Authorization": "must-not-persist", "x-safe": "ok"}},
    }
    first = service.create_probe_run(payload)
    second = service.create_probe_run(payload)
    assert first["probe_run_id"].startswith("PRB-")
    assert len(first["probe_run_id"]) == 36
    assert first["probe_run_id"] != second["probe_run_id"]
    assert first["status"] == "CREATED"
    assert first["configuration"] == {"duration_seconds": 60, "headers": {"x-safe": "ok"}}
    with pytest.raises(ValueError, match="probe_run_id_is_server_generated"):
        service.create_probe_run({**payload, "probe_run_id": "PRB-CALLER"})


def test_list_query_clone_and_tenant_scoped_persistence(tmp_path):
    service = _service(tmp_path)
    original = _create(service, "真实模型巡检")
    _create(service, "其他任务")
    listed = service.list_probe_runs(query="巡检", limit=10)
    assert listed["total"] == 1
    assert listed["items"][0]["probe_run_id"] == original["probe_run_id"]
    clone = service.clone_probe_run(original["probe_run_id"])
    assert clone["status"] == "CREATED"
    assert clone["cloned_from_probe_run_id"] == original["probe_run_id"]
    assert clone["configuration"] == original["configuration"]


def test_lifecycle_transitions_are_validated_and_revision_safe(tmp_path):
    service = _service(tmp_path)
    run = _create(service)
    started = service.transition_probe_run(
        run["probe_run_id"], "start", expected_revision=run["revision"], operator_id="tester"
    )
    assert started["status"] == "RUNNING"
    with pytest.raises(RuntimeError, match="probe_revision_conflict"):
        service.transition_probe_run(run["probe_run_id"], "pause", expected_revision=1)
    paused = service.transition_probe_run(run["probe_run_id"], "pause")
    assert paused["status"] == "PAUSED"
    resumed = service.transition_probe_run(run["probe_run_id"], "resume")
    assert resumed["status"] == "RUNNING"
    stopped = service.transition_probe_run(run["probe_run_id"], "stop", reason="用户停止")
    assert stopped["status"] == "STOPPED"
    assert stopped["stop_requested"] == 1
    with pytest.raises(RuntimeError, match="invalid_probe_state_transition"):
        service.transition_probe_run(run["probe_run_id"], "resume")


def test_metrics_and_requests_use_only_exact_probe_traffic(tmp_path):
    service = _service(tmp_path)
    run = _create(service)
    run_id = run["probe_run_id"]
    common = {
        "occurred_at": "2026-08-07T01:02:03+00:00", "request_id": "REQ-1",
        "local_request_id": "REQ-1", "provider_request_id": "provider-1",
        "provider_response_id": "resp-1", "provider_trace_id": "trace-1",
        "response_id": "resp-1", "decision_id": "DEC-1", "requested_model": "kimi-k3",
        "actual_model": "kimi-k3", "provider": "uat", "stream": 0,
        "request_status": "success", "http_status": 200, "error_category": None,
        "total_latency_ms": 120, "first_token_latency_ms": None, "input_tokens": 5,
        "cached_input_tokens": 1, "output_tokens": 2, "cost_amount": None,
        "currency": "CNY", "cost_status": "actual_provider_cost",
        "cost_type": "provider_actual", "provider_cost_amount_exact": "0.012345",
        "fallback_used": 0, "is_fault_injected": 0, "error_source": "provider_live",
        "fault_id": None, "traffic_class": "probe", "probe_run_id": run_id,
        "duplicate_status": "canonical", "tenant_id": service.scope.tenant_id,
        "workspace_id": service.scope.workspace_id,
    }
    with service.connect() as db:
        for index, overrides in enumerate((
            {},
            {"request_id": "REQ-2", "provider_request_id": "provider-2",
             "request_status": "failed", "http_status": 503, "total_latency_ms": 300,
             "cost_status": "pending_provider_sync", "cost_type": None,
             "provider_cost_amount_exact": None, "is_fault_injected": 1,
             "error_source": "uat_fault_injection", "fault_id": "FLT-1"},
            {"request_id": "REQ-BUSINESS", "traffic_class": "business"},
            {"request_id": "REQ-OTHER", "probe_run_id": "PRB-OTHER"},
        ), start=1):
            row = {**common, **overrides, "record_id": f"REC-{index}"}
            columns = list(row)
            db.execute(
                f"INSERT INTO standardized_call_logs({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})", tuple(row.values())
            )
    metrics = service.probe_metrics(run_id)
    assert metrics["request_count"] == 2
    assert metrics["natural_success_rate"] == 1
    assert metrics["comprehensive_success_rate"] == 0.5
    assert metrics["p50_ms"] == 210
    assert metrics["actual_provider_cost"] == "0.012345"
    assert metrics["actual_cost_synced_count"] == 1
    assert metrics["pending_provider_sync_count"] == 1
    requests = service.probe_requests(run_id)
    assert requests["total"] == 2
    assert {item["request_id"] for item in requests["items"]} == {"REQ-1", "REQ-2"}
    assert requests["items"][0]["actual_provider_cost"] == "0.012345"


def test_event_pagination_does_not_expose_secret_fields(tmp_path):
    service = _service(tmp_path)
    run = _create(service)
    service.probe_event(run["probe_run_id"], "probe_notice", "local_control_event", {
        "message": "ok", "Authorization": "hidden", "nested": {"api_key": "hidden"},
    })
    first = service.probe_events(run["probe_run_id"], limit=1)
    second = service.probe_events(
        run["probe_run_id"], after_event_id=first["next_after_event_id"], limit=10
    )
    assert first["items"]
    assert second["items"][0]["details"] == {"message": "ok", "nested": {}}
    assert "hidden" not in json.dumps(second, ensure_ascii=False)
