from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from backend.continuous_probe_execution_service import (
    ContinuousProbeExecutionError,
    ContinuousProbeExecutionService,
    ProbeExecutionConfig,
)
from backend.live_acceptance_service import LiveAcceptanceService
from backend.tenant_security import TenantScope


def created_run(path: Path) -> str:
    service = LiveAcceptanceService(path, TenantScope.local_development())
    created = service.create_probe_run({
        "environment_id": "china_uat",
        "git_commit": "test",
        "database_watermark": 0,
        "configuration": {
            "phases": [{"interval_seconds": 0.25}],
            "duration_seconds": 3,
        },
    })
    return str(created["probe_run_id"])


def wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition_not_reached")


def result(body: dict, *, injected: bool = False) -> dict:
    model = body["model"]
    return {
        "execution_status": "failed" if injected else "success",
        "request_id": f"REQ-{time.monotonic_ns()}",
        "decision_id": f"DEC-{time.monotonic_ns()}",
        "requested_model": model,
        "http_status": 503 if injected else 200,
        "total_latency_ms": 40,
        "cost_amount": None,
        "error_source": "uat_fault_injection" if injected else "provider_live",
        "is_fault_injected": injected,
        "error": {"error_category": "upstream_5xx"} if injected else None,
    }


def test_configuration_is_bounded_and_china_uat_only() -> None:
    with pytest.raises(ContinuousProbeExecutionError) as error:
        ProbeExecutionConfig.from_mapping({"environment_id": "production"})
    assert error.value.code == "probe_environment_not_allowed"
    with pytest.raises(ContinuousProbeExecutionError) as error:
        ProbeExecutionConfig.from_mapping({"duration_seconds": 0})
    assert error.value.code == "probe_duration_out_of_range"


def test_start_establishes_lease_pause_resume_and_stop(tmp_path: Path) -> None:
    path = tmp_path / "probe.sqlite3"
    run_id = created_run(path)

    def execute(body: dict) -> dict:
        time.sleep(0.45)
        return result(body)

    service = ContinuousProbeExecutionService(
        path,
        credential_provider=lambda: "runtime-only-test-credential",
        request_executor=execute,
        environment_guard=lambda _environment: True,
    )
    status = service.start(run_id, {
        "duration_seconds": 3,
        "interval_seconds": 0.25,
        "max_concurrency": 2,
        "max_requests": 20,
        "models": ["model-a", "model-b"],
    })
    assert status["status"] == "RUNNING"
    assert status["worker_owned"] is True
    assert status["heartbeat_at"]
    wait_for(lambda: service.status(run_id)["max_concurrency_observed"] == 2)
    paused = service.pause(run_id)
    assert paused["status"] == "PAUSED"
    wait_for(lambda: service.status(run_id)["completed_count"] >= 1)
    count = service.status(run_id)["sent_count"]
    time.sleep(0.4)
    assert service.status(run_id)["sent_count"] == count
    assert service.resume(run_id)["status"] == "RUNNING"
    wait_for(lambda: service.status(run_id)["sent_count"] > count)
    service.stop(run_id, wait_seconds=5)
    final = service.status(run_id)
    assert final["status"] == "STOPPED"
    assert final["max_concurrency_observed"] == 2

    # Runtime credentials are never persisted in configuration or events.
    with service.connect() as db:
        serialized = "\n".join(str(value) for row in db.execute(
            "SELECT details_json FROM continuous_probe_events") for value in row)
    assert "runtime-only-test-credential" not in serialized


def test_all_calls_are_probe_scoped_and_fault_provenance_is_preserved(tmp_path: Path) -> None:
    path = tmp_path / "probe.sqlite3"
    run_id = created_run(path)
    received: list[dict] = []

    def execute(body: dict) -> dict:
        received.append(body)
        return result(body, injected=len(received) == 1)

    service = ContinuousProbeExecutionService(
        path,
        credential_provider=lambda: "test",
        request_executor=execute,
        environment_guard=lambda _environment: True,
    )
    service.start(run_id, {
        "duration_seconds": 1,
        "interval_seconds": 0.25,
        "max_requests": 2,
        "models": ["model-a"],
    })
    wait_for(lambda: service.status(run_id)["status"] == "COMPLETED")
    assert len(received) == 2
    assert all(item["traffic_class"] == "probe" for item in received)
    assert all(item["probe_run_id"] == run_id for item in received)
    with service.connect() as db:
        sources = [row[0] for row in db.execute(
            "SELECT source_type FROM continuous_probe_events WHERE event_type='probe_result'")]
        summary = json.loads(db.execute(
            "SELECT summary_json FROM continuous_probe_runs WHERE probe_run_id=?", (run_id,)
        ).fetchone()[0])
    assert sources == ["uat_fault_injection", "provider_live"]
    assert summary["uat_injected_failures"] == 1
    assert summary["pending_provider_sync_count"] == 2


def test_reconcile_marks_stale_worker_interrupted_instead_of_rebilling(tmp_path: Path) -> None:
    path = tmp_path / "probe.sqlite3"
    run_id = created_run(path)
    service = ContinuousProbeExecutionService(
        path,
        credential_provider=lambda: "test",
        request_executor=lambda body: result(body),
        environment_guard=lambda _environment: True,
    )
    with service.connect() as db:
        db.execute("""UPDATE continuous_probe_runs SET status='RUNNING',
          execution_worker_id='OLD',execution_heartbeat_at='2020-01-01T00:00:00+00:00',
          execution_lease_expires_at='2020-01-01T00:00:00+00:00'
          WHERE probe_run_id=?""", (run_id,))
    reconciled = service.reconcile(stale_after_seconds=1)
    assert reconciled == {"interrupted_count": 1, "probe_run_ids": [run_id]}
    status = service.status(run_id)
    assert status["status"] == "FAILED"
    assert status["interruption_reason"] == "worker_restart_interrupted"


def test_failures_throttle_open_circuit_and_half_open_recover(tmp_path: Path) -> None:
    path = tmp_path / "probe.sqlite3"
    run_id = created_run(path)
    attempts = 0

    def execute(body: dict) -> dict:
        nonlocal attempts
        attempts += 1
        return result(body, injected=attempts <= 2)

    service = ContinuousProbeExecutionService(
        path,
        credential_provider=lambda: "test",
        request_executor=execute,
        environment_guard=lambda _environment: True,
    )
    service.start(run_id, {
        "duration_seconds": 5,
        "interval_seconds": 0.25,
        "max_concurrency": 1,
        "max_requests": 4,
        "models": ["model-a"],
        "cooldown_seconds": 1,
        "stop_thresholds": {
            "consecutive_failures": 2,
            "rate_429": 2,
            "rate_5xx": 2,
            "timeout_rate": 2,
            "success_rate": -1,
        },
    })
    wait_for(lambda: service.status(run_id)["status"] == "COMPLETED", timeout=8)
    with service.connect() as db:
        event_types = [row[0] for row in db.execute(
            "SELECT event_type FROM continuous_probe_events WHERE probe_run_id=?", (run_id,)
        )]
    assert "frequency_throttled" in event_types
    assert "circuit_opened" in event_types
    assert "half_open_probe" in event_types
    assert "circuit_recovered" in event_types


def test_missing_run_and_illegal_transitions_have_stable_codes(tmp_path: Path) -> None:
    path = tmp_path / "probe.sqlite3"
    run_id = created_run(path)
    service = ContinuousProbeExecutionService(
        path,
        credential_provider=lambda: "test",
        request_executor=lambda body: result(body),
        environment_guard=lambda _environment: True,
    )
    with pytest.raises(ContinuousProbeExecutionError) as error:
        service.status("PRB-MISSING")
    assert error.value.code == "probe_run_not_found"
    with pytest.raises(ContinuousProbeExecutionError) as error:
        service.pause(run_id)
    assert error.value.code == "probe_pause_invalid_state"
