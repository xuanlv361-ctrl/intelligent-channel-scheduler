from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.realtime_log_sync_service import ACTIVE_STATES, RealtimeLogSyncService
from backend.security.principal import PrincipalContext
from backend.shadow_sync_service import EXACT_CORRELATIONS, ShadowSyncService
from backend.tenant_security import TenantScope


class LogSyncCoordinator:
    """Single-process, persistent coordinator for every automatic sync trigger."""

    def __init__(
        self,
        realtime: RealtimeLogSyncService,
        shadow: ShadowSyncService,
        *,
        recover_session: Callable[[], dict[str, Any]] | None = None,
        now: Callable[[], datetime] | None = None,
        interval_seconds: int = 60,
    ) -> None:
        self.realtime = realtime
        self.shadow = shadow
        self.recover_session = recover_session
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.interval_seconds = max(15, int(interval_seconds))
        self._lock = threading.Lock()
        self._ensure_schema()

    @staticmethod
    def _scope(principal: PrincipalContext | None) -> TenantScope:
        if isinstance(principal, PrincipalContext) and principal.is_verified:
            return TenantScope(principal.tenant_id, principal.workspace_id)
        return TenantScope.local_development()

    def _ensure_schema(self) -> None:
        with self.realtime.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS log_sync_coordinator_state(
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              environment_id TEXT NOT NULL, status TEXT NOT NULL,
              login_status TEXT NOT NULL, last_check_at TEXT,
              last_success_at TEXT, last_job_id TEXT, watermark TEXT,
              next_schedule_at TEXT, last_error TEXT,
              requires_human INTEGER NOT NULL DEFAULT 0,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              backoff_until TEXT, revision INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(tenant_id,workspace_id,environment_id));
            CREATE TABLE IF NOT EXISTS log_sync_coordinator_events(
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              environment_id TEXT NOT NULL, event_type TEXT NOT NULL,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL);
            """)
            scope = TenantScope.local_development()
            db.execute("""INSERT OR IGNORE INTO log_sync_coordinator_state(
              tenant_id,workspace_id,environment_id,status,login_status,revision)
              VALUES(?,?,'china_uat','idle','unknown',0)""", scope.sql_parameters())

    def _event(self, event_type: str, details: dict[str, Any], scope: TenantScope) -> None:
        with self.realtime.connect() as db:
            db.execute("""INSERT INTO log_sync_coordinator_events(
              tenant_id,workspace_id,environment_id,event_type,created_at,details_json)
              VALUES(?,?,'china_uat',?,?,?)""", (
                *scope.sql_parameters(), event_type,
                self.now().astimezone(timezone.utc).isoformat(),
                json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)))

    def _update(self, scope: TenantScope, **updates: Any) -> None:
        if not updates:
            return
        with self.realtime.connect() as db:
            db.execute("UPDATE log_sync_coordinator_state SET " +
                       ",".join(f"{key}=?" for key in updates) +
                       ",revision=revision+1 WHERE tenant_id=? AND workspace_id=? "
                       "AND environment_id='china_uat'", (
                           *updates.values(), *scope.sql_parameters()))

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                timezone.utc)
        except (TypeError, ValueError):
            return None

    def _stop_stale_jobs(
        self, now: datetime, principal: PrincipalContext | None,
        scope: TenantScope,
    ) -> None:
        """Release jobs left active by a dead browser/worker process.

        Lease reconciliation only covers persistent-session jobs. Historical
        temporary browser jobs have no lease, so they need an explicit age
        fence or they can block every future idempotent ensure forever.
        """
        stale_after = max(120, self.interval_seconds * 3)
        for job in self.realtime.list_jobs("china_uat", principal=principal):
            if job.get("state") not in ACTIVE_STATES:
                continue
            heartbeat = self._parse_time(
                job.get("last_poll_at") or job.get("started_at") or job.get("created_at"))
            if heartbeat and (now - heartbeat).total_seconds() <= stale_after:
                continue
            self.realtime.stop(job["sync_job_id"], principal=principal)
            self._event("stale_log_sync_job_recovered", {
                "sync_job_id": job["sync_job_id"],
                "last_activity_at": heartbeat.isoformat() if heartbeat else None,
                "stale_after_seconds": stale_after,
            }, scope)

    def ensure(
        self, *, force: bool = False, trigger: str = "frontend_ensure",
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(principal)
        if not self._lock.acquire(blocking=False):
            status = self.status(principal=principal)
            return {"job_id": status.get("job_id"), "status": "already_running",
                    "already_running": True, "watermark": status.get("watermark"),
                    "started_at": status.get("started_at")}
        try:
            now = self.now().astimezone(timezone.utc)
            self._update(scope, status="checking", last_check_at=now.isoformat(),
                         next_schedule_at=(now + timedelta(
                             seconds=self.interval_seconds)).isoformat())
            # Startup may observe a process that died before its lease was released.
            self.shadow.persistent.reconcile_leases("coordinator_ensure")
            self._stop_stale_jobs(now, principal, scope)
            result = self.shadow.ensure_sync(
                force=force, trigger=trigger, principal=principal)
            if result.get("status") in {"authentication_required", "not_configured"}:
                current = self.status(principal=principal)
                backoff_until = self._parse_time(current.get("backoff_until"))
                if backoff_until and backoff_until > now:
                    return {"job_id": None, "status": "delayed",
                            "already_running": False,
                            "watermark": current.get("watermark"),
                            "started_at": now.isoformat(),
                            "requires_human": current.get("requires_human", False),
                            "retry_after": backoff_until.isoformat()}
                self._update(scope, status="recovering_login",
                             login_status="automatic_recovery_in_progress",
                             last_error=result.get("reason"))
                if self.recover_session is not None:
                    try:
                        recovery = self.recover_session()
                        self._event("provider_session_recovered", {
                            "trigger": trigger,
                            "authentication_status": recovery.get("authentication_status"),
                        }, scope)
                        result = self.shadow.ensure_sync(
                            force=True, trigger="session_recovery_compensation",
                            principal=principal)
                    except Exception as exc:  # only safe error codes are persisted
                        code = str(exc)
                        hard = code in {"captcha_required", "mfa_required",
                                        "account_locked", "permission_denied"}
                        failures = int(current.get("consecutive_failures") or 0) + 1
                        delay = min(300, 15 * (2 ** min(failures - 1, 5)))
                        self._update(scope, status="delayed",
                                     login_status=("human_action_required" if hard
                                                   else "automatic_recovery_delayed"),
                                     last_error=code, requires_human=int(hard),
                                     consecutive_failures=failures,
                                     backoff_until=(now + timedelta(seconds=delay)).isoformat())
                        self._event("provider_session_recovery_failed", {
                            "trigger": trigger, "code": code,
                            "requires_human": hard,
                        }, scope)
                        return {"job_id": None, "status": "delayed",
                                "already_running": False,
                                "watermark": self.status(principal=principal).get("watermark"),
                                "started_at": now.isoformat(),
                                "requires_human": hard}
            job = result.get("job") or {}
            state = str(result.get("status") or "idle")
            coordinator_status = (
                "syncing" if state in {"started", "already_running"}
                else "synced" if state == "fresh" else "delayed")
            self._update(scope, status=coordinator_status,
                         login_status=("authenticated" if coordinator_status != "delayed"
                                       else "automatic_recovery_delayed"),
                         last_job_id=job.get("sync_job_id"),
                         last_error=result.get("reason"), requires_human=0,
                         consecutive_failures=0, backoff_until=None)
            self._event("log_sync_ensured", {
                "trigger": trigger, "status": state,
                "job_id": job.get("sync_job_id"), "created": result.get("created", False),
            }, scope)
            status = self.status(principal=principal)
            return {"job_id": job.get("sync_job_id"), "status": state,
                    "already_running": state == "already_running",
                    "watermark": status.get("watermark"),
                    "started_at": job.get("started_at") or now.isoformat()}
        finally:
            self._lock.release()

    def status(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal)
        jobs = self.realtime.list_jobs("china_uat", principal=principal)
        latest = jobs[0] if jobs else None
        active = next((job for job in jobs if job.get("state") in ACTIVE_STATES), None)
        cursor = self.realtime.cursor("china_uat", principal=principal)
        with self.realtime.connect() as db:
            state = db.execute("""SELECT * FROM log_sync_coordinator_state
              WHERE tenant_id=? AND workspace_id=? AND environment_id='china_uat'""",
              scope.sql_parameters()).fetchone()
            total = db.execute("""SELECT COUNT(*) FROM realtime_log_records
              WHERE tenant_id=? AND workspace_id=? AND environment_id='china_uat'""",
              scope.sql_parameters()).fetchone()[0]
            placeholders = ",".join("?" for _ in EXACT_CORRELATIONS)
            linked = db.execute(f"""SELECT COUNT(*) FROM realtime_log_correlations
              WHERE tenant_id=? AND workspace_id=? AND environment_id='china_uat'
                AND state IN ({placeholders})""",
              (*scope.sql_parameters(), *sorted(EXACT_CORRELATIONS))).fetchone()[0]
        last_success = next((job.get("last_successful_read_at") or
                             job.get("last_successful_poll_at") for job in jobs
                             if job.get("last_successful_read_at") or
                             job.get("last_successful_poll_at")), None)
        row = dict(state) if state else {}
        status = "syncing" if active else ("synced" if last_success else row.get("status", "idle"))
        return {
            "environment_id": "china_uat", "status": status,
            "job_id": (active or latest or {}).get("sync_job_id"),
            "started_at": (active or {}).get("started_at"),
            "last_success_at": last_success, "last_check_at": row.get("last_check_at"),
            "watermark": (cursor or {}).get("cursor_value") or
                         (latest or {}).get("watermark_utc"),
            "batch_read": (latest or {}).get("collected_count", 0),
            "inserted": (latest or {}).get("inserted_count", 0),
            "duplicates": (latest or {}).get("duplicate_count", 0),
            "failed": (latest or {}).get("rejected_count", 0),
            "synced_logs": int(total), "linked": int(linked),
            "pending_link": max(0, int(total) - int(linked)),
            "shadow_results": sum(1 for item in self.shadow._rows(principal)
                                  if item.get("shadow_recommendation")),
            "login_status": row.get("login_status", "unknown"),
            "next_schedule_at": row.get("next_schedule_at"),
            "last_error": row.get("last_error"),
            "requires_human": bool(row.get("requires_human", 0)),
            "consecutive_failures": int(row.get("consecutive_failures", 0)),
            "backoff_until": row.get("backoff_until"),
            "revision": int(row.get("revision", 0)),
            "read_only": True, "uat_execution_switch_required": False,
        }

    def history(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        return {"items": self.realtime.list_jobs("china_uat", principal=principal),
                "environment_id": "china_uat"}

    def events(self, after_id: int = 0, *,
               principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal)
        with self.realtime.connect() as db:
            rows = db.execute("""SELECT * FROM log_sync_coordinator_events
              WHERE tenant_id=? AND workspace_id=? AND environment_id='china_uat'
                AND event_id>? ORDER BY event_id LIMIT 200""",
              (*scope.sql_parameters(), max(0, int(after_id)))).fetchall()
        return {"items": [{**dict(row), "details": json.loads(row["details_json"])}
                          for row in rows], "transport": "short_polling",
                "recommended_interval_seconds": 5}
