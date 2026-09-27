"""Append-only Scheduler intent to authoritative execution attribution.

The service never derives ``actual_channel`` from a Scheduler recommendation.
Only a caller explicitly identified as a trusted execution or platform-log
correlation layer may append that observation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext, PrincipalType
from backend.tenant_security import TenantScope


TRUSTED_SOURCES = {"trusted_execution_layer", "platform_log_correlation"}
SENSITIVE_EXACT_KEYS = {
    "api_key", "apikey", "authorization", "cookie", "cookies", "password",
    "secret", "token", "prompt", "response_body", "ip", "ip_address",
    "client_ip", "remote_ip", "access_token", "refresh_token", "id_token",
    "session_token", "credential", "credentials",
}


def _is_sensitive_key(value: Any) -> bool:
    """Classify credential/content keys without rejecting safe metric names.

    Substring matching treated legitimate fields such as ``adapter_receipt``
    (contains ``ip``) and ``input_tokens`` as secrets.  Security checks remain
    fail-closed for explicit credential, address, prompt and response-body
    fields while allowing the token counts and receipt IDs required for an
    explainable Scheduler decision.
    """

    key = str(value).strip().lower().replace("-", "_")
    if key in SENSITIVE_EXACT_KEYS:
        return True
    return (
        key.startswith(("authorization_", "cookie_", "password_", "api_key_"))
        or key.endswith(("_password", "_secret", "_credential", "_api_key",
                         "_access_token", "_refresh_token", "_session_token",
                         "_ip", "_ip_address", "_response_body"))
    )


class AttributionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reject_sensitive(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_sensitive_key(key):
                raise AttributionError("sensitive_attribution_field_rejected")
            _reject_sensitive(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive(item)


class SchedulerAttributionService:
    def __init__(self, path: str | Path, *,
                 authorization_service: AuthorizationService | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.authorization_service = authorization_service
        self._migrate()

    def _scope(self, principal: PrincipalContext | None, permission: str,
               *, scheduler_only: bool = False) -> TenantScope:
        if self.authorization_service is None:
            return TenantScope.local_development()
        verified = self.authorization_service.authorize(principal, permission)
        if scheduler_only and (verified.principal_type is not PrincipalType.SERVICE
                               or "scheduler_service" not in verified.roles):
            raise PermissionError("scheduler_service_identity_required")
        return TenantScope(verified.tenant_id, verified.workspace_id)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self):
        """Commit/rollback and always release the SQLite file handle.

        ``sqlite3.Connection``'s own context manager controls transactions but
        deliberately does not close the connection.  Explicit closure matters
        for short-lived benchmark databases on Windows, where an open handle
        prevents deterministic cleanup and replay.
        """
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _migrate(self) -> None:
        with self._connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS scheduler_attribution_decisions(
              decision_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              request_id TEXT NOT NULL, selected_target_id TEXT,
              selected_channel_id TEXT, model_id TEXT NOT NULL,
              policy_version TEXT, metric_snapshot_ids_json TEXT NOT NULL,
              confidence_snapshot_ids_json TEXT NOT NULL,
              downstream_correlation_id TEXT, decision_json TEXT NOT NULL,
              decision_sha256 TEXT NOT NULL, created_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1');
            CREATE TABLE IF NOT EXISTS scheduler_execution_attributions(
              event_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
              decision_id TEXT NOT NULL, request_id TEXT NOT NULL,
              downstream_correlation_id TEXT NOT NULL,
              authoritative_actual_channel TEXT NOT NULL,
              execution_result TEXT NOT NULL, source_type TEXT NOT NULL,
              observed_at TEXT NOT NULL, event_sha256 TEXT NOT NULL,
              created_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1');
            CREATE INDEX IF NOT EXISTS idx_scheduler_attr_request
              ON scheduler_attribution_decisions(request_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_execution_attr_decision
              ON scheduler_execution_attributions(decision_id,observed_at);
            """)

    def record_scheduler_decision(self, decision: Mapping[str, Any], *,
                                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "scheduler.decide", scheduler_only=True)
        _reject_sensitive(decision)
        required = ("decision_id", "run_id", "request_id", "requested_model")
        if any(not str(decision.get(key) or "").strip() for key in required):
            raise AttributionError("scheduler_decision_identity_missing")
        safe = {
            "decision_id": str(decision["decision_id"]),
            "run_id": str(decision["run_id"]),
            "request_id": str(decision["request_id"]),
            "selected_target_id": decision.get("selected_target_id"),
            "selected_channel_id": decision.get("selected_channel_id"),
            "model_id": str(decision["requested_model"]),
            "policy_version": decision.get("decision_policy_version"),
            "metric_snapshot_ids": list(decision.get("metric_snapshot_ids") or []),
            "confidence_snapshot_ids": list(decision.get("confidence_snapshot_ids") or []),
            "downstream_correlation_id": decision.get("downstream_request_correlation_id"),
            "execution_status": decision.get("execution_status"),
            "executed_candidate_id": decision.get("executed_candidate_id"),
            "executed_channel_id": decision.get("executed_channel_id"),
            "authoritative_actual_channel": None,
        }
        encoded = _canonical(safe)
        digest = hashlib.sha256(encoded.encode()).hexdigest().upper()
        with self._connection() as db:
            existing = db.execute(
                "SELECT decision_sha256 FROM scheduler_attribution_decisions WHERE decision_id=? "
                "AND tenant_id=? AND workspace_id=?",
                (safe["decision_id"], *scope.sql_parameters())).fetchone()
            if existing:
                if existing["decision_sha256"] != digest:
                    raise AttributionError("scheduler_decision_conflict")
                return self.chain(safe["decision_id"], principal=principal)
            db.execute("""INSERT INTO scheduler_attribution_decisions(
              decision_id,run_id,request_id,selected_target_id,selected_channel_id,
              model_id,policy_version,metric_snapshot_ids_json,
              confidence_snapshot_ids_json,downstream_correlation_id,decision_json,
              decision_sha256,created_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                safe["decision_id"], safe["run_id"], safe["request_id"],
                safe["selected_target_id"], safe["selected_channel_id"], safe["model_id"],
                safe["policy_version"], _canonical({"ids": safe["metric_snapshot_ids"]}),
                _canonical({"ids": safe["confidence_snapshot_ids"]}),
                safe["downstream_correlation_id"], encoded, digest, _now(),
                *scope.sql_parameters()))
        return self.chain(safe["decision_id"], principal=principal)

    def record_execution_attribution(self, event: Mapping[str, Any], *,
                                     principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "scheduler.execute", scheduler_only=True)
        _reject_sensitive(event)
        source = str(event.get("source_type") or "")
        if source not in TRUSTED_SOURCES:
            raise AttributionError("untrusted_execution_attribution_source")
        required = ("decision_id", "request_id", "downstream_correlation_id",
                    "authoritative_actual_channel", "execution_result", "observed_at",
                    "idempotency_key")
        if any(not str(event.get(key) or "").strip() for key in required):
            raise AttributionError("execution_attribution_identity_missing")
        safe = {key: str(event[key]).strip() for key in required}
        safe["source_type"] = source
        encoded = _canonical(safe)
        digest = hashlib.sha256(encoded.encode()).hexdigest().upper()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT event_sha256 FROM scheduler_execution_attributions WHERE idempotency_key=? "
                "AND tenant_id=? AND workspace_id=?",
                (safe["idempotency_key"], *scope.sql_parameters())).fetchone()
            if duplicate:
                if duplicate["event_sha256"] != digest:
                    raise AttributionError("execution_attribution_idempotency_conflict")
                return self.chain(safe["decision_id"], principal=principal)
            conflicts = db.execute("""SELECT authoritative_actual_channel,
              downstream_correlation_id FROM scheduler_execution_attributions
              WHERE decision_id=? AND tenant_id=? AND workspace_id=?""",
              (safe["decision_id"], *scope.sql_parameters())).fetchall()
            if any(row["authoritative_actual_channel"] != safe["authoritative_actual_channel"]
                   or row["downstream_correlation_id"] != safe["downstream_correlation_id"]
                   for row in conflicts):
                raise AttributionError("conflicting_authoritative_attribution")
            db.execute("""INSERT INTO scheduler_execution_attributions(
              event_id,idempotency_key,decision_id,request_id,downstream_correlation_id,
              authoritative_actual_channel,execution_result,source_type,observed_at,
              event_sha256,created_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                "ATTR-" + uuid.uuid4().hex.upper(), safe["idempotency_key"],
                safe["decision_id"], safe["request_id"], safe["downstream_correlation_id"],
                safe["authoritative_actual_channel"], safe["execution_result"], source,
                safe["observed_at"], digest, _now(), *scope.sql_parameters()))
        return self.chain(safe["decision_id"], principal=principal)

    def chain(self, decision_id: str, *,
              principal: PrincipalContext | None = None) -> dict[str, Any]:
        if (self.authorization_service is not None and principal is not None
                and principal.principal_type is PrincipalType.SERVICE
                and "scheduler_service" in principal.roles):
            scope = self._scope(
                principal, "scheduler.decide", scheduler_only=True)
        else:
            scope = self._scope(principal, "decision.read")
        with self._connection() as db:
            decision = db.execute(
                "SELECT * FROM scheduler_attribution_decisions WHERE decision_id=? "
                "AND tenant_id=? AND workspace_id=?",
                (decision_id, *scope.sql_parameters())).fetchone()
            events = db.execute("""SELECT event_id,idempotency_key,decision_id,
              request_id,downstream_correlation_id,authoritative_actual_channel,
              execution_result,source_type,observed_at,event_sha256,created_at
              FROM scheduler_execution_attributions WHERE decision_id=? AND tenant_id=?
              AND workspace_id=? ORDER BY observed_at,event_id""",
              (decision_id, *scope.sql_parameters())).fetchall()
        parsed = json.loads(decision["decision_json"]) if decision else None
        actual = events[0]["authoritative_actual_channel"] if events else None
        if decision is None:
            status = "pending_scheduler_decision"
        elif not events:
            status = "authoritative_execution_attribution_missing"
        else:
            status = "authoritatively_attributed"
        return {
            "decision_id": decision_id, "status": status,
            "scheduler_decision": parsed,
            "authoritative_actual_channel": actual,
            "execution_events": [dict(row) for row in events],
            "event_count": len(events),
        }

    def list_chains(self, *, limit: int = 100,
                    principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "decision.read")
        limit = max(1, min(int(limit), 500))
        with self._connection() as db:
            ids = [row[0] for row in db.execute(
                "SELECT decision_id FROM scheduler_attribution_decisions WHERE tenant_id=? "
                "AND workspace_id=? ORDER BY created_at DESC LIMIT ?",
                (*scope.sql_parameters(), limit)).fetchall()]
        return {"status": "ready", "items": [self.chain(value, principal=principal) for value in ids],
                "total": len(ids), "network_called": False}
