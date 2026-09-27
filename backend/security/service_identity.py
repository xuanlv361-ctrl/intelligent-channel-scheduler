from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .identity import PrincipalResolver, VerifiedIdentity
from .principal import PrincipalContext, PrincipalType


class ServiceIdentityError(PermissionError):
    pass


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class ServiceIdentityManager:
    def __init__(self, database_path: str | Path, registry_path: str | Path, *,
                 signing_key: bytes, resolver: PrincipalResolver,
                 clock: Callable[[], datetime] | None = None):
        if len(signing_key) < 32:
            raise ServiceIdentityError("service_signing_key_too_short")
        self.path, self.signing_key = Path(database_path), bytes(signing_key)
        self.resolver = resolver
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        try:
            raw = json.loads(Path(registry_path).read_text(encoding="utf-8"))
            self.registry_version = str(raw["registry_version"])
            self.maximum_ttl_seconds = int(raw["maximum_ttl_seconds"])
            self.identities = {str(x["service_name"]): x for x in raw["service_identities"]}
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ServiceIdentityError("service_identity_registry_invalid") from exc
        if self.maximum_ttl_seconds <= 0 or len(self.identities) != 6:
            raise ServiceIdentityError("service_identity_registry_invalid")
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS enterprise_service_credentials(
              credential_id TEXT PRIMARY KEY, service_name TEXT NOT NULL,
              token_digest TEXT NOT NULL, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL, audience TEXT NOT NULL,
              roles_json TEXT NOT NULL, jti TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
              last_used_at TEXT, revoked_at TEXT, revocation_reason TEXT,
              key_version INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1);
            CREATE INDEX IF NOT EXISTS ix_service_credentials_scope
              ON enterprise_service_credentials(tenant_id,workspace_id,service_name,revoked_at);
            CREATE TABLE IF NOT EXISTS enterprise_service_replay_nonces(
              credential_id TEXT NOT NULL, nonce_digest TEXT NOT NULL,
              consumed_at TEXT NOT NULL,
              PRIMARY KEY(credential_id,nonce_digest));
            CREATE TABLE IF NOT EXISTS enterprise_service_audit(
              audit_id TEXT PRIMARY KEY, credential_id TEXT,
              service_name TEXT NOT NULL, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL, event_type TEXT NOT NULL,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL);
            """)

    def _digest(self, value: str) -> str:
        return hmac.new(self.signing_key, value.encode(), hashlib.sha256).hexdigest()

    def issue(self, service_name: str, tenant_id: str, workspace_id: str,
              audience: str, *, ttl_seconds: int, key_version: int = 1,
              rotate_credential_id: str | None = None) -> tuple[str, dict[str, object]]:
        definition = self.identities.get(service_name)
        if not definition or audience not in definition["allowed_audiences"]:
            raise ServiceIdentityError("service_identity_not_authorized")
        ttl = int(ttl_seconds)
        if ttl <= 0 or ttl > self.maximum_ttl_seconds or key_version <= 0:
            raise ServiceIdentityError("service_credential_ttl_invalid")
        if not tenant_id or not workspace_id:
            raise ServiceIdentityError("service_identity_scope_required")
        now = self.clock().astimezone(timezone.utc)
        credential_id, jti = f"SVC-{uuid.uuid4().hex}", f"JTI-{uuid.uuid4().hex}"
        token = f"sv1.{credential_id}.{secrets.token_urlsafe(48)}"
        with self._lock, self.connect() as db:
            if rotate_credential_id:
                changed = db.execute("""UPDATE enterprise_service_credentials
                  SET revoked_at=?,revocation_reason='rotated',version=version+1
                  WHERE credential_id=? AND service_name=? AND revoked_at IS NULL""",
                  (_iso(now), rotate_credential_id, service_name)).rowcount
                if changed != 1:
                    raise ServiceIdentityError("service_credential_rotation_failed")
            db.execute("""INSERT INTO enterprise_service_credentials VALUES(
              ?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,1)""", (
                credential_id, service_name, self._digest(token), tenant_id, workspace_id,
                audience, json.dumps(definition["roles"], sort_keys=True), jti,
                _iso(now), _iso(now + timedelta(seconds=ttl)), key_version))
            db.execute("INSERT INTO enterprise_service_audit VALUES(?,?,?,?,?,?,?,?)", (
                f"SIA-{uuid.uuid4().hex}", credential_id, service_name, tenant_id,
                workspace_id, "service_credential_issued", _iso(now), "{}"))
        return token, {"credential_id": credential_id, "service_name": service_name,
                       "tenant_id": tenant_id, "workspace_id": workspace_id,
                       "audience": audience, "jti": jti,
                       "created_at": _iso(now),
                       "expires_at": _iso(now + timedelta(seconds=ttl))}

    def validate(self, token: str, audience: str, *, request_nonce: str,
                 correlation_id: str | None = None) -> PrincipalContext:
        parts = str(token or "").split(".")
        if len(parts) != 3 or parts[0] != "sv1" or not request_nonce:
            raise ServiceIdentityError("service_token_invalid")
        now = self.clock().astimezone(timezone.utc)
        with self._lock, self.connect() as db:
            row = db.execute("SELECT * FROM enterprise_service_credentials WHERE credential_id=?",
                             (parts[1],)).fetchone()
            if not row or not hmac.compare_digest(row["token_digest"], self._digest(token)):
                raise ServiceIdentityError("service_token_invalid")
            if row["revoked_at"]:
                raise ServiceIdentityError("service_credential_revoked")
            if _dt(row["expires_at"]) <= now:
                raise ServiceIdentityError("service_credential_expired")
            definition = self.identities.get(row["service_name"])
            if not definition or audience != row["audience"] or \
                    audience not in definition["allowed_audiences"]:
                raise ServiceIdentityError("service_audience_mismatch")
            nonce_digest = self._digest(f"nonce:{request_nonce}")
            try:
                columns = {str(item[1]) for item in db.execute(
                    "PRAGMA table_info(enterprise_service_replay_nonces)")}
                if {"tenant_id", "workspace_id"}.issubset(columns):
                    db.execute("""INSERT INTO enterprise_service_replay_nonces(
                      credential_id,nonce_digest,consumed_at,tenant_id,workspace_id)
                      VALUES(?,?,?,?,?)""", (
                        row["credential_id"], nonce_digest, _iso(now),
                        row["tenant_id"], row["workspace_id"]))
                else:
                    db.execute("""INSERT INTO enterprise_service_replay_nonces(
                      credential_id,nonce_digest,consumed_at) VALUES(?,?,?)""",
                      (row["credential_id"], nonce_digest, _iso(now)))
            except sqlite3.IntegrityError as exc:
                raise ServiceIdentityError("service_token_replay_detected") from exc
            changed = db.execute("""UPDATE enterprise_service_credentials
              SET last_used_at=?,version=version+1
              WHERE credential_id=? AND version=? AND revoked_at IS NULL""",
              (_iso(now), row["credential_id"], row["version"])).rowcount
            if changed != 1:
                raise ServiceIdentityError("service_credential_concurrent_update")
        identity = VerifiedIdentity(
            principal_id=f"service:{row['service_name']}",
            principal_type=PrincipalType.SERVICE,
            tenant_id=row["tenant_id"], workspace_id=row["workspace_id"],
            roles=tuple(json.loads(row["roles_json"])),
            authn_method="local_service_credential_v1",
            issued_at=_dt(row["created_at"]), expires_at=_dt(row["expires_at"]),
            session_id=None, token_id=row["jti"], is_development_identity=False,
            security_version=int(row["key_version"]),
        )
        return self.resolver.resolve_verified(identity, correlation_id=correlation_id)

    def revoke(self, credential_id: str, reason: str) -> bool:
        with self._lock, self.connect() as db:
            return bool(db.execute("""UPDATE enterprise_service_credentials
              SET revoked_at=?,revocation_reason=?,version=version+1
              WHERE credential_id=? AND revoked_at IS NULL""",
              (_iso(self.clock()), str(reason)[:256], credential_id)).rowcount)
