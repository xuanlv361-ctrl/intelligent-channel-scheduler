from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.collector_run_service import CollectorRunError, CollectorRunService


def create_run(service: CollectorRunService, session: str = "session-a"):
    return service.create(
        environment_id="china_uat", date_from="2026-07-28", date_to="2026-07-28",
        maximum_pages=1, maximum_records=20, page_delay_ms=1500,
        operator_confirmation=True, browser_session=session,
    )


def test_additive_run_state_machine_and_sanitized_persistence(tmp_path):
    service = CollectorRunService(tmp_path / "runs.sqlite3")
    run = create_run(service)
    assert run["status"] == "created"
    assert not {"password", "cookie", "authorization", "storage_state"} & set(run)
    service.transition(run["run_id"], "launching_browser", worker_pid=42)
    service.transition(run["run_id"], "awaiting_manual_login",
                       current_safe_url="https://uat.weimeta.cn/console/billing/logs")
    service.transition(run["run_id"], "login_confirmed")
    service.transition(run["run_id"], "collecting")
    with pytest.raises(CollectorRunError, match="collector_invalid_state_transition"):
        service.transition(run["run_id"], "completed")


def test_limits_environment_and_duplicate_active_run_are_enforced(tmp_path):
    service = CollectorRunService(tmp_path / "runs.sqlite3")
    create_run(service)
    with pytest.raises(CollectorRunError, match="collector_already_running"):
        create_run(service, "session-b")
    with pytest.raises(CollectorRunError, match="collector_environment_required"):
        service.create(environment_id="all", date_from="2026-07-28", date_to="2026-07-28",
                       maximum_pages=1, maximum_records=1, page_delay_ms=1500,
                       operator_confirmation=True, browser_session="other")
    with pytest.raises(CollectorRunError, match="collector_limits_invalid"):
        CollectorRunService(tmp_path / "other.sqlite3").create(
            environment_id="china_uat", date_from="2026-07-28", date_to="2026-07-28",
            maximum_pages=11, maximum_records=20, page_delay_ms=1500,
            operator_confirmation=True, browser_session="other")


def test_heartbeat_loss_and_stop_are_idempotent(tmp_path):
    service = CollectorRunService(tmp_path / "runs.sqlite3")
    run = create_run(service)
    service.transition(run["run_id"], "launching_browser")
    old = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    service.update(run["run_id"], last_heartbeat_at=old)
    assert service.heartbeat_lost(run["run_id"], 5)
    assert service.get(run["run_id"])["status"] == "heartbeat_lost"
    assert service.request_stop(run["run_id"])["status"] == "heartbeat_lost"


def test_preview_hash_is_persisted_without_raw_credentials(tmp_path):
    service = CollectorRunService(tmp_path / "runs.sqlite3")
    run = create_run(service)
    for state in ("launching_browser", "awaiting_manual_login", "login_confirmed", "collecting"):
        service.transition(run["run_id"], state)
    preview = {"collection_id": "COL-1", "payload_sha256": "a" * 64, "record_count": 0,
               "captured_count": 0, "duplicate_count": 0, "rejected_count": 0,
               "warnings": [], "records": []}
    service.save_preview(run["run_id"], preview)
    assert service.preview(run["run_id"]) == preview
    columns = {row[1] for row in service.connect().execute("PRAGMA table_info(collector_runs)")}
    assert not {"api_key", "password", "cookie", "storage_state", "request_headers"} & columns

