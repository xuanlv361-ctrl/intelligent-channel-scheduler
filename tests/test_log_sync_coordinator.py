from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3

from backend.log_sync_coordinator import LogSyncCoordinator


NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


class FakeRealtime:
    def __init__(self, path, jobs=None):
        self.path = str(path)
        self.jobs = list(jobs or [])
        self.stopped = []
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE realtime_log_records(
              tenant_id TEXT,workspace_id TEXT,environment_id TEXT);
            CREATE TABLE realtime_log_correlations(
              tenant_id TEXT,workspace_id TEXT,environment_id TEXT,state TEXT);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def list_jobs(self, environment_id, principal=None):
        return list(self.jobs)

    def cursor(self, environment_id, principal=None):
        return {"cursor_value": "2026-08-06T11:00:00+00:00"}

    def stop(self, job_id, principal=None):
        self.stopped.append(job_id)
        for job in self.jobs:
            if job["sync_job_id"] == job_id:
                job["state"] = "stopped"
        return next(job for job in self.jobs if job["sync_job_id"] == job_id)


class FakePersistent:
    def __init__(self):
        self.reconciled = []

    def reconcile_leases(self, reason):
        self.reconciled.append(reason)


class FakeShadow:
    def __init__(self, realtime, results=None, rows=None):
        self.realtime = realtime
        self.persistent = FakePersistent()
        self.results = list(results or [{"status": "fresh", "created": False}])
        self.rows = list(rows or [])

    def ensure_sync(self, **kwargs):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def _rows(self, principal=None):
        return list(self.rows)


def build(tmp_path, *, jobs=None, results=None, recovery=None, rows=None):
    realtime = FakeRealtime(tmp_path / "coordinator.sqlite3", jobs=jobs)
    shadow = FakeShadow(realtime, results=results, rows=rows)
    coordinator = LogSyncCoordinator(
        realtime, shadow, recover_session=recovery, now=lambda: NOW,
        interval_seconds=60)
    return coordinator, realtime, shadow


def test_concurrent_ensure_is_idempotently_coalesced(tmp_path):
    coordinator, _, _ = build(tmp_path)
    coordinator._lock.acquire()
    try:
        result = coordinator.ensure(trigger="second_page")
    finally:
        coordinator._lock.release()
    assert result["already_running"] is True
    assert result["status"] == "already_running"


def test_stale_temporary_job_is_closed_before_new_ensure(tmp_path):
    coordinator, realtime, _ = build(tmp_path, jobs=[{
        "sync_job_id": "LSYNC-STALE", "state": "syncing",
        "last_poll_at": (NOW - timedelta(hours=2)).isoformat(),
        "started_at": (NOW - timedelta(hours=2)).isoformat(),
    }])
    result = coordinator.ensure(trigger="backend_startup")
    assert realtime.stopped == ["LSYNC-STALE"]
    assert result["status"] == "fresh"
    assert any(item["details"]["sync_job_id"] == "LSYNC-STALE"
               for item in coordinator.events()["items"]
               if item["event_type"] == "stale_log_sync_job_recovered")


def test_fresh_active_job_is_not_stopped(tmp_path):
    coordinator, realtime, _ = build(tmp_path, jobs=[{
        "sync_job_id": "LSYNC-LIVE", "state": "syncing",
        "last_poll_at": (NOW - timedelta(seconds=30)).isoformat(),
        "started_at": NOW.isoformat(),
    }], results=[{"status": "already_running", "created": False,
                 "job": {"sync_job_id": "LSYNC-LIVE", "state": "syncing"}}])
    result = coordinator.ensure()
    assert not realtime.stopped
    assert result["already_running"] is True


def test_status_separates_synced_linked_and_pending(tmp_path):
    coordinator, realtime, _ = build(
        tmp_path, rows=[{"shadow_recommendation": {"model": "m1"}}])
    with realtime.connect() as db:
        for _ in range(3):
            db.execute("INSERT INTO realtime_log_records VALUES(?,?,?)",
                       ("tenant_local_dev_v1", "workspace_local_dev_v1", "china_uat"))
        db.execute("INSERT INTO realtime_log_correlations VALUES(?,?,?,?)", (
            "tenant_local_dev_v1", "workspace_local_dev_v1", "china_uat",
            "exact_provider_request_id"))
        db.execute("INSERT INTO realtime_log_correlations VALUES(?,?,?,?)", (
            "tenant_local_dev_v1", "workspace_local_dev_v1", "china_uat", "unmatched"))
    status = coordinator.status()
    assert status["synced_logs"] == 3
    assert status["linked"] == 1
    assert status["pending_link"] == 2
    assert status["shadow_results"] == 1
    assert status["read_only"] is True
    assert status["uat_execution_switch_required"] is False


def test_authentication_recovery_retries_the_original_sync(tmp_path):
    calls = []
    coordinator, _, _ = build(
        tmp_path,
        results=[{"status": "authentication_required", "reason": "expired"},
                 {"status": "fresh", "created": False}],
        recovery=lambda: calls.append("recovered") or {
            "authentication_status": "authenticated"})
    result = coordinator.ensure()
    assert calls == ["recovered"]
    assert result["status"] == "fresh"
    assert coordinator.status()["consecutive_failures"] == 0


def test_failed_recovery_uses_backoff_without_marking_human_block(tmp_path):
    attempts = []

    def fail():
        attempts.append(1)
        raise RuntimeError("authentication_check_failed")

    coordinator, _, _ = build(
        tmp_path, results=[{"status": "authentication_required", "reason": "expired"}],
        recovery=fail)
    first = coordinator.ensure()
    second = coordinator.ensure()
    status = coordinator.status()
    assert first["status"] == second["status"] == "delayed"
    assert len(attempts) == 1
    assert status["requires_human"] is False
    assert status["consecutive_failures"] == 1
    assert status["backoff_until"] is not None


def test_mfa_failure_requires_human_action(tmp_path):
    def fail():
        raise RuntimeError("mfa_required")

    coordinator, _, _ = build(
        tmp_path, results=[{"status": "authentication_required", "reason": "expired"}],
        recovery=fail)
    result = coordinator.ensure()
    assert result["requires_human"] is True
    assert coordinator.status()["login_status"] == "human_action_required"

