from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3

from backend.shadow_sync_service import EXACT_CORRELATIONS, ShadowSyncService
from backend.tenant_security import TenantScope


NOW = datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc)


class FakeRealtime:
    def __init__(self, jobs=None, cursor=None):
        self.jobs = list(jobs or [])
        self.saved_cursor = cursor
        self.updated = []
        self.events = []

    def list_jobs(self, environment_id, principal=None):
        assert environment_id == "china_uat"
        return list(self.jobs)

    def cursor(self, environment_id, principal=None):
        return self.saved_cursor

    def update_job(self, job_id, **updates):
        self.updated.append((job_id, updates))
        return {"sync_job_id": job_id, "state": "created", **updates}

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class FakePersistent:
    def __init__(self, session=None):
        self.session = session
        self.created = []

    def status(self, principal=None):
        return {"session": self.session, "active_job": None}

    def create_job(self, body, principal=None):
        self.created.append(body)
        return {"sync_job_id": "LSYNC-AUTO", "state": "created"}


def active_session():
    return {
        "persistent_session_id": "PSS-1", "state": "active",
        "authentication_status": "authenticated", "usage_state": "available",
    }


def test_ensure_sync_is_idempotent_when_active_job_exists():
    realtime = FakeRealtime([{"sync_job_id": "LSYNC-1", "state": "synchronizing"}])
    persistent = FakePersistent(active_session())
    launched = []
    service = ShadowSyncService(
        realtime, persistent, launched.append, now=lambda: NOW)
    result = service.ensure_sync()
    assert result["status"] == "already_running"
    assert not persistent.created and not launched


def test_ensure_sync_respects_freshness_without_network_launch():
    realtime = FakeRealtime([{
        "sync_job_id": "LSYNC-1", "state": "completed",
        "last_successful_read_at": "2026-08-05T02:59:30+00:00",
    }])
    persistent = FakePersistent(active_session())
    launched = []
    service = ShadowSyncService(
        realtime, persistent, launched.append, freshness_seconds=60,
        now=lambda: NOW)
    result = service.ensure_sync()
    assert result["status"] == "fresh"
    assert not persistent.created and not launched


def test_ensure_sync_starts_one_bounded_incremental_read():
    realtime = FakeRealtime([], None)
    persistent = FakePersistent(active_session())
    launched = []
    service = ShadowSyncService(
        realtime, persistent, launched.append, now=lambda: NOW)
    result = service.ensure_sync(force=True, trigger="manual_sync")
    assert result["status"] == "started" and result["created"] is True
    assert launched == ["LSYNC-AUTO"]
    body = persistent.created[0]
    assert body["periodic_polling"] is False
    assert body["maximum_http_reads"] == 50
    assert body["maximum_pages"] == 50
    assert body["explicit_confirmation"] is True


def test_real_provider_identifier_states_are_exact_and_legacy_local_ids_are_not():
    assert "exact_provider_request_id" in EXACT_CORRELATIONS
    assert "exact_provider_response_id" in EXACT_CORRELATIONS
    assert "exact_provider_trace_id" in EXACT_CORRELATIONS
    assert "exact_client_correlation_id" in EXACT_CORRELATIONS
    assert "exact_request_id" not in EXACT_CORRELATIONS
    assert "exact_decision_id" not in EXACT_CORRELATIONS


def test_ensure_sync_uses_last_nonempty_watermark_after_empty_batch():
    realtime = FakeRealtime([
        {"sync_job_id": "LSYNC-EMPTY", "state": "completed", "watermark_utc": None},
        {"sync_job_id": "LSYNC-PREV", "state": "completed",
         "watermark_utc": "2026-08-05T02:50:00+00:00"},
    ])
    persistent = FakePersistent(active_session())
    service = ShadowSyncService(
        realtime, persistent, lambda job_id: 101, overlap_seconds=120,
        now=lambda: NOW)
    result = service.ensure_sync(force=True)
    assert result["status"] == "started"
    assert persistent.created[0]["date_from"] == "2026-08-05T02:48:00+00:00"


def test_missing_authentication_is_reported_without_job_or_network():
    realtime = FakeRealtime()
    persistent = FakePersistent(None)
    launched = []
    result = ShadowSyncService(
        realtime, persistent, launched.append, now=lambda: NOW).ensure_sync()
    assert result == {
        "status": "not_configured", "created": False,
        "trigger": "frontend_init", "reason": "persistent_session_missing",
    }
    assert not persistent.created and not launched


class ScopedRealtime:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE realtime_log_records(
              record_id TEXT PRIMARY KEY,environment_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            CREATE TABLE realtime_log_correlations(
              correlation_id TEXT PRIMARY KEY,execution_id TEXT,record_id TEXT NOT NULL,
              environment_id TEXT NOT NULL,state TEXT NOT NULL,method TEXT,created_at TEXT NOT NULL,
              details_json TEXT NOT NULL,tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
              UNIQUE(environment_id,record_id));
            CREATE TABLE uat_executions(
              execution_id TEXT PRIMARY KEY,environment_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
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

    def _scope(self, principal, permission):
        assert permission == "evidence.import"
        return TenantScope.local_development()


def test_manual_confirmation_is_scoped_audited_and_idempotent(tmp_path):
    realtime = ScopedRealtime(tmp_path / "shadow.sqlite3")
    scope = TenantScope.local_development()
    with realtime.connect() as db:
        db.execute("INSERT INTO realtime_log_records VALUES(?,?,?,?)",
                   ("LOG-1", "china_uat", *scope.sql_parameters()))
        db.execute("INSERT INTO uat_executions VALUES(?,?,?,?)",
                   ("EXEC-1", "china_uat", *scope.sql_parameters()))
    service = ShadowSyncService(realtime, FakePersistent(), lambda _: 0, now=lambda: NOW)
    result = service.confirm_correlation("LOG-1", "EXEC-1")
    assert result["correlation_status"] == "manually_confirmed"
    assert service.confirm_correlation("LOG-1", "EXEC-1")["status"] == "already_confirmed"
    with realtime.connect() as db:
        row = db.execute("SELECT * FROM realtime_log_correlations").fetchone()
        assert row["tenant_id"] == scope.tenant_id
        assert row["workspace_id"] == scope.workspace_id
        assert '"previous_state": "unmatched"' in row["details_json"]


def test_manual_confirmation_rejects_unknown_or_cross_scope_execution(tmp_path):
    realtime = ScopedRealtime(tmp_path / "shadow.sqlite3")
    scope = TenantScope.local_development()
    with realtime.connect() as db:
        db.execute("INSERT INTO realtime_log_records VALUES(?,?,?,?)",
                   ("LOG-1", "china_uat", *scope.sql_parameters()))
        db.execute("INSERT INTO uat_executions VALUES(?,?,?,?)",
                   ("EXEC-OTHER", "china_uat", "other", "workspace"))
    service = ShadowSyncService(realtime, FakePersistent(), lambda _: 0, now=lambda: NOW)
    try:
        service.confirm_correlation("LOG-1", "EXEC-OTHER")
    except LookupError as exc:
        assert str(exc) == "shadow_execution_not_found"
    else:
        raise AssertionError("cross-scope execution must fail closed")
