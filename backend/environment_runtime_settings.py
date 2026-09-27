"""Operator-confirmed runtime settings layered over the versioned registry."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext
from backend.tenant_security import TenantScope
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from backend.platform_environments import load_platform_environments

LOG_PAGE_PREVIEW_TTL_SECONDS = 600
LOG_PAGE_HOST = "weimeta.ai"
DOMESTIC_UAT_LOG_PAGE_URL = "https://uat.weimeta.cn/console/billing/logs"
LOG_PAGE_SETTING_VERSION = "environment_log_page_confirmation_v1"
EXECUTION_STATE_SETTING_VERSION = "environment_execution_state_v1"
SECRET_QUERY_MARKERS = (
    "api_key", "apikey", "authorization", "auth", "token", "cookie",
    "session", "signature", "secret", "password", "credential",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_overseas_log_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("overseas_log_page_invalid")
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https" or host != LOG_PAGE_HOST or parsed.port is not None
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("overseas_log_page_invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("overseas_log_page_invalid")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    for key, _ in query:
        normalized_key = key.casefold().replace("-", "_")
        if any(marker in normalized_key for marker in SECRET_QUERY_MARKERS):
            raise ValueError("overseas_log_page_invalid")
    path = parsed.path or "/"
    safe_query = urlencode(query, doseq=True)
    return urlunsplit(("https", LOG_PAGE_HOST, path, safe_query, ""))


def normalize_domestic_uat_log_url(value: Any) -> str:
    """Accept only the reviewed domestic-UAT billing-log page."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("domestic_uat_log_page_invalid")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold().rstrip(".") != "uat.weimeta.cn"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/console/billing/logs"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("domestic_uat_log_page_invalid")
    return DOMESTIC_UAT_LOG_PAGE_URL


class EnvironmentRuntimeSettings:
    requires_domestic_confirmation = True

    def __init__(self, path: Path, preview_ttl_seconds: int = LOG_PAGE_PREVIEW_TTL_SECONDS,
                 authorization: AuthorizationService | None = None):
        self.path = path
        self.preview_ttl_seconds = preview_ttl_seconds
        self._lock = threading.Lock()
        self._previews: dict[str, tuple[float, dict[str, Any]]] = {}
        self.authorization = authorization
        self._init()

    def _scope(self, principal: PrincipalContext | None, permission: str) -> TenantScope:
        if self.authorization is None:
            if isinstance(principal, PrincipalContext) and principal.is_verified:
                return TenantScope(principal.tenant_id, principal.workspace_id)
            return TenantScope.local_development()
        try:
            verified = self.authorization.authorize(principal, permission)
        except PermissionError as exc:
            raise ValueError(str(exc)) from exc
        return TenantScope(verified.tenant_id, verified.workspace_id)

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS environment_runtime_settings(
              setting_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL,
              setting_name TEXT NOT NULL, setting_value TEXT NOT NULL,
              validation_status TEXT NOT NULL, source_type TEXT NOT NULL,
              validation_id TEXT NOT NULL, confirmed_by TEXT NOT NULL,
              confirmed_at TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, active INTEGER NOT NULL,
              value_sha256 TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_environment_runtime_setting_active
              ON environment_runtime_settings(environment_id,setting_name)
              WHERE active=1;
            """)
            columns = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(environment_runtime_settings)")}
            if "setting_version" not in columns:
                db.execute(
                    "ALTER TABLE environment_runtime_settings "
                    "ADD COLUMN setting_version TEXT NOT NULL DEFAULT "
                    "'environment_log_page_confirmation_v1'")
            local = TenantScope.local_development()
            for column,value in (("tenant_id",local.tenant_id),("workspace_id",local.workspace_id)):
                if column not in columns:
                    db.execute(f"ALTER TABLE environment_runtime_settings ADD COLUMN {column} TEXT NOT NULL DEFAULT '{value}'")
            db.execute("DROP INDEX IF EXISTS uq_environment_runtime_setting_active")
            db.execute("""CREATE UNIQUE INDEX uq_environment_runtime_setting_active
              ON environment_runtime_settings(tenant_id,workspace_id,environment_id,setting_name)
              WHERE active=1""")

    def preview_log_page(self, url: Any) -> dict[str, Any]:
        return self.preview_environment_log_page("overseas", url)

    def preview_environment_log_page(
        self, environment_id: str, url: Any,
    ) -> dict[str, Any]:
        if environment_id == "overseas":
            normalized = normalize_overseas_log_url(url)
            host = LOG_PAGE_HOST
            source_type = "operator_observed_browser_address"
        elif environment_id == "china_uat":
            normalized = normalize_domestic_uat_log_url(url)
            host = "uat.weimeta.cn"
            source_type = "reviewed_registry_operator_confirmation"
        else:
            raise ValueError("environment_not_supported")
        load_platform_environments().require(environment_id)
        validation_id = f"LOGURL-{uuid.uuid4().hex[:16].upper()}"
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        preview = {
            "validation_id": validation_id, "environment_id": environment_id,
            "normalized_url": normalized, "host": host, "https": True,
            "allowed_origin": f"https://{host}",
            "path": urlsplit(normalized).path,
            "setting_version": LOG_PAGE_SETTING_VERSION,
            "source_type": source_type,
            "structurally_valid": True, "validation_status": "structurally_validated",
            "live_validation_status": "not_attempted",
            "warnings": [
                "此操作只确认经过评审的只读日志页，不会访问 UAT 或启动同步。"],
            "value_sha256": digest,
        }
        with self._lock:
            self._previews[validation_id] = (time.monotonic(), preview)
        return dict(preview)

    def confirm_log_page(
        self, validation_id: str, value_sha256: str, explicit_confirmation: bool,
        confirmed_by: str,
    ) -> dict[str, Any]:
        return self.confirm_environment_log_page(
            "overseas", validation_id, value_sha256,
            explicit_confirmation, confirmed_by)

    def confirm_environment_log_page(
        self, environment_id: str, validation_id: str, value_sha256: str,
        explicit_confirmation: bool, confirmed_by: str,
        *, principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.reauthenticate")
        if not explicit_confirmation:
            raise ValueError("explicit_confirmation_required")
        with self._lock:
            entry = self._previews.pop(validation_id, None)
        if not entry or time.monotonic() - entry[0] > self.preview_ttl_seconds:
            raise ValueError("log_page_preview_expired")
        preview = entry[1]
        if value_sha256 != preview["value_sha256"]:
            raise ValueError("log_page_value_sha256_mismatch")
        if preview["environment_id"] != environment_id:
            raise ValueError("log_page_environment_mismatch")
        load_platform_environments().require(environment_id)
        now = _now()
        setting_id = f"RTS-{uuid.uuid4().hex[:16].upper()}"
        with self.connect() as db:
            previous = db.execute("""SELECT setting_id,setting_value,value_sha256
              FROM environment_runtime_settings
              WHERE environment_id=? AND setting_name='logs_page_url' AND active=1
              AND tenant_id=? AND workspace_id=?""",
              (environment_id,*scope.sql_parameters())).fetchone()
            db.execute("""UPDATE environment_runtime_settings SET active=0,updated_at=?
              WHERE environment_id=? AND setting_name='logs_page_url' AND active=1
              AND tenant_id=? AND workspace_id=?""",
              (now, environment_id,*scope.sql_parameters()))
            db.execute("""INSERT INTO environment_runtime_settings(
              setting_id,environment_id,setting_name,setting_value,validation_status,
              source_type,validation_id,confirmed_by,confirmed_at,created_at,updated_at,
              active,value_sha256,setting_version,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                setting_id, environment_id, "logs_page_url", preview["normalized_url"],
                "operator_confirmed", preview["source_type"],
                validation_id, confirmed_by, now, now, now, 1, value_sha256,
                LOG_PAGE_SETTING_VERSION,
                *scope.sql_parameters(),
            ))
            audit = {
                "setting_id": setting_id, "setting_name": "logs_page_url",
                "validation_id": validation_id, "value_sha256": value_sha256,
                "previous_setting_id": previous["setting_id"] if previous else None,
                "amendment": bool(previous),
            }
            db.execute("""INSERT INTO audit_events(
              event_id,execution_id,event_type,created_at,actor,details,environment_id)
              VALUES(?,?,?,?,?,?,?)""", (
                str(uuid.uuid4()), None, "environment_runtime_setting_confirmed",
                now, confirmed_by, json.dumps(audit, sort_keys=True), environment_id,
            ))
        return self.status(environment_id, principal=principal,
                           permission="collector.session.reauthenticate")

    def active(self, environment_id: str, setting_name: str, *,
               principal: PrincipalContext | None = None,
               permission: str = "collector.session.read") -> dict[str, Any] | None:
        scope = self._scope(principal, permission)
        with self.connect() as db:
            row = db.execute("""SELECT * FROM environment_runtime_settings
              WHERE environment_id=? AND setting_name=? AND active=1
              AND tenant_id=? AND workspace_id=?""",
              (environment_id, setting_name,*scope.sql_parameters())).fetchone()
        return dict(row) if row else None

    def execution_enabled(
        self, environment_id: str, *, principal: PrincipalContext | None = None,
    ) -> bool | None:
        """Return the operator-saved environment switch, or None when unset."""
        state = self.execution_state(environment_id, principal=principal)
        return None if state is None else bool(state["enabled"])

    def execution_state(
        self, environment_id: str, *, principal: PrincipalContext | None = None,
    ) -> dict[str, Any] | None:
        """Return only the persisted environment switch; task state is never joined."""
        row = self.active(
            environment_id, "real_execution_enabled", principal=principal,
            permission="decision.read",
        )
        if row is None:
            return None
        value = str(row["setting_value"]).strip().casefold()
        if value == "true":
            enabled = True
        elif value == "false":
            enabled = False
        else:
            raise ValueError("environment_execution_state_invalid")
        return {
            "environment_id": environment_id,
            "enabled": enabled,
            "state_source": "operator_runtime_toggle",
            "updated_at": row["updated_at"],
            "updated_by": row["confirmed_by"],
            "setting_version": row["setting_version"],
        }

    def set_execution_enabled(
        self, environment_id: str, enabled: bool, changed_by: str, *,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        """Persist the direct operator switch without creating an approval task."""
        if environment_id != "china_uat":
            raise ValueError("environment_execution_toggle_not_supported")
        if type(enabled) is not bool:
            raise ValueError("environment_execution_enabled_boolean_required")
        load_platform_environments().require(environment_id)
        scope = self._scope(principal, "uat.execution.manage")
        now = _now()
        value = "true" if enabled else "false"
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        setting_id = f"RTS-{uuid.uuid4().hex[:16].upper()}"
        with self.connect() as db:
            previous = db.execute("""SELECT setting_id,setting_value FROM environment_runtime_settings
              WHERE environment_id=? AND setting_name='real_execution_enabled' AND active=1
              AND tenant_id=? AND workspace_id=?""",
              (environment_id, *scope.sql_parameters())).fetchone()
            db.execute("""UPDATE environment_runtime_settings SET active=0,updated_at=?
              WHERE environment_id=? AND setting_name='real_execution_enabled' AND active=1
              AND tenant_id=? AND workspace_id=?""",
              (now, environment_id, *scope.sql_parameters()))
            db.execute("""INSERT INTO environment_runtime_settings(
              setting_id,environment_id,setting_name,setting_value,validation_status,
              source_type,validation_id,confirmed_by,confirmed_at,created_at,updated_at,
              active,value_sha256,setting_version,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                setting_id, environment_id, "real_execution_enabled", value,
                "enabled" if enabled else "disabled", "operator_runtime_toggle",
                setting_id, changed_by, now, now, now, 1, digest,
                EXECUTION_STATE_SETTING_VERSION, *scope.sql_parameters(),
            ))
            db.execute("""INSERT INTO audit_events(
              event_id,execution_id,event_type,created_at,actor,details,environment_id,
              tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?)""", (
                str(uuid.uuid4()), None, "environment_execution_state_changed", now,
                changed_by, json.dumps({
                    "setting_id": setting_id,
                    "enabled": enabled,
                    "previous_enabled": (
                        str(previous["setting_value"]).casefold() == "true"
                        if previous else None
                    ),
                    "setting_version": EXECUTION_STATE_SETTING_VERSION,
                }, sort_keys=True), environment_id, *scope.sql_parameters(),
            ))
        return {
            "environment_id": environment_id,
            "enabled": enabled,
            "state_source": "operator_runtime_toggle",
            "updated_at": now,
            "updated_by": changed_by,
            "setting_version": EXECUTION_STATE_SETTING_VERSION,
        }

    def disable_log_page(self, confirmed_by: str) -> dict[str, Any]:
        now = _now()
        with self.connect() as db:
            current = db.execute("""SELECT setting_id,value_sha256 FROM environment_runtime_settings
              WHERE environment_id='overseas' AND setting_name='logs_page_url' AND active=1""").fetchone()
            if current:
                db.execute("""UPDATE environment_runtime_settings SET active=0,
                  validation_status='disabled',updated_at=?
                  WHERE setting_id=?""", (now, current["setting_id"]))
                db.execute("""INSERT INTO audit_events(
                  event_id,execution_id,event_type,created_at,actor,details,environment_id)
                  VALUES(?,?,?,?,?,?,?)""", (
                    str(uuid.uuid4()), None, "environment_runtime_setting_disabled",
                    now, confirmed_by, json.dumps({
                        "setting_id": current["setting_id"],
                        "value_sha256": current["value_sha256"],
                    }, sort_keys=True), "overseas",
                ))
        return self.status("overseas")

    def mark_live_validation(
        self, recognized: bool, records_observed: bool, record_count: int,
        collection_id: str | None,
        *, principal: PrincipalContext | None = None,
    ) -> None:
        scope = self._scope(principal, "evidence.import")
        if not recognized:
            return
        now = _now()
        with self.connect() as db:
            row = db.execute("""SELECT setting_id FROM environment_runtime_settings
              WHERE environment_id='overseas' AND setting_name='logs_page_url' AND active=1
              AND tenant_id=? AND workspace_id=?""",scope.sql_parameters()).fetchone()
            if row:
                db.execute("""UPDATE environment_runtime_settings SET
                  validation_status='browser_live_validated',updated_at=?
                  WHERE setting_id=?""", (now, row["setting_id"]))
                db.execute("""INSERT INTO audit_events(
                  event_id,execution_id,event_type,created_at,actor,details,environment_id)
                  VALUES(?,?,?,?,?,?,?)""", (
                    str(uuid.uuid4()), None, "overseas_log_page_live_validated", now,
                    "local_operator", json.dumps({
                        "collection_id": collection_id,
                        "records_observed": records_observed,
                        "record_count": record_count,
                    }, sort_keys=True), "overseas",
                ))

    def status(self, environment_id: str, *, principal: PrincipalContext | None = None,
               permission: str = "collector.session.read") -> dict[str, Any]:
        scope = self._scope(principal, permission)
        item = self.active(environment_id, "logs_page_url", principal=principal,
                           permission=permission)
        latest = None
        try:
            with self.connect() as db:
                latest = db.execute("""SELECT collection_id,collected_at,record_count
                  FROM collector_collections WHERE environment_id=?
                  AND tenant_id=? AND workspace_id=?
                  ORDER BY collected_at DESC LIMIT 1""",
                  (environment_id,*scope.sql_parameters())).fetchone()
        except sqlite3.OperationalError:
            latest = None
        return {
            "environment_id": environment_id,
            "log_page_status": item["validation_status"] if item else "pending_confirmation",
            "logs_page_url": item["setting_value"] if item else None,
            "source_type": item["source_type"] if item else None,
            "structural_validation_status": "structurally_validated" if item else "pending_confirmation",
            "operator_confirmed_at": item["confirmed_at"] if item else None,
            "browser_live_validation_status": (
                "browser_live_validated"
                if item and item["validation_status"] == "browser_live_validated"
                else "not_attempted"
            ),
            "latest_collection_at": latest["collected_at"] if latest else None,
            "latest_record_count": latest["record_count"] if latest else None,
            "latest_collection_id": latest["collection_id"] if latest else None,
            "value_sha256": item["value_sha256"] if item else None,
            "setting_version": (
                item.get("setting_version") if item else
                LOG_PAGE_SETTING_VERSION),
            "allowed_origin": (
                f"https://{urlsplit(item['setting_value']).hostname}"
                if item else None),
            "log_page_path": (
                urlsplit(item["setting_value"]).path if item else None),
            "blocking_reason": None if item else "log_page_not_confirmed",
        }


ERROR_CODE_COMPATIBILITY = {
    "overseas_endpoint_unconfirmed": "environment_configuration_incomplete",
    "overseas_execution_not_authorized": "overseas_completion_not_yet_authorized",
}


def canonical_error_code(code: str) -> str:
    return ERROR_CODE_COMPATIBILITY.get(code, code)
