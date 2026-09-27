from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .identity import PrincipalResolver, VerifiedIdentity
from .principal import PrincipalContext, PrincipalType


class SessionError(PermissionError):
    pass


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise SessionError("session_timestamp_timezone_required")
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _token_digest(signing_key: bytes, value: str) -> str:
    return hmac.new(signing_key, value.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionValidation:
    principal: PrincipalContext
    session_id: str
    jti: str
    csrf_generation: int
    key_version: int
    version: int


class EnterpriseSessionManager:
    def __init__(self, database_path: str | Path, *, signing_key: bytes,
                 resolver: PrincipalResolver, absolute_ttl_seconds: int = 28800,
                 idle_ttl_seconds: int = 1800, key_version: int = 1,
                 clock_skew_seconds: int = 60,
                 clock: Callable[[], datetime] | None = None):
        if len(signing_key) < 32:
            raise SessionError("session_signing_key_too_short")
        if min(absolute_ttl_seconds, idle_ttl_seconds, key_version) <= 0:
            raise SessionError("session_policy_invalid")
        self.path = Path(database_path)
        self.signing_key = bytes(signing_key)
        self.resolver = resolver
        self.absolute_ttl_seconds = int(absolute_ttl_seconds)
        self.idle_ttl_seconds = int(idle_ttl_seconds)
        self.key_version = int(key_version)
        self.clock_skew_seconds = max(0, int(clock_skew_seconds))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_db(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS enterprise_security_versions(
              scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
              security_version INTEGER NOT NULL DEFAULT 1,
              updated_at TEXT NOT NULL,
              PRIMARY KEY(scope_type,scope_id));
            CREATE TABLE IF NOT EXISTS enterprise_sessions(
              session_id TEXT PRIMARY KEY, jti TEXT NOT NULL UNIQUE,
              token_digest TEXT NOT NULL, principal_id TEXT NOT NULL,
              principal_type TEXT NOT NULL CHECK(principal_type='human'),
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              roles_json TEXT NOT NULL, authn_method TEXT NOT NULL,
              authenticated_at TEXT NOT NULL, created_at TEXT NOT NULL,
              last_activity_at TEXT NOT NULL, absolute_expires_at TEXT NOT NULL,
              idle_expires_at TEXT NOT NULL, revoked_at TEXT,
              revocation_reason TEXT, principal_security_version INTEGER NOT NULL,
              tenant_security_version INTEGER NOT NULL,
              key_version INTEGER NOT NULL, csrf_generation INTEGER NOT NULL DEFAULT 1,
              version INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS ix_enterprise_sessions_principal
              ON enterprise_sessions(tenant_id,workspace_id,principal_id,revoked_at);
            CREATE TABLE IF NOT EXISTS enterprise_session_replay_nonces(
              session_id TEXT NOT NULL REFERENCES enterprise_sessions(session_id),
              nonce_digest TEXT NOT NULL, purpose TEXT NOT NULL,
              consumed_at TEXT NOT NULL,
              PRIMARY KEY(session_id,nonce_digest,purpose));
            CREATE TABLE IF NOT EXISTS enterprise_session_audit(
              audit_id TEXT PRIMARY KEY, session_id TEXT,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              principal_id TEXT NOT NULL, event_type TEXT NOT NULL,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL);
            """)

    def _security_version(self, db: sqlite3.Connection, scope_type: str,
                          scope_id: str, now: datetime, *, tenant_id: str,
                          workspace_id: str) -> int:
        columns = {str(row[1]) for row in db.execute(
            "PRAGMA table_info(enterprise_security_versions)")}
        tenant_scoped = {"tenant_id", "workspace_id"}.issubset(columns)
        if tenant_scoped:
            row = db.execute(
                "SELECT security_version FROM enterprise_security_versions "
                "WHERE tenant_id=? AND workspace_id=? AND scope_type=? AND scope_id=?",
                (tenant_id, workspace_id, scope_type, scope_id)).fetchone()
        else:
            row = db.execute("SELECT security_version FROM enterprise_security_versions "
                             "WHERE scope_type=? AND scope_id=?",
                             (scope_type, scope_id)).fetchone()
        if row:
            return int(row[0])
        if tenant_scoped:
            db.execute("""INSERT INTO enterprise_security_versions(
              scope_type,scope_id,security_version,updated_at,tenant_id,workspace_id)
              VALUES(?,?,1,?,?,?)""",
              (scope_type, scope_id, _iso(now), tenant_id, workspace_id))
        else:
            db.execute("""INSERT INTO enterprise_security_versions(
              scope_type,scope_id,security_version,updated_at) VALUES(?,?,1,?)""",
              (scope_type, scope_id, _iso(now)))
        return 1

    def _audit(self, db: sqlite3.Connection, row: sqlite3.Row | dict,
               event_type: str, details: dict[str, object] | None = None) -> None:
        db.execute("INSERT INTO enterprise_session_audit VALUES(?,?,?,?,?,?,?,?)", (
            f"SA-{uuid.uuid4().hex}", row.get("session_id") if isinstance(row, dict) else row["session_id"],
            row["tenant_id"], row["workspace_id"], row["principal_id"], event_type,
            _iso(self.clock()), json.dumps(details or {}, sort_keys=True, separators=(",", ":"))))

    def create(self, principal: PrincipalContext, *,
               absolute_ttl_seconds: int | None = None,
               idle_ttl_seconds: int | None = None) -> tuple[str, SessionValidation]:
        if not principal.is_verified or principal.principal_type is not PrincipalType.HUMAN:
            raise SessionError("verified_human_principal_required")
        if principal.is_development_identity and \
                self.resolver.config.deployment_mode != "development":
            raise SessionError("development_identity_forbidden_in_production")
        now = self.clock().astimezone(timezone.utc)
        absolute = min(absolute_ttl_seconds or self.absolute_ttl_seconds,
                       self.absolute_ttl_seconds)
        idle = min(idle_ttl_seconds or self.idle_ttl_seconds,
                   self.idle_ttl_seconds, absolute)
        if min(absolute, idle) <= 0:
            raise SessionError("session_policy_invalid")
        session_id, jti = f"SES-{uuid.uuid4().hex}", f"JTI-{uuid.uuid4().hex}"
        secret = secrets.token_urlsafe(48)
        token = f"es1.{session_id}.{secret}"
        with self._lock, self.connect() as db:
            principal_version = self._security_version(
                db, "principal", principal.principal_id, now,
                tenant_id=principal.tenant_id, workspace_id=principal.workspace_id)
            tenant_version = self._security_version(
                db, "tenant", principal.tenant_id, now,
                tenant_id=principal.tenant_id, workspace_id=principal.workspace_id)
            db.execute("""INSERT INTO enterprise_sessions VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,?,1)""", (
                session_id, jti, _token_digest(self.signing_key, token), principal.principal_id,
                PrincipalType.HUMAN.value, principal.tenant_id, principal.workspace_id,
                json.dumps(sorted(principal.roles)), principal.authn_method, _iso(now), _iso(now),
                _iso(now), _iso(now + timedelta(seconds=absolute)),
                _iso(now + timedelta(seconds=idle)), principal_version, tenant_version,
                self.key_version, 1))
            row = db.execute("SELECT * FROM enterprise_sessions WHERE session_id=?",
                             (session_id,)).fetchone()
            self._audit(db, row, "session_created", {"key_version": self.key_version})
        return token, self._validation_from_row(row, now, correlation_id=principal.correlation_id)

    def _parse(self, token: str) -> tuple[str, str]:
        parts = str(token or "").split(".")
        if len(parts) != 3 or parts[0] != "es1" or not parts[1].startswith("SES-"):
            raise SessionError("session_token_invalid")
        return parts[1], str(token)

    def _validation_from_row(self, row: sqlite3.Row, now: datetime, *,
                             correlation_id: str | None = None) -> SessionValidation:
        identity = VerifiedIdentity(
            principal_id=row["principal_id"], principal_type=PrincipalType.HUMAN,
            tenant_id=row["tenant_id"], workspace_id=row["workspace_id"],
            roles=tuple(json.loads(row["roles_json"])), authn_method=row["authn_method"],
            issued_at=_dt(row["created_at"]), expires_at=_dt(row["absolute_expires_at"]),
            session_id=row["session_id"], token_id=row["jti"],
            is_development_identity=row["authn_method"].startswith("windows_current_user_development"),
            security_version=int(row["principal_security_version"]),
        )
        principal = self.resolver.resolve_verified(identity, correlation_id=correlation_id)
        return SessionValidation(principal, row["session_id"], row["jti"],
                                 int(row["csrf_generation"]), int(row["key_version"]),
                                 int(row["version"]))

    def validate(self, token: str, *, request_nonce: str | None = None,
                 nonce_purpose: str = "request", touch: bool = True,
                 correlation_id: str | None = None) -> SessionValidation:
        session_id, full_token = self._parse(token)
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM enterprise_sessions WHERE session_id=?",
                             (session_id,)).fetchone()
            if not row or not hmac.compare_digest(
                    row["token_digest"], _token_digest(self.signing_key, full_token)):
                raise SessionError("session_token_invalid")
            if row["revoked_at"]:
                raise SessionError("session_revoked")
            if int(row["key_version"]) != self.key_version:
                raise SessionError("session_key_version_revoked")
            if _dt(row["absolute_expires_at"]) <= now:
                raise SessionError("session_absolute_expired")
            if _dt(row["idle_expires_at"]) <= now:
                raise SessionError("session_idle_expired")
            pver = self._security_version(
                db, "principal", row["principal_id"], now,
                tenant_id=row["tenant_id"], workspace_id=row["workspace_id"])
            tver = self._security_version(
                db, "tenant", row["tenant_id"], now,
                tenant_id=row["tenant_id"], workspace_id=row["workspace_id"])
            if pver != int(row["principal_security_version"]):
                raise SessionError("session_roles_changed")
            if tver != int(row["tenant_security_version"]):
                raise SessionError("session_tenant_revoked")
            if request_nonce is not None:
                self._consume_nonce(db, session_id, request_nonce, nonce_purpose, now)
            if touch:
                new_idle = min(now + timedelta(seconds=self.idle_ttl_seconds),
                               _dt(row["absolute_expires_at"]))
                changed = db.execute("""UPDATE enterprise_sessions
                  SET last_activity_at=?,idle_expires_at=?,version=version+1
                  WHERE session_id=? AND version=? AND revoked_at IS NULL""",
                  (_iso(now), _iso(new_idle), session_id, row["version"])).rowcount
                if changed != 1:
                    raise SessionError("session_concurrent_update_conflict")
                row = db.execute("SELECT * FROM enterprise_sessions WHERE session_id=?",
                                 (session_id,)).fetchone()
            return self._validation_from_row(row, now, correlation_id=correlation_id)

    def _consume_nonce(self, db: sqlite3.Connection, session_id: str, nonce: str,
                       purpose: str, now: datetime) -> None:
        if not nonce or len(nonce) > 512:
            raise SessionError("session_replay_nonce_invalid")
        digest = _token_digest(self.signing_key, f"{purpose}:{nonce}")
        try:
            columns = {str(row[1]) for row in db.execute(
                "PRAGMA table_info(enterprise_session_replay_nonces)")}
            if {"tenant_id", "workspace_id"}.issubset(columns):
                scope = db.execute("""SELECT tenant_id,workspace_id
                  FROM enterprise_sessions WHERE session_id=?""",
                  (session_id,)).fetchone()
                if not scope:
                    raise SessionError("session_revoked")
                db.execute("""INSERT INTO enterprise_session_replay_nonces(
                  session_id,nonce_digest,purpose,consumed_at,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?)""",
                  (session_id, digest, purpose, _iso(now),
                   scope["tenant_id"], scope["workspace_id"]))
            else:
                db.execute("""INSERT INTO enterprise_session_replay_nonces(
                  session_id,nonce_digest,purpose,consumed_at) VALUES(?,?,?,?)""",
                  (session_id, digest, purpose, _iso(now)))
        except sqlite3.IntegrityError as exc:
            raise SessionError("session_replay_detected") from exc

    def consume_nonce(self, session_id: str, nonce: str, purpose: str) -> None:
        with self._lock, self.connect() as db:
            row = db.execute("SELECT revoked_at FROM enterprise_sessions WHERE session_id=?",
                             (session_id,)).fetchone()
            if not row or row["revoked_at"]:
                raise SessionError("session_revoked")
            self._consume_nonce(db, session_id, nonce, purpose,
                                self.clock().astimezone(timezone.utc))

    def rotate_csrf(self, session_id: str) -> int:
        with self._lock, self.connect() as db:
            changed = db.execute("""UPDATE enterprise_sessions
              SET csrf_generation=csrf_generation+1,version=version+1
              WHERE session_id=? AND revoked_at IS NULL""", (session_id,)).rowcount
            if changed != 1:
                raise SessionError("session_revoked")
            return int(db.execute("SELECT csrf_generation FROM enterprise_sessions "
                                  "WHERE session_id=?", (session_id,)).fetchone()[0])

    def revoke_session(self, session_id: str, reason: str) -> bool:
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM enterprise_sessions WHERE session_id=?",
                             (session_id,)).fetchone()
            if not row:
                return False
            changed = db.execute("""UPDATE enterprise_sessions SET revoked_at=?,
              revocation_reason=?,csrf_generation=csrf_generation+1,version=version+1
              WHERE session_id=? AND revoked_at IS NULL""",
              (_iso(now), str(reason)[:256], session_id)).rowcount
            if changed:
                self._audit(db, row, "session_revoked", {"reason": str(reason)[:256]})
            return bool(changed)

    def revoke_all_for_principal(self, principal_id: str, reason: str) -> int:
        return self._revoke_scope("principal_id", principal_id, reason)

    def revoke_tenant(self, tenant_id: str, reason: str) -> int:
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self.connect() as db:
            scopes = db.execute("""SELECT DISTINCT tenant_id,workspace_id
              FROM enterprise_sessions WHERE tenant_id=?""", (tenant_id,)).fetchall()
            for scope in scopes:
                self._bump_version(db, "tenant", tenant_id, now,
                                   tenant_id=scope["tenant_id"],
                                   workspace_id=scope["workspace_id"])
            return db.execute("""UPDATE enterprise_sessions SET revoked_at=?,
              revocation_reason=?,csrf_generation=csrf_generation+1,version=version+1
              WHERE tenant_id=? AND revoked_at IS NULL""",
              (_iso(now), str(reason)[:256], tenant_id)).rowcount

    def _revoke_scope(self, column: str, value: str, reason: str) -> int:
        if column != "principal_id":
            raise SessionError("session_scope_invalid")
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self.connect() as db:
            scopes = db.execute("""SELECT DISTINCT tenant_id,workspace_id
              FROM enterprise_sessions WHERE principal_id=?""", (value,)).fetchall()
            for scope in scopes:
                self._bump_version(db, "principal", value, now,
                                   tenant_id=scope["tenant_id"],
                                   workspace_id=scope["workspace_id"])
            return db.execute(f"""UPDATE enterprise_sessions SET revoked_at=?,
              revocation_reason=?,csrf_generation=csrf_generation+1,version=version+1
              WHERE {column}=? AND revoked_at IS NULL""",
              (_iso(now), str(reason)[:256], value)).rowcount

    def bump_principal_security_version(self, principal_id: str) -> int:
        with self._lock, self.connect() as db:
            scopes = db.execute("""SELECT DISTINCT tenant_id,workspace_id
              FROM enterprise_sessions WHERE principal_id=?""",
              (principal_id,)).fetchall()
            if not scopes:
                raise SessionError("principal_session_scope_missing")
            versions = [self._bump_version(
                db, "principal", principal_id, self.clock().astimezone(timezone.utc),
                tenant_id=scope["tenant_id"], workspace_id=scope["workspace_id"])
                for scope in scopes]
            return max(versions)

    def _bump_version(self, db: sqlite3.Connection, scope_type: str,
                      scope_id: str, now: datetime, *, tenant_id: str,
                      workspace_id: str) -> int:
        current = self._security_version(
            db, scope_type, scope_id, now,
            tenant_id=tenant_id, workspace_id=workspace_id)
        updated = current + 1
        columns = {str(row[1]) for row in db.execute(
            "PRAGMA table_info(enterprise_security_versions)")}
        if {"tenant_id", "workspace_id"}.issubset(columns):
            db.execute("""UPDATE enterprise_security_versions
              SET security_version=?,updated_at=?
              WHERE tenant_id=? AND workspace_id=? AND scope_type=? AND scope_id=?""",
              (updated, _iso(now), tenant_id, workspace_id, scope_type, scope_id))
        else:
            db.execute("""UPDATE enterprise_security_versions
              SET security_version=?,updated_at=? WHERE scope_type=? AND scope_id=?""",
              (updated, _iso(now), scope_type, scope_id))
        return updated
