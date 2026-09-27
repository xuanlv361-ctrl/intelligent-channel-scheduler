from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.encrypted_session_vault import (
    APPROVED_HOSTS, EncryptedSessionVault, VaultError,
)
from backend.realtime_log_sync_service import (
    ACTIVE_STATES, STOPPING_STATES, TERMINAL_STATES, RealtimeLogSyncError,
    RealtimeLogSyncService, utcnow,
)
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext
from backend.tenant_security import TenantScope

LEASE_JOB_STATES = ACTIVE_STATES | STOPPING_STATES


class PersistentSessionError(ValueError):
    pass


class PersistentSessionService:
    def __init__(self, realtime: RealtimeLogSyncService,
                 vault: EncryptedSessionVault | None,
                 enabled: bool = False, default_ttl_hours: int = 8,
                 maximum_ttl_hours: int = 24,
                 lease_timeout_seconds: int = 120,
                 clock: Callable[[], datetime] | None = None,
                 authorization: AuthorizationService | None = None):
        self.realtime = realtime
        self.vault = vault
        self.enabled = bool(enabled)
        self.default_ttl_hours = int(default_ttl_hours)
        self.maximum_ttl_hours = int(maximum_ttl_hours)
        self.lease_timeout_seconds = max(30, int(lease_timeout_seconds))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.authorization = authorization or realtime.authorization
        if self.default_ttl_hours <= 0 or self.maximum_ttl_hours <= 0 or \
                self.default_ttl_hours > self.maximum_ttl_hours:
            raise PersistentSessionError("persistent_session_ttl_configuration_invalid")
        self._init()
        self.reconcile_leases("backend_startup")
        self.reconcile_pairings("backend_startup")

    def _scope(self, principal: PrincipalContext | None,
               permission: str) -> TenantScope:
        if self.authorization is None:
            if isinstance(principal, PrincipalContext) and principal.is_verified:
                return TenantScope(principal.tenant_id, principal.workspace_id)
            return TenantScope.local_development()
        try:
            verified = self.authorization.authorize(principal, permission)
        except PermissionError as exc:
            raise PersistentSessionError(str(exc)) from exc
        return TenantScope(verified.tenant_id, verified.workspace_id)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise PersistentSessionError("persistent_session_clock_invalid")
        return value.astimezone(timezone.utc)

    def connect(self):
        return self.realtime.connect()

    def _init(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS persistent_session_pairings(
              pairing_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL,
              state TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              current_safe_url TEXT, browser_state TEXT NOT NULL,
              worker_pid INTEGER, failure_code TEXT, failure_summary TEXT,
              storage_diagnostic_json TEXT, storage_diagnostic_status TEXT);
            CREATE TABLE IF NOT EXISTS persistent_browser_sessions(
              persistent_session_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL,
              state TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              last_validated_at TEXT, last_used_at TEXT, vault_reference_id TEXT NOT NULL,
              ciphertext_sha256 TEXT NOT NULL, authentication_status TEXT NOT NULL,
              revocation_status TEXT NOT NULL, created_by_local_operator INTEGER NOT NULL,
              auto_resume_enabled INTEGER NOT NULL DEFAULT 0, failure_code TEXT,
              failure_summary TEXT, storage_types_json TEXT NOT NULL DEFAULT '[]',
              revision INTEGER NOT NULL DEFAULT 1);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_persistent_session
              ON persistent_browser_sessions(environment_id)
              WHERE state IN ('active','validating','in_use');
            CREATE TABLE IF NOT EXISTS persistent_session_leases(
              lease_id TEXT PRIMARY KEY,
              persistent_session_id TEXT NOT NULL
                REFERENCES persistent_browser_sessions(persistent_session_id),
              lease_owner_job_id TEXT NOT NULL
                REFERENCES realtime_log_sync_jobs(sync_job_id),
              state TEXT NOT NULL,
              acquired_at TEXT NOT NULL,
              heartbeat_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              released_at TEXT,
              release_reason TEXT,
              generation INTEGER NOT NULL,
              timeout_seconds INTEGER NOT NULL DEFAULT 120,
              version INTEGER NOT NULL DEFAULT 1);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_persistent_session_lease
              ON persistent_session_leases(persistent_session_id)
              WHERE state='active';
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_persistent_job_lease
              ON persistent_session_leases(lease_owner_job_id)
              WHERE state='active';
            CREATE INDEX IF NOT EXISTS ix_persistent_session_lease_expiry
              ON persistent_session_leases(state,expires_at);
            """)
            existing_pairing = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(persistent_session_pairings)").fetchall()}
            if "storage_diagnostic_json" not in existing_pairing:
                db.execute(
                    "ALTER TABLE persistent_session_pairings "
                    "ADD COLUMN storage_diagnostic_json TEXT")
            if "storage_diagnostic_status" not in existing_pairing:
                db.execute(
                    "ALTER TABLE persistent_session_pairings "
                    "ADD COLUMN storage_diagnostic_status TEXT")
            if "service_credential_id" not in existing_pairing:
                db.execute(
                    "ALTER TABLE persistent_session_pairings "
                    "ADD COLUMN service_credential_id TEXT")
            db.execute("DROP INDEX IF EXISTS uq_active_persistent_pairing")
            db.execute("""CREATE UNIQUE INDEX uq_active_persistent_pairing
              ON persistent_session_pairings(environment_id)
              WHERE state IN ('browser_starting','waiting_for_manual_login',
                'waiting_for_operator','authentication_in_progress',
                'checking_authentication_storage','waiting_for_confirmation',
                'confirmation_requested','authentication_validation_failed')""")
            existing_session = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(persistent_browser_sessions)").fetchall()}
            if "storage_types_json" not in existing_session:
                db.execute(
                    "ALTER TABLE persistent_browser_sessions "
                    "ADD COLUMN storage_types_json TEXT NOT NULL DEFAULT '[]'")
            existing_lease = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(persistent_session_leases)").fetchall()}
            if "timeout_seconds" not in existing_lease:
                db.execute(
                    "ALTER TABLE persistent_session_leases "
                    "ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 120")
            local_scope = TenantScope.local_development()
            for table in ("persistent_session_pairings", "persistent_browser_sessions",
                          "persistent_session_leases"):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, value in (("tenant_id", local_scope.tenant_id),
                                      ("workspace_id", local_scope.workspace_id)):
                    if column not in columns:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{value}'")
                db.execute(f"""CREATE INDEX IF NOT EXISTS ix_{table}_enterprise_scope
                  ON {table}(tenant_id,workspace_id)""")
            db.execute("DROP INDEX IF EXISTS uq_active_persistent_pairing")
            db.execute("""CREATE UNIQUE INDEX uq_active_persistent_pairing
              ON persistent_session_pairings(tenant_id,workspace_id,environment_id)
              WHERE state IN ('browser_starting','waiting_for_manual_login',
                'waiting_for_operator','authentication_in_progress',
                'checking_authentication_storage','waiting_for_confirmation',
                'confirmation_requested','authentication_validation_failed')""")
            db.execute("DROP INDEX IF EXISTS uq_active_persistent_session")
            db.execute("""CREATE UNIQUE INDEX uq_active_persistent_session
              ON persistent_browser_sessions(tenant_id,workspace_id,environment_id)
              WHERE state IN ('active','validating','in_use')""")
            db.execute("DROP INDEX IF EXISTS uq_active_persistent_session_lease")
            db.execute("""CREATE UNIQUE INDEX uq_active_persistent_session_lease
              ON persistent_session_leases(tenant_id,workspace_id,persistent_session_id)
              WHERE state='active'""")
            db.execute("DROP INDEX IF EXISTS uq_active_persistent_job_lease")
            db.execute("""CREATE UNIQUE INDEX uq_active_persistent_job_lease
              ON persistent_session_leases(tenant_id,workspace_id,lease_owner_job_id)
              WHERE state='active'""")

    def _require_enabled(self):
        if not self.enabled:
            raise PersistentSessionError("persistent_mode_not_supported_on_platform")
        if self.vault is None:
            raise PersistentSessionError("persistent_mode_windows_only")

    @staticmethod
    def _require_china(environment_id: str):
        if environment_id != "china_uat":
            raise PersistentSessionError("persistent_session_environment_mismatch")

    def _event(self, event_type: str, details: dict[str, Any] | None = None,
               job_id: str | None = None, *,
               principal: PrincipalContext | None = None,
               permission: str = "collector.session.reauthenticate"):
        self.realtime.event(
            job_id, "china_uat", event_type, details or {},
            principal=principal, permission=permission)

    @staticmethod
    def audit_session_id(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _lease_event(
        db: sqlite3.Connection, job_id: str | None, event_type: str,
        details: dict[str, Any],
    ):
        db.execute("""INSERT INTO realtime_log_sync_events(
          sync_job_id,environment_id,event_type,created_at,details_json)
          VALUES(?,?,?,?,?)""", (
            job_id, "china_uat", event_type, utcnow(),
            json.dumps(details, ensure_ascii=False, sort_keys=True),
        ))

    def reconcile_leases(self, reason: str) -> dict[str, int]:
        """Repair only persisted lease/job inconsistencies; never touch vault data."""
        now = self._now()
        released = recovered = adopted = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            leases = db.execute("""SELECT l.*,j.state AS job_state,
              s.expires_at AS session_expires_at
              FROM persistent_session_leases l
              LEFT JOIN realtime_log_sync_jobs j
                ON j.sync_job_id=l.lease_owner_job_id
              JOIN persistent_browser_sessions s
                ON s.persistent_session_id=l.persistent_session_id
              WHERE l.state='active'""").fetchall()
            for lease in leases:
                job_state = lease["job_state"]
                lease_expired = datetime.fromisoformat(
                    lease["expires_at"]) <= now
                if job_state in LEASE_JOB_STATES and not lease_expired:
                    continue
                release_reason = (
                    "lease_expired_recovery" if lease_expired
                    else "terminal_job_recovery")
                db.execute("""UPDATE persistent_session_leases SET
                  state='released',released_at=?,release_reason=?,version=version+1
                  WHERE lease_id=? AND state='active'""", (
                    now.isoformat(), release_reason, lease["lease_id"]))
                if lease_expired and job_state == "stopping":
                    db.execute("""UPDATE realtime_log_sync_jobs SET
                      state='stopped',stopped_at=?,error_code=NULL,
                      safe_error_message=NULL,stop_reason='operator_stop',
                      browser_context_active=0
                      WHERE sync_job_id=? AND state='stopping'""", (
                        now.isoformat(), lease["lease_owner_job_id"]))
                elif lease_expired and job_state in LEASE_JOB_STATES:
                    db.execute("""UPDATE realtime_log_sync_jobs SET
                      state='failed',stopped_at=?,error_code=?,
                      stop_reason=?,browser_context_active=0
                      WHERE sync_job_id=? AND state IN
                      ('created','decrypting_session','launching_headless_browser',
                       'validating_authentication','synchronizing','stopping')""", (
                        now.isoformat(), "persistent_session_lease_expired",
                        "persistent_session_lease_expired",
                        lease["lease_owner_job_id"]))
                db.execute("""UPDATE persistent_browser_sessions SET state='active',
                  revision=revision+1
                  WHERE persistent_session_id=? AND state='in_use'""",
                           (lease["persistent_session_id"],))
                self._lease_event(
                    db, lease["lease_owner_job_id"],
                    "persistent_session_lease_recovered", {
                        "lease_id": lease["lease_id"],
                        "release_reason": release_reason,
                        "reconciliation_reason": reason,
                    })
                released += 1
            legacy_jobs = db.execute("""SELECT j.sync_job_id,j.persistent_session_id,
              j.created_at,j.state,s.expires_at
              FROM realtime_log_sync_jobs j
              JOIN persistent_browser_sessions s
                ON s.persistent_session_id=j.persistent_session_id
              LEFT JOIN persistent_session_leases l
                ON l.lease_owner_job_id=j.sync_job_id AND l.state='active'
              WHERE j.state IN
              ('created','decrypting_session','launching_headless_browser',
               'validating_authentication','synchronizing','stopping')
              AND j.persistent_session_id IS NOT NULL AND l.lease_id IS NULL""").fetchall()
            for job in legacy_jobs:
                if job["state"] == "stopping":
                    changed = db.execute("""UPDATE realtime_log_sync_jobs SET
                      state='stopped',stopped_at=?,error_code=NULL,
                      safe_error_message=NULL,stop_reason='operator_stop',
                      browser_context_active=0
                      WHERE sync_job_id=? AND state='stopping'""", (
                        now.isoformat(), job["sync_job_id"])).rowcount
                    if changed:
                        self._lease_event(
                            db, job["sync_job_id"],
                            "persistent_session_stop_reconciled", {
                                "reconciliation_reason": reason,
                                "stop_reason": "operator_stop",
                            })
                    continue
                changed = db.execute("""UPDATE realtime_log_sync_jobs SET
                  state='failed',stopped_at=?,error_code=?,
                  stop_reason=?,browser_context_active=0
                  WHERE sync_job_id=? AND state IN
                  ('created','decrypting_session','launching_headless_browser',
                   'validating_authentication','synchronizing','stopping')""", (
                    now.isoformat(), "persistent_session_lease_missing",
                    "orphan_job_reconciliation",
                    job["sync_job_id"])).rowcount
                if not changed:
                    continue
                self._lease_event(
                    db, job["sync_job_id"],
                    "persistent_session_orphan_job_recovered", {
                        "reconciliation_reason": reason,
                    })
                recovered += 1
            stale_sessions = db.execute("""SELECT s.persistent_session_id
              FROM persistent_browser_sessions s
              LEFT JOIN persistent_session_leases l
                ON l.persistent_session_id=s.persistent_session_id
                AND l.state='active'
              WHERE s.state='in_use' AND l.lease_id IS NULL""").fetchall()
            for session in stale_sessions:
                db.execute("""UPDATE persistent_browser_sessions SET state='active',
                  revision=revision+1
                  WHERE persistent_session_id=? AND state='in_use'""",
                           (session["persistent_session_id"],))
                self._lease_event(
                    db, None, "persistent_session_stale_lease_recovered", {
                        "persistent_session_id":
                            session["persistent_session_id"],
                        "reconciliation_reason": reason,
                    })
                recovered += 1
        return {"released": released, "adopted": adopted, "recovered": recovered}

    @staticmethod
    def _process_is_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except PermissionError:
            return True
        except (OSError, ValueError, TypeError):
            return False

    def reconcile_pairings(self, reason: str) -> int:
        """Release active pairing rows whose browser worker no longer exists."""
        active_states = (
            "browser_starting", "waiting_for_manual_login",
            "waiting_for_operator", "authentication_in_progress",
            "checking_authentication_storage", "waiting_for_confirmation",
            "confirmation_requested", "authentication_validation_failed",
        )
        with self.connect() as db:
            rows = db.execute(
                "SELECT pairing_id,worker_pid FROM persistent_session_pairings "
                f"WHERE state IN ({','.join('?' for _ in active_states)}) "
                "AND worker_pid IS NOT NULL",
                active_states,
            ).fetchall()
            stale = [row["pairing_id"] for row in rows
                     if not self._process_is_alive(row["worker_pid"])]
            for pairing_id in stale:
                db.execute("""UPDATE persistent_session_pairings
                  SET state='stopped',browser_state='closed',
                  failure_code='persistent_pairing_worker_not_running',
                  failure_summary='临时登录窗口已关闭；可以重新开始登录配对。'
                  WHERE pairing_id=?""", (pairing_id,))
        for pairing_id in stale:
            self._event("persistent_pairing_reconciled", {
                "pairing_id": pairing_id, "reason": reason})
        return len(stale)

    def heartbeat_lease_outcome(
        self, job_id: str, generation: int,
    ) -> str:
        """Renew an authoritative lease or return a state-aware fenced outcome."""
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT * FROM realtime_log_sync_jobs
              WHERE sync_job_id=?""", (job_id,)).fetchone()
            if not job:
                return "job_not_found"
            current_generation = job["lease_generation"]
            if current_generation is None or int(current_generation) != int(generation):
                self._lease_event(
                    db, job_id, "persistent_session_stale_heartbeat_ignored", {
                        "requested_generation": int(generation),
                        "current_generation": current_generation,
                        "job_state": job["state"],
                    })
                return "stale_generation"
            if job["state"] in TERMINAL_STATES:
                return "terminal"
            if job["state"] == "stopping":
                return "stopping"
            lease = db.execute("""SELECT l.*,s.expires_at AS session_expires_at
              FROM persistent_session_leases l
              JOIN persistent_browser_sessions s
                ON s.persistent_session_id=l.persistent_session_id
              WHERE l.lease_owner_job_id=? AND l.generation=?
                AND l.state='active' AND l.expires_at>?""",
                               (job_id, int(generation), now.isoformat())).fetchone()
            if not lease:
                other = db.execute("""SELECT lease_owner_job_id,generation
                  FROM persistent_session_leases
                  WHERE persistent_session_id=? AND state='active'""",
                                   (job["persistent_session_id"],)).fetchone()
                if other:
                    self._lease_event(
                        db, job_id,
                        "persistent_session_wrong_owner_heartbeat_rejected", {
                            "requested_generation": int(generation),
                            "current_owner_job_id": other["lease_owner_job_id"],
                            "current_generation": other["generation"],
                        })
                    return "wrong_owner"
                self._terminalize_job_and_release(
                    db, job, "failed", "persistent_session_lease_lost", now,
                    "persistent_session_lease_lost", {
                        "generation": int(generation),
                        "classification": "unexpected_missing_while_running",
                    })
                db.execute("""UPDATE realtime_log_sync_jobs SET
                  error_code='persistent_session_lease_lost',
                  error_at=?
                  WHERE sync_job_id=? AND state='failed'
                    AND stop_reason='persistent_session_lease_lost'""",
                           (now.isoformat(), job_id))
                return "lease_lost"
            expires = min(
                datetime.fromisoformat(lease["session_expires_at"]),
                now + timedelta(seconds=int(lease["timeout_seconds"])),
            ).isoformat()
            changed = db.execute("""UPDATE persistent_session_leases SET
              heartbeat_at=?,expires_at=?,version=version+1
              WHERE lease_id=? AND generation=? AND state='active'""", (
                now.isoformat(), expires, lease["lease_id"],
                int(generation))).rowcount
            if changed:
                self._lease_event(
                    db, job_id, "persistent_session_lease_heartbeat", {
                        "lease_id": lease["lease_id"],
                        "generation": int(generation),
                        "expires_at": expires,
                    })
            return "renewed" if changed else "lease_lost"

    def heartbeat_lease(self, job_id: str, generation: int) -> bool:
        """Compatibility wrapper for callers that only need renewed/not-renewed."""
        return self.heartbeat_lease_outcome(job_id, generation) == "renewed"

    @staticmethod
    def _assert_active_lease(
        db: sqlite3.Connection, session_id: str, job_id: str, generation: int,
        scope: TenantScope,
    ) -> None:
        lease = db.execute("""SELECT 1
          FROM persistent_session_leases l
          JOIN realtime_log_sync_jobs j
            ON j.sync_job_id=l.lease_owner_job_id
          WHERE l.persistent_session_id=? AND l.lease_owner_job_id=?
            AND l.generation=? AND l.state='active' AND l.expires_at>?
            AND l.tenant_id=? AND l.workspace_id=?
            AND j.tenant_id=l.tenant_id AND j.workspace_id=l.workspace_id
            AND j.lease_generation=l.generation
            AND j.state IN
            ('created','decrypting_session','launching_headless_browser',
             'validating_authentication','synchronizing')""", (
                session_id, job_id, int(generation), utcnow(),
                *scope.sql_parameters())).fetchone()
        if not lease:
            raise PersistentSessionError("persistent_session_lease_lost")

    def release_lease(
        self, job_id: str, reason: str, generation: int | None = None,
    ) -> bool:
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if generation is None:
                lease = db.execute("""SELECT * FROM persistent_session_leases
                  WHERE lease_owner_job_id=? AND state='active'""",
                                   (job_id,)).fetchone()
            else:
                lease = db.execute("""SELECT * FROM persistent_session_leases
                  WHERE lease_owner_job_id=? AND generation=? AND state='active'""",
                                   (job_id, int(generation))).fetchone()
            if not lease:
                return False
            changed = db.execute("""UPDATE persistent_session_leases SET
              state='released',released_at=?,release_reason=?,version=version+1
              WHERE lease_id=? AND state='active'""",
                                 (now, reason[:80], lease["lease_id"])).rowcount
            if changed:
                self._lease_event(
                    db, job_id, "persistent_session_lease_released", {
                        "lease_id": lease["lease_id"],
                        "generation": lease["generation"],
                        "release_reason": reason[:80],
                    })
            return bool(changed)

    def _terminalize_job_and_release(
        self, db: sqlite3.Connection, job: sqlite3.Row,
        terminal_state: str, stop_reason: str, now: datetime,
        event_type: str, event_details: dict[str, Any] | None = None,
    ) -> bool:
        """CAS one terminal state and release its same-generation lease."""
        changed = db.execute("""UPDATE realtime_log_sync_jobs SET
          state=?,stopped_at=?,stop_reason=?,browser_context_active=0,
          error_code=NULL,safe_error_message=NULL,elapsed_seconds=?
          WHERE sync_job_id=? AND state NOT IN
          ('stopped','completed','blocked','failed','session_expired',
           'persistent_session_expired','reauthentication_required')""", (
            terminal_state, now.isoformat(), stop_reason[:80],
            max(0, int((now - datetime.fromisoformat(
                job["started_at"] or job["created_at"])).total_seconds())),
            job["sync_job_id"],
        )).rowcount
        if changed != 1:
            return False
        lease = db.execute("""SELECT * FROM persistent_session_leases
          WHERE lease_owner_job_id=? AND generation=? AND state='active'""", (
            job["sync_job_id"], int(job["lease_generation"]))).fetchone()
        if lease:
            released = db.execute("""UPDATE persistent_session_leases SET
              state='released',released_at=?,release_reason=?,version=version+1
              WHERE lease_id=? AND state='active' AND generation=?""", (
                now.isoformat(), stop_reason[:80], lease["lease_id"],
                int(job["lease_generation"]))).rowcount
            if released:
                self._lease_event(
                    db, job["sync_job_id"],
                    "persistent_session_lease_released", {
                        "lease_id": lease["lease_id"],
                        "generation": int(job["lease_generation"]),
                        "release_reason": stop_reason[:80],
                    })
        active_lease = db.execute("""SELECT 1
          FROM persistent_session_leases
          WHERE persistent_session_id=? AND state='active'""",
                                  (job["persistent_session_id"],)).fetchone()
        if not active_lease:
            db.execute("""UPDATE persistent_browser_sessions SET
              state='active',revision=revision+1
              WHERE persistent_session_id=? AND state='in_use'""",
                       (job["persistent_session_id"],))
        details = {
            "terminal_state": terminal_state,
            "stop_reason": stop_reason[:80],
            "generation": int(job["lease_generation"]),
        }
        details.update(event_details or {})
        self._lease_event(db, job["sync_job_id"], event_type, details)
        return True

    def finalize_stop(
        self, job_id: str, generation: int, reason: str = "operator_stop",
        *, principal: PrincipalContext | None = None,
        permission: str = "collector.oneshot.execute",
    ) -> dict[str, Any]:
        """Idempotently converge stopping to stopped and release the lease."""
        scope = self._scope(principal, permission)
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT * FROM realtime_log_sync_jobs
              WHERE sync_job_id=? AND tenant_id=? AND workspace_id=?""",
              (job_id,*scope.sql_parameters())).fetchone()
            if not job:
                raise LookupError("log_sync_job_not_found")
            if job["lease_generation"] is None or int(
                    job["lease_generation"]) != int(generation):
                self._lease_event(
                    db, job_id, "persistent_session_stale_stop_ignored", {
                        "requested_generation": int(generation),
                        "current_generation": job["lease_generation"],
                    })
            elif job["state"] == "stopping":
                self._terminalize_job_and_release(
                    db, job, "stopped", reason, now,
                    "persistent_session_stop_finalized")
        result = self.realtime.get_job(job_id, principal=principal,
                                       permission=permission)
        self.realtime.finalize_batch(
            job_id, reason, principal=principal, permission=permission)
        return result

    def finalize_one_shot(
        self,
        job_id: str,
        generation: int,
        *,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        """Write the one-shot terminal state once and release its lease."""
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                "SELECT * FROM realtime_log_sync_jobs WHERE sync_job_id=?",
                (job_id,),
            ).fetchone()
            if not job:
                raise LookupError("log_sync_job_not_found")
            if (
                job["lease_generation"] is None
                or int(job["lease_generation"]) != int(generation)
            ):
                self._lease_event(
                    db,
                    job_id,
                    "persistent_session_stale_one_shot_finalize_ignored",
                    {
                        "requested_generation": int(generation),
                        "current_generation": job["lease_generation"],
                    },
                )
            elif job["state"] not in TERMINAL_STATES:
                terminal_state = "failed" if error_code else "stopped"
                reason = error_code or "one_shot_completed"
                self._terminalize_job_and_release(
                    db,
                    job,
                    terminal_state,
                    reason,
                    now,
                    "persistent_one_shot_finalized",
                    {"http_read_count": int(job["http_read_count"])},
                )
                if error_code:
                    db.execute(
                        """UPDATE realtime_log_sync_jobs
                           SET error_code=?,error_at=?
                           WHERE sync_job_id=? AND state='failed'
                             AND stop_reason=?""",
                        (error_code, now.isoformat(), job_id, reason),
                    )
        result = self.realtime.get_job(job_id)
        self.realtime.finalize_batch(job_id, error_code or "one_shot_completed")
        return result

    def authorize_http_read(
        self, job_id: str, generation: int,
    ) -> dict[str, Any]:
        """Atomically enforce independent read/record/time bounds."""
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT * FROM realtime_log_sync_jobs
              WHERE sync_job_id=?""", (job_id,)).fetchone()
            if not job:
                raise LookupError("log_sync_job_not_found")
            if job["lease_generation"] is None or int(
                    job["lease_generation"]) != int(generation):
                self._lease_event(
                    db, job_id, "persistent_session_stale_poll_ignored", {
                        "requested_generation": int(generation),
                        "current_generation": job["lease_generation"],
                    })
                return {
                    "authorized": False, "reason": "stale_generation",
                    "http_read_count": int(job["http_read_count"]),
                }
            if job["state"] == "stopping":
                return {
                    "authorized": False, "reason": "stopping",
                    "http_read_count": int(job["http_read_count"]),
                }
            if job["state"] in TERMINAL_STATES:
                return {
                    "authorized": False, "reason": "terminal",
                    "http_read_count": int(job["http_read_count"]),
                }
            lease = db.execute("""SELECT 1 FROM persistent_session_leases
              WHERE lease_owner_job_id=? AND generation=? AND state='active'
                AND expires_at>?""", (
                    job_id, int(generation), now.isoformat())).fetchone()
            if not lease:
                self._terminalize_job_and_release(
                    db, job, "failed", "persistent_session_lease_lost", now,
                    "persistent_session_lease_lost", {
                        "generation": int(generation),
                        "classification":
                            "missing_before_http_read_authorization",
                    })
                db.execute("""UPDATE realtime_log_sync_jobs SET
                  error_code='persistent_session_lease_lost',error_at=?
                  WHERE sync_job_id=? AND state='failed'
                    AND stop_reason='persistent_session_lease_lost'""",
                           (now.isoformat(), job_id))
                return {
                    "authorized": False, "reason": "lease_lost",
                    "http_read_count": int(job["http_read_count"]),
                    "terminalized": True,
                }
            started = datetime.fromisoformat(
                job["started_at"] or job["created_at"])
            elapsed = max(0, int((now - started).total_seconds()))
            limits = (
                ("maximum_elapsed_seconds",
                 elapsed >= int(job["maximum_elapsed_seconds"])),
                ("maximum_http_reads",
                 int(job["http_read_count"]) >=
                 int(job["maximum_http_reads"])),
                ("maximum_records_observed",
                 int(job["collected_count"]) >=
                 int(job["maximum_records_observed"])),
                ("maximum_records_accepted",
                 int(job["inserted_count"]) >=
                 int(job["maximum_records_accepted"])),
            )
            reached = next((name for name, hit in limits if hit), None)
            if reached:
                self._terminalize_job_and_release(
                    db, job, "stopped", "bounded_limit_reached", now,
                    "persistent_sync_bounded_limit_reached", {
                        "limit_type": reached,
                        "authorized_http_reads":
                            int(job["maximum_http_reads"]),
                        "actual_http_reads": int(job["http_read_count"]),
                        "records_observed": int(job["collected_count"]),
                        "records_accepted": int(job["inserted_count"]),
                        "elapsed_seconds": elapsed,
                    })
                return {
                    "authorized": False,
                    "reason": "bounded_limit_reached",
                    "limit_type": reached,
                    "http_read_count": int(job["http_read_count"]),
                    "terminalized": True,
                }
            changed = db.execute("""UPDATE realtime_log_sync_jobs SET
              http_read_count=http_read_count+1,network_called=1,
              elapsed_seconds=?
              WHERE sync_job_id=? AND lease_generation=? AND state IN
              ('validating_authentication','synchronizing')
              AND http_read_count<maximum_http_reads
              AND collected_count<maximum_records_observed
              AND inserted_count<maximum_records_accepted""", (
                elapsed, job_id, int(generation))).rowcount
            if changed != 1:
                return {
                    "authorized": False, "reason": "state_changed",
                    "http_read_count": int(job["http_read_count"]),
                }
            count = int(job["http_read_count"]) + 1
            self._lease_event(
                db, job_id, "persistent_sync_http_read_authorized", {
                    "http_read_count": count,
                    "maximum_http_reads": int(job["maximum_http_reads"]),
                    "generation": int(generation),
                })
            return {
                "authorized": True, "reason": "authorized",
                "http_read_count": count, "elapsed_seconds": elapsed,
            }

    def check_bounded_limits(
        self, job_id: str, generation: int,
    ) -> dict[str, Any]:
        """Check limits without reserving a read; terminalize on an exact bound."""
        now = self._now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT * FROM realtime_log_sync_jobs
              WHERE sync_job_id=?""", (job_id,)).fetchone()
            if not job:
                raise LookupError("log_sync_job_not_found")
            if job["lease_generation"] is None or int(
                    job["lease_generation"]) != int(generation):
                return {"bounded": False, "reason": "stale_generation"}
            if job["state"] in TERMINAL_STATES:
                return {"bounded": True, "reason": "terminal"}
            if job["state"] == "stopping":
                return {"bounded": True, "reason": "stopping"}
            started = datetime.fromisoformat(
                job["started_at"] or job["created_at"])
            elapsed = max(0, int((now - started).total_seconds()))
            checks = (
                ("maximum_elapsed_seconds",
                 elapsed >= int(job["maximum_elapsed_seconds"])),
                ("maximum_http_reads",
                 int(job["http_read_count"]) >=
                 int(job["maximum_http_reads"])),
                ("maximum_records_observed",
                 int(job["collected_count"]) >=
                 int(job["maximum_records_observed"])),
                ("maximum_records_accepted",
                 int(job["inserted_count"]) >=
                 int(job["maximum_records_accepted"])),
            )
            reached = next((name for name, hit in checks if hit), None)
            if not reached:
                db.execute("""UPDATE realtime_log_sync_jobs
                  SET elapsed_seconds=? WHERE sync_job_id=?""",
                           (elapsed, job_id))
                return {
                    "bounded": False, "reason": "within_limits",
                    "elapsed_seconds": elapsed,
                }
            self._terminalize_job_and_release(
                db, job, "stopped", "bounded_limit_reached", now,
                "persistent_sync_bounded_limit_reached", {
                    "limit_type": reached,
                    "authorized_http_reads": int(job["maximum_http_reads"]),
                    "actual_http_reads": int(job["http_read_count"]),
                    "records_observed": int(job["collected_count"]),
                    "records_accepted": int(job["inserted_count"]),
                    "elapsed_seconds": elapsed,
                })
            return {
                "bounded": True, "reason": "bounded_limit_reached",
                "limit_type": reached,
                "http_read_count": int(job["http_read_count"]),
                "terminalized": True,
            }

    @staticmethod
    def _pairing_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item.pop("worker_pid", None)
        item.pop("storage_diagnostic_json", None)
        return item

    def start_pairing(self, environment_id: str, ttl_hours: int | None,
                      explicit_confirmation: bool, *,
                      principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.reauthenticate")
        self._require_enabled()
        self._require_china(environment_id)
        if not explicit_confirmation:
            raise PersistentSessionError("persistent_explicit_confirmation_required")
        ttl = self.default_ttl_hours if ttl_hours is None else int(ttl_hours)
        if ttl <= 0 or ttl > self.maximum_ttl_hours:
            raise PersistentSessionError("persistent_session_ttl_invalid")
        contract = self.realtime.resolve_environment(environment_id)
        if contract["log_page_url"] != "https://uat.weimeta.cn/console/billing/logs" or \
                set(contract["allowed_hosts"]) != set(APPROVED_HOSTS):
            raise PersistentSessionError("persistent_session_host_mismatch")
        now = self._now()
        pairing_id = f"PAIR-{uuid.uuid4().hex[:24].upper()}"
        try:
            with self.connect() as db:
                # A failed validation is terminal for that pairing.  Keeping it
                # inside the unique "active pairing" index prevents the next
                # operator-authorized reauthentication from ever starting.
                db.execute("""UPDATE persistent_session_pairings
                  SET state='stopped',browser_state='authentication_failed'
                  WHERE environment_id=? AND state='authentication_validation_failed'
                  AND tenant_id=? AND workspace_id=?""",
                           (environment_id, *scope.sql_parameters()))
                db.execute("""UPDATE persistent_session_pairings
                  SET state='expired',browser_state='closed',
                  failure_code='persistent_session_expired',
                  failure_summary='登录配对已过期。'
                  WHERE environment_id=? AND expires_at<=? AND state IN
                  ('browser_starting','waiting_for_manual_login',
                   'waiting_for_operator','authentication_in_progress',
                   'checking_authentication_storage','waiting_for_confirmation',
                   'confirmation_requested','authentication_validation_failed')
                  AND tenant_id=? AND workspace_id=?""",
                           (environment_id, now.isoformat(),*scope.sql_parameters()))
                db.execute("""INSERT INTO persistent_session_pairings(
                  pairing_id,environment_id,state,created_at,expires_at,browser_state,
                  tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?)""", (
                    pairing_id, environment_id, "browser_starting",
                    now.isoformat(), (now + timedelta(hours=ttl)).isoformat(),
                    "launch_requested",
                    *scope.sql_parameters(),
                ))
        except sqlite3.IntegrityError as exc:
            raise PersistentSessionError("persistent_session_already_active") from exc
        self._event("persistent_pairing_started", {
            "pairing_id": pairing_id, "expires_at": (now + timedelta(hours=ttl)).isoformat()},
            principal=principal)
        return self.get_pairing(pairing_id, principal=principal,
                                permission="collector.session.reauthenticate")

    def start_reauthentication(
        self, environment_id: str, ttl_hours: int | None,
        explicit_confirmation: bool,
        *, principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        """Start an isolated pairing operation without creating a sync job."""
        pairing = self.start_pairing(
            environment_id, ttl_hours, explicit_confirmation, principal=principal)
        self._event("persistent_reauthentication_started", {
            "pairing_id": pairing["pairing_id"],
            "environment_id": environment_id,
        }, principal=principal)
        return pairing

    def get_pairing(self, pairing_id: str, private: bool = False, *,
                    principal: PrincipalContext | None = None,
                    permission: str = "collector.session.read") -> dict[str, Any]:
        scope = self._scope(principal, permission)
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM persistent_session_pairings WHERE pairing_id=?
                AND tenant_id=? AND workspace_id=?""",
                (pairing_id,*scope.sql_parameters())).fetchone()
        if not row:
            raise PersistentSessionError("persistent_pairing_not_found")
        return dict(row) if private else self._pairing_public(row)

    def update_pairing(self, pairing_id: str, *,
                       principal: PrincipalContext | None = None,
                       permission: str = "collector.session.reauthenticate",
                       **updates: Any) -> dict[str, Any]:
        scope = self._scope(principal, permission)
        allowed = {
            "state", "current_safe_url", "browser_state", "worker_pid",
            "failure_code", "failure_summary", "storage_diagnostic_json",
            "storage_diagnostic_status",
        }
        if set(updates) - allowed:
            raise PersistentSessionError("persistent_pairing_update_not_allowed")
        with self.connect() as db:
            db.execute("UPDATE persistent_session_pairings SET " +
                       ",".join(f"{key}=?" for key in updates) +
                       " WHERE pairing_id=? AND tenant_id=? AND workspace_id=?",
                       (*updates.values(), pairing_id,*scope.sql_parameters()))
        return self.get_pairing(pairing_id, private=True, principal=principal,
                                permission=permission)

    def save_storage_diagnostic(
        self, pairing_id: str, diagnostic: dict[str, Any],
        *, principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        """Persist only names and security metadata, never authentication values."""
        allowed_top = {
            "cookie_metadata", "local_storage_keys", "session_storage_keys",
            "indexed_db_names", "authentication_origins",
            "changed_storage_types", "indexed_db_capture_supported",
        }
        if set(diagnostic) - allowed_top:
            raise PersistentSessionError("persistent_storage_diagnostic_invalid")
        serialized = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
        for forbidden in ("value", "authorization", "password", "cookie_header"):
            if f'"{forbidden}"' in serialized.casefold():
                raise PersistentSessionError("persistent_storage_diagnostic_contains_secret")
        self.update_pairing(
            pairing_id, storage_diagnostic_json=serialized,
            storage_diagnostic_status="ready", state="waiting_for_confirmation",
            principal=principal)
        return self.storage_diagnostic(pairing_id, principal=principal)

    def storage_diagnostic(self, pairing_id: str, *,
                           principal: PrincipalContext | None = None) -> dict[str, Any]:
        pairing = self.get_pairing(pairing_id, private=True, principal=principal)
        raw = pairing.get("storage_diagnostic_json")
        return {
            "pairing_id": pairing_id,
            "status": pairing.get("storage_diagnostic_status") or "pending",
            "diagnostic": json.loads(raw) if raw else None,
        }

    def request_confirmation(self, pairing_id: str,
                             explicit_confirmation: bool, *,
                             principal: PrincipalContext | None = None) -> dict[str, Any]:
        self._require_enabled()
        if not explicit_confirmation:
            raise PersistentSessionError("persistent_explicit_confirmation_required")
        pairing = self.get_pairing(pairing_id, private=True, principal=principal,
                                   permission="collector.session.reauthenticate")
        if pairing["environment_id"] != "china_uat":
            raise PersistentSessionError("persistent_session_environment_mismatch")
        if pairing["state"] not in {
                "waiting_for_manual_login", "waiting_for_operator",
                "waiting_for_confirmation",
                "authentication_validation_failed"}:
            raise PersistentSessionError("persistent_pairing_invalid_state")
        self.update_pairing(
            pairing_id, state="confirmation_requested",
            failure_code=None, failure_summary=None, principal=principal)
        return self.get_pairing(pairing_id, principal=principal,
                                permission="collector.session.reauthenticate")

    def complete_pairing(
        self, pairing_id: str, cookies: list[dict[str, Any]],
        web_storage: list[dict[str, Any]] | None = None,
        indexed_db: list[dict[str, Any]] | None = None,
        remote_expires_at: str | None = None,
        *, principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.reauthenticate")
        self._require_enabled()
        pairing = self.get_pairing(pairing_id, private=True, principal=principal,
                                   permission="collector.session.reauthenticate")
        if pairing["state"] != "confirmation_requested":
            raise PersistentSessionError("persistent_pairing_invalid_state")
        pairing_created_at = datetime.fromisoformat(pairing["created_at"])
        pairing_expires_at = datetime.fromisoformat(pairing["expires_at"])
        requested_ttl = pairing_expires_at - pairing_created_at
        session_created_at = self._now()
        local_upper_bound = session_created_at + min(
            requested_ttl, timedelta(hours=24))
        if remote_expires_at is not None:
            try:
                remote_expiry = datetime.fromisoformat(
                    str(remote_expires_at).replace("Z", "+00:00"))
            except ValueError as exc:
                raise PersistentSessionError("remote_session_expiry_invalid") from exc
            if remote_expiry.tzinfo is None or remote_expiry.utcoffset() is None:
                raise PersistentSessionError("remote_session_expiry_invalid")
            remote_expiry = remote_expiry.astimezone(timezone.utc)
            if remote_expiry <= session_created_at:
                raise PersistentSessionError("remote_session_expired")
            session_expires_at = min(remote_expiry, local_upper_bound)
        else:
            session_expires_at = local_upper_bound
        metadata = self.vault.save(
            "china_uat", session_created_at.isoformat(),
            session_expires_at.isoformat(), cookies,
            web_storage, indexed_db, tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id)
        storage_types = [
            *(["cookies"] if metadata["cookie_count"] else []),
            *(["localStorage"] if metadata["local_storage_count"] else []),
            *(["sessionStorage"] if metadata["session_storage_count"] else []),
            *(["IndexedDB"] if metadata["indexed_db_count"] else []),
        ]
        session_id = f"PSESSION-{uuid.uuid4().hex[:24].upper()}"
        old_reference = None
        try:
            with self.connect() as db:
                lease = db.execute("""SELECT l.lease_id
                  FROM persistent_session_leases l
                  JOIN persistent_browser_sessions s
                    ON s.persistent_session_id=l.persistent_session_id
                  WHERE s.environment_id='china_uat' AND l.state='active'
                    AND l.expires_at>? AND l.tenant_id=? AND l.workspace_id=?
                    AND s.tenant_id=l.tenant_id AND s.workspace_id=l.workspace_id
                    LIMIT 1""", (utcnow(),*scope.sql_parameters())).fetchone()
                if lease:
                    raise PersistentSessionError("persistent_session_in_use")
                old = db.execute("""SELECT vault_reference_id FROM
                  persistent_browser_sessions WHERE environment_id='china_uat'
                  AND state IN ('active','in_use') AND tenant_id=? AND workspace_id=?""",
                  scope.sql_parameters()).fetchone()
                old_reference = old[0] if old else None
                db.execute("""UPDATE persistent_browser_sessions
                  SET state='replaced',authentication_status='superseded',
                  revision=revision+1 WHERE environment_id='china_uat'
                  AND state IN ('active','in_use') AND tenant_id=? AND workspace_id=?""",
                  scope.sql_parameters())
                db.execute("""INSERT INTO persistent_browser_sessions(
                  persistent_session_id,environment_id,state,created_at,expires_at,
                  last_validated_at,last_used_at,vault_reference_id,ciphertext_sha256,
                  authentication_status,revocation_status,created_by_local_operator,
                  auto_resume_enabled,failure_code,failure_summary,
                  storage_types_json,revision,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    session_id, "china_uat", "active", session_created_at.isoformat(),
                    session_expires_at.isoformat(), utcnow(), None,
                    metadata["vault_reference_id"], metadata["ciphertext_sha256"],
                    "verified", "not_revoked", 1, 0, None, None,
                    json.dumps(storage_types), 1,*scope.sql_parameters(),
                ))
                db.execute("""UPDATE persistent_session_pairings
                  SET state='completed',browser_state='authenticated_chrome_running'
                  WHERE pairing_id=? AND tenant_id=? AND workspace_id=?""",
                  (pairing_id,*scope.sql_parameters()))
        except Exception:
            self.vault.delete(metadata["vault_reference_id"])
            raise
        if old_reference:
            self.vault.delete(old_reference)
        self._event("persistent_login_confirmed", {"pairing_id": pairing_id}, principal=principal)
        self._event("persistent_authentication_verified", {"pairing_id": pairing_id}, principal=principal)
        self._event("persistent_session_encrypted", {"persistent_session_id": session_id}, principal=principal)
        self._event("persistent_session_saved", {"persistent_session_id": session_id}, principal=principal)
        return self.get_session(session_id, principal=principal,
                                permission="collector.session.reauthenticate")

    @staticmethod
    def _session_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item.pop("vault_reference_id", None)
        item.pop("ciphertext_sha256", None)
        item["created_by_local_operator"] = bool(item["created_by_local_operator"])
        item["auto_resume_enabled"] = bool(item["auto_resume_enabled"])
        item["storage_types"] = json.loads(item.pop("storage_types_json", "[]"))
        return item

    def _session_with_lease(
        self, row: sqlite3.Row | dict[str, Any], scope: TenantScope,
    ) -> dict[str, Any]:
        item = self._session_public(row)
        with self.connect() as db:
            lease = db.execute("""SELECT l.lease_id,l.lease_owner_job_id,l.state,
              l.acquired_at,l.heartbeat_at,l.expires_at,l.released_at,
              l.release_reason,l.generation,l.version
              FROM persistent_session_leases l
              JOIN realtime_log_sync_jobs j
                ON j.sync_job_id=l.lease_owner_job_id
              WHERE l.persistent_session_id=? AND l.state='active'
                AND l.expires_at>? AND j.lease_generation=l.generation
                AND l.tenant_id=? AND l.workspace_id=?
                AND j.tenant_id=l.tenant_id AND j.workspace_id=l.workspace_id
                AND j.state IN
                ('created','decrypting_session','launching_headless_browser',
                 'validating_authentication','synchronizing','stopping')
              ORDER BY l.generation DESC LIMIT 1""",
                               (item["persistent_session_id"], utcnow(),
                                *scope.sql_parameters())).fetchone()
        if item["state"] in {"active", "in_use"}:
            item["state"] = "in_use" if lease else "active"
        item["lease"] = dict(lease) if lease else None
        item["usage_state"] = "in_use" if lease else "available"
        return item

    def get_session(self, session_id: str, private: bool = False, *,
                    principal: PrincipalContext | None = None,
                    permission: str = "collector.session.read") -> dict[str, Any]:
        scope = self._scope(principal, permission)
        with self.connect() as db:
            row = db.execute("""SELECT * FROM persistent_browser_sessions
              WHERE persistent_session_id=? AND tenant_id=? AND workspace_id=?""",
              (session_id,*scope.sql_parameters())).fetchone()
        if not row:
            raise PersistentSessionError("persistent_session_not_found")
        return dict(row) if private else self._session_with_lease(row, scope)

    def status(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.read")
        with self.connect() as db:
            row = db.execute("""SELECT * FROM persistent_browser_sessions
              WHERE environment_id='china_uat' AND tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC LIMIT 1""",scope.sql_parameters()).fetchone()
            pairing = db.execute("""SELECT * FROM persistent_session_pairings
              WHERE environment_id='china_uat' AND tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC LIMIT 1""",scope.sql_parameters()).fetchone()
            active_job = db.execute("""SELECT j.sync_job_id,j.state,
              j.last_successful_poll_at,j.inserted_count,j.correlated_count,
              l.lease_id,l.heartbeat_at AS lease_heartbeat_at,
              l.expires_at AS lease_expires_at,l.generation AS lease_generation
              FROM persistent_session_leases l
              JOIN realtime_log_sync_jobs j
                ON j.sync_job_id=l.lease_owner_job_id
              WHERE l.state='active' AND l.expires_at>?
              AND l.persistent_session_id=?
              AND l.tenant_id=? AND l.workspace_id=?
              AND j.tenant_id=l.tenant_id AND j.workspace_id=l.workspace_id
              AND j.lease_generation=l.generation AND j.state IN
              ('created','decrypting_session','launching_headless_browser',
               'validating_authentication','synchronizing','stopping')
              ORDER BY l.acquired_at DESC LIMIT 1""",
                                    (utcnow(), row["persistent_session_id"]
                                     if row else "",*scope.sql_parameters())).fetchone()
            latest_job = db.execute("""SELECT sync_job_id,state,created_at,
              stopped_at,last_successful_poll_at,collected_count,inserted_count,
              rejected_count,duplicate_count,schema_status,stop_reason,
              error_code,safe_error_message,source_type,sync_mode,
              periodic_polling,
              http_read_count,maximum_http_reads,pages_read,maximum_pages,
              current_page,consecutive_empty_reads,consecutive_failures,
              last_successful_read_at,watermark_utc,source_cursor,
              lease_generation,latest_metric_snapshot_id,
              latest_confidence_snapshot_id,provenance_manifest_sha256,
              date_from_utc,date_to_utc,timezone,out_of_range_count,
              missing_timestamp_count
              FROM realtime_log_sync_jobs
              WHERE persistent_session_id=? AND tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC LIMIT 1""",
                                    (row["persistent_session_id"]
                                     if row else "",*scope.sql_parameters())).fetchone()
            latest_schema = None
            if latest_job:
                latest_schema = db.execute("""SELECT schema_fingerprint,
                  schema_adapter_id,adapter_version,selection_status,
                  rejection_reason,observed_at
                  FROM realtime_log_schema_audit
                  WHERE sync_job_id=? AND tenant_id=? AND workspace_id=?
                  ORDER BY observed_at DESC LIMIT 1""",
                  (latest_job["sync_job_id"],*scope.sql_parameters())).fetchone()
            last_lease_event = db.execute("""SELECT event_type,created_at,
              details_json FROM realtime_log_sync_events
              WHERE event_type IN
              ('persistent_session_stale_lease_recovered',
               'persistent_session_lease_recovered',
               'persistent_session_lease_released')
              AND tenant_id=? AND workspace_id=?
              ORDER BY event_id DESC LIMIT 1""",scope.sql_parameters()).fetchone()
            actual_cost_enriched_count = 0
            if active_job:
                cost_rows = db.execute("""SELECT c.details_json
                  FROM realtime_log_correlations c
                  JOIN realtime_log_records r ON r.record_id=c.record_id
                    AND r.tenant_id=c.tenant_id AND r.workspace_id=c.workspace_id
                  WHERE r.sync_job_id=? AND c.state IN
                  ('exact_request_id','exact_response_id')
                  AND c.tenant_id=? AND c.workspace_id=?""",
                  (active_job["sync_job_id"],*scope.sql_parameters())).fetchall()
                actual_cost_enriched_count = sum(
                    json.loads(item["details_json"]).get("actual_cost") is not None
                    for item in cost_rows)
        public_session = self._session_with_lease(row, scope) if row else None
        if public_session and public_session["state"] in {"active", "in_use"} and \
                datetime.fromisoformat(
                    public_session["expires_at"]) <= datetime.now(timezone.utc):
            public_session = {
                **public_session,
                "state": "expired",
                "authentication_status": "expired",
                "failure_code": "persistent_session_expired",
            }
        return {
            "enabled": self.enabled,
            "environment_id": "china_uat",
            "session": public_session,
            "pairing": self._pairing_public(pairing) if pairing else None,
            "active_job": ({
                **dict(active_job),
                "actual_cost_enriched_count": actual_cost_enriched_count,
            } if active_job else None),
            "latest_job": ({
                **dict(latest_job),
                "schema_adapter": dict(latest_schema)
                if latest_schema else None,
            } if latest_job else None),
            "last_lease_event": ({
                "event_type": last_lease_event["event_type"],
                "created_at": last_lease_event["created_at"],
                "details": json.loads(last_lease_event["details_json"]),
            } if last_lease_event else None),
        }

    def load_authentication_state(
        self, session_id: str, environment_id: str, *,
        job_id: str, lease_generation: int,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(principal, "evidence.import")
        self._require_enabled()
        self._require_china(environment_id)
        session = self.get_session(session_id, private=True, principal=principal,
                                   permission="evidence.import")
        if session["environment_id"] != environment_id:
            raise PersistentSessionError("persistent_session_environment_mismatch")
        if session["state"] not in {"active", "in_use"}:
            raise PersistentSessionError("persistent_session_not_found")
        if datetime.fromisoformat(session["expires_at"]) <= datetime.now(timezone.utc):
            self._expire(session)
            raise PersistentSessionError("persistent_session_expired")
        with self.connect() as db:
            self._assert_active_lease(
                db, session_id, job_id, lease_generation, scope)
        self._event("persistent_session_decryption_started", {
            "session_id_hash": self.audit_session_id(session_id)}, job_id,
            principal=principal,permission="evidence.import")
        try:
            payload = self.vault.load(session["vault_reference_id"], environment_id,
                                      tenant_id=scope.tenant_id,
                                      workspace_id=scope.workspace_id)
        except VaultError as exc:
            code = str(exc)
            self._disable(session, code)
            self._event("persistent_session_validation_failed", {
                "session_id_hash": self.audit_session_id(session_id),
                "failure_code": code}, job_id, principal=principal,
                permission="evidence.import")
            raise PersistentSessionError(code) from exc
        self._event("persistent_session_decryption_succeeded", {
            "session_id_hash": self.audit_session_id(session_id)}, job_id,
            principal=principal,permission="evidence.import")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_active_lease(
                db, session_id, job_id, lease_generation, scope)
            db.execute("""UPDATE persistent_browser_sessions SET
              last_validated_at=?,last_used_at=?,authentication_status='verified',
              revision=revision+1 WHERE persistent_session_id=?
              AND tenant_id=? AND workspace_id=?""",
                       (utcnow(), utcnow(), session_id,*scope.sql_parameters()))
        return payload

    def inspect_session(
        self, session_id: str, environment_id: str,
        *, principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.read")
        self._require_enabled()
        self._require_china(environment_id)
        session = self.get_session(session_id, private=True, principal=principal)
        if session["environment_id"] != environment_id:
            raise PersistentSessionError("persistent_session_environment_mismatch")
        self._require_usable_session(session)
        try:
            return {
                "persistent_session_id": session_id,
                **self.vault.inspect_metadata(
                    session["vault_reference_id"], environment_id,
                    tenant_id=scope.tenant_id,workspace_id=scope.workspace_id),
            }
        except VaultError as exc:
            raise PersistentSessionError(str(exc)) from exc

    def validate_session(
        self, session_id: str, environment_id: str,
        *, principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.reauthenticate")
        self._require_enabled()
        session = self.get_session(session_id, private=True, principal=principal,
                                   permission="collector.session.reauthenticate")
        self._require_china(environment_id)
        if session["environment_id"] != environment_id:
            raise PersistentSessionError("persistent_session_environment_mismatch")
        self._require_usable_session(session)
        self._event("persistent_session_validation_started", {
            "session_id_hash": self.audit_session_id(session_id)},principal=principal)
        try:
            result = self.vault.validate(
                session["vault_reference_id"], session["environment_id"],
                tenant_id=scope.tenant_id,workspace_id=scope.workspace_id)
        except VaultError as exc:
            self._disable(session, str(exc))
            self._event("persistent_session_validation_failed", {
                "session_id_hash": self.audit_session_id(session_id),
                "failure_code": str(exc)},principal=principal)
            raise PersistentSessionError(str(exc)) from exc
        self._event("persistent_session_validation_succeeded", {
            "session_id_hash": self.audit_session_id(session_id)},principal=principal)
        return {
            "persistent_session_id": session_id,
            "vault_status": result["status"],
            "schema_version": result["schema_version"],
            "environment_id": result["environment_id"],
            "approved_origins": result["approved_origins"],
            "created_at": result["created_at"],
            "expires_at": result["expires_at"],
            "storage_types": result["storage_types"],
        }

    @staticmethod
    def _require_usable_session(session: dict[str, Any]):
        if datetime.fromisoformat(session["expires_at"]) <= datetime.now(timezone.utc):
            raise PersistentSessionError("persistent_session_expired")
        if session.get("state") not in {"active", "in_use"} or \
                session.get("authentication_status") != "verified" or \
                session.get("revocation_status") != "not_revoked" or \
                not bool(session.get("created_by_local_operator")):
            raise PersistentSessionError("persistent_session_not_found")

    def rotate_authentication_state(
        self, session_id: str, cookies: list[dict[str, Any]],
        web_storage: list[dict[str, Any]], indexed_db: list[dict[str, Any]],
        *, job_id: str, lease_generation: int,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(principal, "evidence.import")
        session = self.get_session(session_id, private=True, principal=principal,
                                   permission="evidence.import")
        with self.connect() as db:
            self._assert_active_lease(
                db, session_id, job_id, lease_generation, scope)
        metadata = self.vault.save(
            session["environment_id"],
            session["created_at"], session["expires_at"], cookies,
            web_storage, indexed_db,tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id)
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._assert_active_lease(
                    db, session_id, job_id, lease_generation, scope)
                changed = db.execute("""UPDATE persistent_browser_sessions SET
                  vault_reference_id=?,ciphertext_sha256=?,last_validated_at=?,
                  revision=revision+1
                  WHERE persistent_session_id=? AND revision=?
                    AND state IN ('active','in_use')
                    AND authentication_status='verified'
                    AND revocation_status='not_revoked'
                    AND tenant_id=? AND workspace_id=?""", (
                        metadata["vault_reference_id"],
                        metadata["ciphertext_sha256"], utcnow(), session_id,
                        session["revision"],*scope.sql_parameters())).rowcount
                if changed != 1:
                    raise PersistentSessionError(
                        "persistent_session_rotation_conflict")
        except Exception:
            self.vault.delete(metadata["vault_reference_id"])
            raise
        self.vault.delete(session["vault_reference_id"])
        self._event("persistent_session_rotated", {
            "session_id_hash": self.audit_session_id(session_id),
            "lease_generation": int(lease_generation)}, job_id,principal=principal,
            permission="evidence.import")
        return self.get_session(session_id, principal=principal,
                                permission="evidence.import")

    def shutdown_active_jobs(self) -> int:
        """Fail closed before workers are terminated during backend shutdown."""
        with self.connect() as db:
            rows = db.execute("""SELECT sync_job_id
              FROM realtime_log_sync_jobs
              WHERE sync_mode='persistent_encrypted_session' AND state IN
              ('created','decrypting_session','launching_headless_browser',
               'validating_authentication','synchronizing','stopping')""").fetchall()
        for row in rows:
            self.realtime.update_job(
                row["sync_job_id"], state="failed", stopped_at=utcnow(),
                browser_context_active=0,
                error_code="persistent_backend_shutdown",
                stop_reason="backend_shutdown")
        return len(rows)

    def stop_job(self, job_id: str, stop_worker: Any, *,
                 principal: PrincipalContext | None = None) -> dict[str, Any]:
        self._scope(principal, "collector.oneshot.execute")
        job = self.realtime.get_job(job_id, private=True, principal=principal,
                                    permission="collector.oneshot.execute")
        if job.get("sync_mode") != "persistent_encrypted_session":
            raise PersistentSessionError(
                "persistent_session_environment_mismatch")
        if job["state"] in {
                "stopped", "completed", "blocked", "failed",
                "session_expired", "persistent_session_expired",
                "reauthentication_required"}:
            return self.realtime.get_job(job_id, principal=principal,
                                         permission="collector.oneshot.execute")
        generation = int(job["lease_generation"])
        self.realtime.update_job(
            job_id, principal=principal,permission="collector.oneshot.execute",
            state="stopping", stop_reason="operator_stop")
        try:
            stopped = bool(stop_worker(job_id))
        except Exception as exc:
            raise PersistentSessionError(
                "persistent_worker_stop_failed") from exc
        if not stopped:
            raise PersistentSessionError("persistent_worker_stop_failed")
        return self.finalize_stop(job_id, generation, "operator_stop",
                                  principal=principal)

    def _stop_session_jobs(self, session_id: str, reason: str):
        with self.connect() as db:
            jobs = db.execute("""SELECT sync_job_id
              FROM realtime_log_sync_jobs
              WHERE persistent_session_id=? AND state IN
              ('created','decrypting_session','launching_headless_browser',
               'validating_authentication','synchronizing','stopping')""",
                              (session_id,)).fetchall()
        for job in jobs:
            self.realtime.update_job(
                job["sync_job_id"], state="failed", stopped_at=utcnow(),
                browser_context_active=0, error_code=reason)

    def _disable(self, session: dict[str, Any], code: str):
        self._stop_session_jobs(session["persistent_session_id"], code)
        self.vault.delete(session["vault_reference_id"])
        with self.connect() as db:
            db.execute("""UPDATE persistent_browser_sessions SET state='disabled',
              authentication_status='invalid',failure_code=?,failure_summary=?,
              revision=revision+1 WHERE persistent_session_id=?""",
                       (code, "本地加密登录状态不可用。", session["persistent_session_id"]))

    def invalidate_reauthentication(self, session_id: str):
        session = self.get_session(session_id, private=True)
        self._disable(session, "persistent_session_reauthentication_required")
        self._event("persistent_session_validation_failed", {
            "session_id_hash": self.audit_session_id(session_id),
            "failure_code": "persistent_session_reauthentication_required"})

    def _expire(self, session: dict[str, Any]):
        self._stop_session_jobs(
            session["persistent_session_id"], "persistent_session_expired")
        self.vault.delete(session["vault_reference_id"])
        with self.connect() as db:
            db.execute("""UPDATE persistent_browser_sessions
              SET state='expired',authentication_status='expired',
              failure_code='persistent_session_expired',
              failure_summary='本地加密登录状态已过期。',revision=revision+1
              WHERE persistent_session_id=?""", (session["persistent_session_id"],))
        self._event("persistent_session_expired", {
            "persistent_session_id": session["persistent_session_id"]})

    def create_job(self, body: dict[str, Any], *,
                   principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.oneshot.execute")
        self._require_enabled()
        if not body.get("explicit_confirmation"):
            raise PersistentSessionError("persistent_explicit_confirmation_required")
        self._require_china(body["environment_id"])
        runtime = self.realtime.runtime_settings
        setting = (
            runtime.active("china_uat", "logs_page_url")
            if runtime is not None else None)
        if (
            runtime is not None
            and getattr(runtime, "requires_domestic_confirmation", False)
            and not setting
        ):
            raise PersistentSessionError("log_sync_log_page_unconfirmed")
        self.reconcile_leases("job_start")
        self.inspect_session(
            body["persistent_session_id"], body["environment_id"],principal=principal)
        expected_session = self.get_session(
            body["persistent_session_id"], private=True,principal=principal,
            permission="collector.oneshot.execute")
        expected_revision = int(expected_session["revision"])
        try:
            interval = int(body["sync_interval_seconds"])
            maximum = int(body["maximum_records"])
            maximum_http_reads = int(body.get("maximum_http_reads", 20))
            maximum_observed = int(body.get(
                "maximum_records_observed", maximum))
            maximum_accepted = int(body.get(
                "maximum_records_accepted", maximum))
            maximum_elapsed = int(body.get("maximum_elapsed_seconds", 600))
            periodic_polling = body.get("periodic_polling", True)
            page_size = int(body.get("page_size", 100))
            maximum_pages = int(body.get("maximum_pages", 10))
            if not isinstance(periodic_polling, bool):
                raise RealtimeLogSyncError(
                    "log_sync_periodic_polling_invalid")
            if not 7 <= interval <= 30:
                raise RealtimeLogSyncError("log_sync_poll_interval_invalid")
            if not 1 <= maximum <= 1000:
                raise RealtimeLogSyncError("log_sync_maximum_records_invalid")
            if not 1 <= maximum_http_reads <= 50:
                raise RealtimeLogSyncError(
                    "log_sync_maximum_http_reads_invalid")
            if not 1 <= maximum_observed <= 1000:
                raise RealtimeLogSyncError(
                    "log_sync_maximum_records_observed_invalid")
            if not 1 <= maximum_accepted <= maximum_observed:
                raise RealtimeLogSyncError(
                    "log_sync_maximum_records_accepted_invalid")
            if not 10 <= maximum_elapsed <= 600:
                raise RealtimeLogSyncError(
                    "log_sync_maximum_elapsed_invalid")
            if not 1 <= page_size <= 100:
                raise RealtimeLogSyncError("log_sync_page_size_invalid")
            if not 1 <= maximum_pages <= 50:
                raise RealtimeLogSyncError("log_sync_maximum_pages_invalid")
            if not periodic_polling and maximum_http_reads != 1:
                raise RealtimeLogSyncError(
                    "log_sync_one_shot_requires_one_http_read")
            date_from, date_to, timezone_name = self.realtime.normalize_range(
                body["date_from"], body["date_to"], body["timezone"])
            contract = self.realtime.resolve_environment(body["environment_id"])
            job_id = f"LSYNC-{uuid.uuid4().hex[:20].upper()}"
            lease_id = f"PSSL-{uuid.uuid4().hex[:24].upper()}"
            now = datetime.now(timezone.utc)
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                session = db.execute("""SELECT * FROM persistent_browser_sessions
                  WHERE persistent_session_id=? AND tenant_id=? AND workspace_id=?""",
                                     (body["persistent_session_id"],*scope.sql_parameters())).fetchone()
                if not session or session["environment_id"] != body["environment_id"]:
                    raise PersistentSessionError(
                        "persistent_session_environment_mismatch")
                self._require_usable_session(dict(session))
                if int(session["revision"]) != expected_revision:
                    raise PersistentSessionError(
                        "persistent_session_concurrent_update")
                active_lease = db.execute("""SELECT lease_id
                  FROM persistent_session_leases
                  WHERE persistent_session_id=? AND state='active'
                  AND tenant_id=? AND workspace_id=?""",
                  (body["persistent_session_id"],*scope.sql_parameters())).fetchone()
                if active_lease:
                    raise PersistentSessionError("persistent_session_in_use")
                generation = db.execute("""SELECT COALESCE(MAX(generation),0)+1
                  FROM persistent_session_leases
                  WHERE persistent_session_id=? AND tenant_id=? AND workspace_id=?""",
                  (body["persistent_session_id"],*scope.sql_parameters())).fetchone()[0]
                db.execute("""INSERT INTO realtime_log_sync_jobs(
                  sync_job_id,environment_id,state,created_at,safe_log_page_url,
                  poll_interval_seconds,maximum_records,source_type,date_from_utc,
                  date_to_utc,timezone,allowed_hosts_json,sync_mode,
                  persistent_session_id,lease_generation,maximum_http_reads,
                  maximum_records_observed,maximum_records_accepted,
                  maximum_elapsed_seconds,periodic_polling,page_size,
                  maximum_pages,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    job_id, body["environment_id"], "created", now.isoformat(),
                    contract["log_page_url"], interval, maximum,
                    contract["source_type"], date_from, date_to, timezone_name,
                    json.dumps(contract["allowed_hosts"]),
                    "persistent_encrypted_session",
                     body["persistent_session_id"],
                     generation,
                     maximum_http_reads, maximum_observed, maximum_accepted,
                     maximum_elapsed, int(periodic_polling), page_size,
                     maximum_pages,
                     *scope.sql_parameters(),
                 ))
                cursor_row = db.execute("""SELECT cursor_kind,cursor_value,
                  updated_at,sync_job_id FROM realtime_log_sync_cursors
                  WHERE environment_id=? AND tenant_id=? AND workspace_id=?""", (
                    body["environment_id"], *scope.sql_parameters())).fetchone()
                pending_count = 0
                call_log_columns = {row[1] for row in db.execute(
                    "PRAGMA table_info(standardized_call_logs)")}
                if {"cost_status", "traffic_class", "request_id"}.issubset(
                        call_log_columns):
                    pending_count = int(db.execute("""SELECT COUNT(*) FROM
                      standardized_call_logs WHERE tenant_id=? AND workspace_id=?
                      AND environment_id=? AND source_type='realtime_execution'
                      AND cost_status='pending_provider_sync'
                      AND traffic_class='business' AND request_id IS NOT NULL
                      AND TRIM(request_id)<>''""", (
                        *scope.sql_parameters(), body["environment_id"])
                    ).fetchone()[0])
                db.execute("""INSERT INTO provider_log_sync_batches(
                  sync_batch_id,sync_job_id,environment_id,source_type,
                  started_at,previous_watermark_json,initial_pending_count,
                  tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?)""", (
                    job_id, job_id, body["environment_id"],
                    contract["source_type"], now.isoformat(),
                    json.dumps(dict(cursor_row) if cursor_row else {},
                               ensure_ascii=False, sort_keys=True),
                    pending_count, *scope.sql_parameters()))
                lease_expires = min(
                    datetime.fromisoformat(session["expires_at"]),
                    now + timedelta(seconds=self.lease_timeout_seconds),
                ).isoformat()
                db.execute("""INSERT INTO persistent_session_leases(
                  lease_id,persistent_session_id,lease_owner_job_id,state,
                  acquired_at,heartbeat_at,expires_at,generation,
                  timeout_seconds,version,tenant_id,workspace_id)
                  VALUES(?,?,?,'active',?,?,?,?,?,1,?,?)""", (
                    lease_id, body["persistent_session_id"], job_id,
                    now.isoformat(), now.isoformat(), lease_expires, generation,
                    self.lease_timeout_seconds,
                    *scope.sql_parameters(),
                ))
                self._lease_event(db, job_id, "job_created", {
                    "log_page_url": contract["log_page_url"],
                    "date_from_utc": date_from,
                    "date_to_utc": date_to,
                    "timezone": timezone_name,
                    "periodic_polling": periodic_polling,
                    "page_size": page_size,
                    "maximum_pages": maximum_pages,
                })
                self._lease_event(
                    db, job_id, "persistent_session_lease_acquired", {
                        "lease_id": lease_id,
                        "generation": generation,
                        "expires_at": lease_expires,
                    })
            return self.realtime.get_job(job_id, private=True,principal=principal,
                                         permission="collector.oneshot.execute")
        except sqlite3.IntegrityError as exc:
            raise PersistentSessionError(
                "log_sync_job_already_active") from exc
        except RealtimeLogSyncError as exc:
            raise PersistentSessionError(str(exc)) from exc

    def revoke(self, session_id: str, confirmation_text: str,
               stop_job: Any, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.reauthenticate")
        self._require_enabled()
        if confirmation_text != "我确认清除本机保存的UAT登录状态。":
            raise PersistentSessionError("persistent_revoke_confirmation_required")
        session = self.get_session(session_id, private=True,principal=principal,
                                   permission="collector.session.reauthenticate")
        with self.connect() as db:
            jobs = db.execute("""SELECT sync_job_id FROM realtime_log_sync_jobs
              WHERE persistent_session_id=? AND state IN
              ('created','decrypting_session','launching_headless_browser',
               'validating_authentication','synchronizing','stopping')
              AND tenant_id=? AND workspace_id=?""",
              (session_id,*scope.sql_parameters())).fetchall()
        for row in jobs:
            if self.authorization is None:
                self.realtime.stop(row[0])
            else:
                self.realtime.stop(row[0],principal=principal,
                                   permission="collector.session.reauthenticate")
            stop_job(row[0])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("""SELECT vault_reference_id
              FROM persistent_browser_sessions
              WHERE persistent_session_id=? AND tenant_id=? AND workspace_id=?""",
              (session_id,*scope.sql_parameters())).fetchone()
            if not current:
                raise PersistentSessionError("persistent_session_not_found")
            db.execute("""UPDATE persistent_browser_sessions SET state='revoked',
              revocation_status='revoked_local_copy',
              authentication_status='unavailable',revision=revision+1
              WHERE persistent_session_id=? AND tenant_id=? AND workspace_id=?""",
              (session_id,*scope.sql_parameters()))
            reference_id = current["vault_reference_id"]
        self.vault.delete(reference_id)
        self._event("persistent_session_revoked", {
            "session_id_hash": self.audit_session_id(session_id)},principal=principal)
        return {
            "persistent_session_id": session_id, "state": "revoked",
            "local_encrypted_session_removed": True,
            "platform_session_revoked": False,
        }

    def set_auto_resume(self, session_id: str, enabled: bool,
                        explicit_confirmation: bool, *,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.reauthenticate")
        self._require_enabled()
        if not explicit_confirmation:
            raise PersistentSessionError("persistent_explicit_confirmation_required")
        session = self.get_session(session_id, private=True,principal=principal,
                                   permission="collector.session.reauthenticate")
        if session["state"] not in {"active", "in_use"}:
            raise PersistentSessionError("persistent_session_not_found")
        if enabled:
            raise PersistentSessionError(
                "persistent_auto_resume_not_safely_supported")
        with self.connect() as db:
            db.execute("""UPDATE persistent_browser_sessions
              SET auto_resume_enabled=?,revision=revision+1
              WHERE persistent_session_id=? AND tenant_id=? AND workspace_id=?""",
              (int(enabled), session_id,*scope.sql_parameters()))
        return self.get_session(session_id,principal=principal,
                                permission="collector.session.reauthenticate")

    def auto_resume_candidates(self) -> list[str]:
        # Recovery requires a new fenced lease generation. Until that explicit
        # workflow exists, fail closed rather than relaunching a possibly live
        # child worker after backend restart.
        return []
