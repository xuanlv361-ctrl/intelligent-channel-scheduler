from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.platform_environments import load_platform_environments
from backend.log_schema_adapters import (
    SchemaAdapterRegistry, SchemaObservationError, observe_schema,
)
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext
from backend.tenant_security import TenantScope

ACTIVE_STATES = {
    "browser_starting", "waiting_for_manual_login",
    "waiting_for_operator_confirmation", "syncing",
    "created", "decrypting_session", "launching_headless_browser",
    "validating_authentication", "synchronizing",
}
TERMINAL_STATES = {
    "stopped", "completed", "blocked", "failed", "session_expired",
    "persistent_session_expired", "reauthentication_required",
}
STOPPING_STATES = {"stopping"}
ALLOWED_STATES = {"idle", *ACTIVE_STATES, *STOPPING_STATES, *TERMINAL_STATES}
SOURCE_TYPES = {
    "china_uat": "measured_uat_realtime_browser_sync",
    "overseas": "measured_overseas_realtime_browser_sync",
}
ERROR_NEXT_ACTIONS = {
    "log_sync_browser_launch_failed": "确认本机已安装 Chromium，并检查浏览器进程启动权限后重试。",
    "log_sync_session_expired": "重新启动同步并完成一次人工登录确认。",
    "persistent_headless_launch_failed": "检查本机 Chromium 与 DPAPI 会话状态，然后重新认证。",
    "persistent_session_reauthentication_required": "清除失效的本机会话并重新登录配对。",
    "persistent_session_requires_unsupported_web_storage": "继续使用临时模式，等待独立评审 Web Storage 的最小安全范围。",
    "schema_mapping_required": "由管理员提供脱敏字段映射证据后再继续同步。",
}
SENSITIVE_KEYS = {
    "authorization", "cookie", "set-cookie", "set_cookie", "password", "api_key", "apikey",
    "access_token", "refresh_token", "prompt", "messages", "request_body",
    "response_body", "localstorage", "sessionstorage", "storage_state",
}
NORMALIZED_FIELDS = (
    "platform_log_id", "environment_id", "observed_at", "platform_created_at",
    "request_id", "response_id", "provider_request_id", "provider_response_id",
    "provider_trace_id", "client_correlation_id", "decision_id",
    "requested_model", "actual_model",
    "api_key_display_name", "stream", "http_status", "input_tokens",
    "output_tokens", "cached_tokens", "total_tokens", "ttft_ms", "latency_ms",
    "actual_cost", "currency", "actual_channel_id", "actual_channel_name",
    "actual_cost_status", "actual_channel_status",
    "raw_quota", "quota_unit", "quota_conversion_rate",
    "converted_amount", "cost_precision", "rounding_mode",
    "cost_source_reference", "billing_scope",
    "finish_reason", "result", "error_category", "source_type", "collection_id",
    "sync_job_id", "evidence_sha256", "is_mock", "estimated_cost",
    "estimated_cost_source", "actual_cost_source",
    "original_safe_timestamp_text", "normalized_utc_timestamp",
)
ALIASES = {
    "platform_log_id": ("platform_log_id", "log_id", "id"),
    "platform_created_at": ("platform_created_at", "created_at", "timestamp"),
    # Provider billing identifiers deliberately do not populate the local
    # request_id/response_id namespace.  A billing row's request_id is a
    # Provider identifier and must never be compared with our REQ-* value.
    "provider_request_id": ("provider_request_id", "request_id", "requestId"),
    "provider_response_id": ("provider_response_id", "response_id", "responseId"),
    "provider_trace_id": ("provider_trace_id", "trace_id", "traceId"),
    "client_correlation_id": (
        "client_correlation_id", "client_request_id", "clientRequestId"),
    "decision_id": ("decision_id", "decisionId"),
    "requested_model": ("requested_model", "model_name"),
    "actual_model": ("actual_model", "model", "model_name"),
    "api_key_display_name": ("api_key_display_name", "key_name", "token_name"),
    "stream": ("stream", "is_stream"),
    "http_status": ("http_status", "status_code"),
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cached_tokens": ("cached_tokens",),
    "total_tokens": ("total_tokens",),
    "ttft_ms": ("ttft_ms", "first_token_ms"),
    "latency_ms": ("latency_ms", "elapsed_ms", "elapsed_time", "use_time"),
    "actual_cost": ("actual_cost", "cost", "cost_cny"),
    "currency": ("currency",),
    "actual_channel_id": ("actual_channel_id", "channel_id"),
    "actual_channel_name": ("actual_channel_name", "channel_name"),
    "finish_reason": ("finish_reason",),
    "result": ("result",),
    "error_category": ("error_category",),
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_url(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    if not host:
        return "[invalid-url]"
    try:
        port = parsed.port
    except ValueError:
        return "[invalid-url]"
    authority = host if port in {None, 443} else f"{host}:{port}"
    return urlunsplit((parsed.scheme.casefold(), authority, parsed.path, "", ""))


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in SENSITIVE_KEYS:
                continue
            result[str(key)] = sanitize(item)
        return result
    if isinstance(value, list):
        return [sanitize(item) for item in value[:1000]]
    if isinstance(value, str):
        text = value.replace("\r", " ").replace("\n", " ")
        if len(text) > 1000:
            return text[:1000] + "…"
        if "bearer " in text.casefold():
            return "[REDACTED]"
        return text
    return value


def schema_summary(payload: Any) -> dict[str, Any]:
    records = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        for key in ("data", "items", "records", "list"):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
        if not records:
            records = [payload]
    keys = sorted({str(key) for row in records[:20] if isinstance(row, dict) for key in row})
    return {"root_type": type(payload).__name__, "record_count": len(records), "observed_keys": keys}


class RealtimeLogSyncError(ValueError):
    pass


class RealtimeLogSyncService:
    def __init__(self, database_path: Path, execution_path: Path,
                 runtime_settings: Any | None = None,
                 cost_tolerance: float = 0.000001,
                 maximum_range_hours: int = 168,
                 clock_skew_seconds: int = 300,
                 schema_registry_path: Path | None = None,
                 authorization: AuthorizationService | None = None):
        self.database_path = Path(database_path)
        self.execution_path = Path(execution_path)
        self.runtime_settings = runtime_settings
        self.cost_tolerance = max(0.0, float(cost_tolerance))
        self.maximum_range = timedelta(hours=max(1, int(maximum_range_hours)))
        self.clock_skew = timedelta(seconds=max(0, int(clock_skew_seconds)))
        self.authorization = authorization
        self.schema_registry = SchemaAdapterRegistry(
            schema_registry_path or Path(__file__).resolve().parents[1] /
            "config" / "domestic_uat_log_schema_registry_v1.json")
        self._init()

    def _scope(self, principal: PrincipalContext | None,
               permission: str) -> TenantScope:
        if self.authorization is None:
            if isinstance(principal, PrincipalContext) and principal.is_verified:
                return TenantScope(principal.tenant_id, principal.workspace_id)
            return TenantScope.local_development()
        try:
            verified = self.authorization.authorize(principal, permission)
        except PermissionError as exc:
            raise RealtimeLogSyncError(str(exc)) from exc
        return TenantScope(verified.tenant_id, verified.workspace_id)

    def connect(self):
        db = sqlite3.connect(self.database_path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _init(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS realtime_log_sync_jobs(
              sync_job_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL,
              state TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT,
              stopped_at TEXT, last_poll_at TEXT, last_successful_poll_at TEXT,
              last_observed_log_time TEXT, safe_log_page_url TEXT NOT NULL,
              current_safe_url TEXT,
              collected_count INTEGER NOT NULL DEFAULT 0,
              inserted_count INTEGER NOT NULL DEFAULT 0,
              duplicate_count INTEGER NOT NULL DEFAULT 0,
              rejected_count INTEGER NOT NULL DEFAULT 0,
              out_of_range_count INTEGER NOT NULL DEFAULT 0,
              missing_timestamp_count INTEGER NOT NULL DEFAULT 0,
              correlated_count INTEGER NOT NULL DEFAULT 0,
              ambiguous_count INTEGER NOT NULL DEFAULT 0,
              error_code TEXT, safe_error_message TEXT,
              stop_reason TEXT, error_at TEXT, suggested_next_action TEXT,
              browser_context_active INTEGER NOT NULL DEFAULT 0,
              cookies_persisted INTEGER NOT NULL DEFAULT 0,
              credentials_persisted INTEGER NOT NULL DEFAULT 0,
              network_called INTEGER NOT NULL DEFAULT 0,
              poll_interval_seconds INTEGER NOT NULL,
              maximum_records INTEGER NOT NULL, source_type TEXT NOT NULL,
              http_read_count INTEGER NOT NULL DEFAULT 0,
              maximum_http_reads INTEGER NOT NULL DEFAULT 20,
              maximum_records_observed INTEGER NOT NULL DEFAULT 500,
              maximum_records_accepted INTEGER NOT NULL DEFAULT 500,
              maximum_elapsed_seconds INTEGER NOT NULL DEFAULT 600,
              elapsed_seconds INTEGER NOT NULL DEFAULT 0,
              periodic_polling INTEGER NOT NULL DEFAULT 1,
              page_size INTEGER NOT NULL DEFAULT 100,
              maximum_pages INTEGER NOT NULL DEFAULT 10,
              current_page INTEGER NOT NULL DEFAULT 1,
              pages_read INTEGER NOT NULL DEFAULT 0,
              consecutive_empty_reads INTEGER NOT NULL DEFAULT 0,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              last_response_envelope_sha256 TEXT,
              last_successful_read_at TEXT,
              source_cursor TEXT,
              watermark_utc TEXT,
              latest_metric_snapshot_id TEXT,
              latest_confidence_snapshot_id TEXT,
              provenance_manifest_sha256 TEXT,
              date_from_utc TEXT, date_to_utc TEXT, timezone TEXT,
              schema_status TEXT NOT NULL DEFAULT 'pending_observation',
              sync_mode TEXT NOT NULL DEFAULT 'temporary',
              persistent_session_id TEXT,
              allowed_hosts_json TEXT NOT NULL, worker_pid INTEGER);
            CREATE TABLE IF NOT EXISTS realtime_log_sync_cursors(
              environment_id TEXT PRIMARY KEY, cursor_kind TEXT,
              cursor_value TEXT, updated_at TEXT NOT NULL,
              sync_job_id TEXT REFERENCES realtime_log_sync_jobs(sync_job_id));
            CREATE TABLE IF NOT EXISTS realtime_log_evidence(
              evidence_sha256 TEXT PRIMARY KEY, sync_job_id TEXT NOT NULL
                REFERENCES realtime_log_sync_jobs(sync_job_id),
              environment_id TEXT NOT NULL, sanitized_payload TEXT NOT NULL,
              schema_summary TEXT NOT NULL, source_url TEXT NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS realtime_log_schema_audit(
              selection_id TEXT PRIMARY KEY,
              sync_job_id TEXT NOT NULL
                REFERENCES realtime_log_sync_jobs(sync_job_id),
              environment_id TEXT NOT NULL,
              observed_at TEXT NOT NULL,
              schema_fingerprint TEXT NOT NULL,
              schema_adapter_id TEXT,
              adapter_version TEXT,
              mapping_version TEXT,
              selection_status TEXT NOT NULL,
              candidate_record_count INTEGER,
              sensitive_field_count INTEGER NOT NULL DEFAULT 0,
              accepted_field_coverage REAL NOT NULL DEFAULT 0,
              rejected_field_count INTEGER NOT NULL DEFAULT 0,
              evidence_sha256 TEXT NOT NULL,
              source_url TEXT NOT NULL, pagination_json TEXT,
              adapter_metadata_json TEXT,
              rejection_reason TEXT);
            CREATE TABLE IF NOT EXISTS realtime_log_rejections(
              rejection_id TEXT PRIMARY KEY,
              sync_job_id TEXT NOT NULL
                REFERENCES realtime_log_sync_jobs(sync_job_id),
              evidence_sha256 TEXT NOT NULL,
              source_record_index INTEGER,
              schema_fingerprint TEXT NOT NULL,
              schema_adapter_id TEXT,
              adapter_version TEXT,
              rejection_reason TEXT NOT NULL,
              created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS realtime_log_records(
              record_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL,
              environment_id TEXT NOT NULL, platform_log_id TEXT,
              request_id TEXT, response_id TEXT,
              provider_request_id TEXT, provider_response_id TEXT,
              provider_trace_id TEXT, client_correlation_id TEXT,
              observed_at TEXT NOT NULL,
              platform_created_at TEXT, source_type TEXT NOT NULL,
              sync_job_id TEXT NOT NULL REFERENCES realtime_log_sync_jobs(sync_job_id),
              evidence_sha256 TEXT NOT NULL REFERENCES realtime_log_evidence(evidence_sha256),
              normalized_json TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, UNIQUE(environment_id,identity_key));
            CREATE TABLE IF NOT EXISTS realtime_log_correlations(
              correlation_id TEXT PRIMARY KEY, execution_id TEXT,
              record_id TEXT NOT NULL REFERENCES realtime_log_records(record_id),
              environment_id TEXT NOT NULL, state TEXT NOT NULL,
              method TEXT, matched_field TEXT, matched_value_hash TEXT,
              match_confidence TEXT, provider_log_ids TEXT, matched_at TEXT,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL,
              UNIQUE(environment_id,record_id));
            CREATE TABLE IF NOT EXISTS realtime_log_sync_events(
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,
              sync_job_id TEXT REFERENCES realtime_log_sync_jobs(sync_job_id),
              environment_id TEXT NOT NULL, event_type TEXT NOT NULL,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS realtime_log_amendments(
              amendment_id TEXT PRIMARY KEY, record_id TEXT NOT NULL
                REFERENCES realtime_log_records(record_id),
              previous_json TEXT NOT NULL, replacement_json TEXT NOT NULL,
              reason TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS provider_log_sync_batches(
              sync_batch_id TEXT PRIMARY KEY, sync_job_id TEXT NOT NULL,
              environment_id TEXT NOT NULL, source_type TEXT NOT NULL,
              started_at TEXT NOT NULL, finished_at TEXT,
              previous_watermark_json TEXT NOT NULL,
              final_watermark_json TEXT,
              initial_pending_count INTEGER NOT NULL,
              pending_created_during_sync INTEGER NOT NULL DEFAULT 0,
              provider_records_read INTEGER NOT NULL DEFAULT 0,
              provider_records_deduplicated INTEGER NOT NULL DEFAULT 0,
              matched_request_count INTEGER NOT NULL DEFAULT 0,
              unmatched_request_count INTEGER NOT NULL DEFAULT 0,
              cost_backfilled_count INTEGER NOT NULL DEFAULT 0,
              final_pending_count INTEGER,
              pages_read INTEGER NOT NULL DEFAULT 0,
              end_reason TEXT, billing_context_json TEXT,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS provider_log_sync_pages(
              sync_batch_id TEXT NOT NULL, page_number INTEGER NOT NULL,
              provider_count INTEGER NOT NULL, accepted_count INTEGER NOT NULL,
              duplicate_count INTEGER NOT NULL, response_sha256 TEXT NOT NULL,
              provider_cursor TEXT, provider_log_time TEXT, read_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,sync_batch_id,page_number));
            CREATE TABLE IF NOT EXISTS provider_cost_components(
              component_id TEXT NOT NULL, provider_log_id TEXT NOT NULL,
              request_id TEXT NOT NULL, environment_id TEXT NOT NULL,
              component_role TEXT NOT NULL, raw_quota TEXT NOT NULL,
              quota_unit TEXT NOT NULL, conversion_rate TEXT NOT NULL,
              converted_amount TEXT NOT NULL, currency TEXT NOT NULL,
              precision INTEGER NOT NULL, rounding_mode TEXT NOT NULL,
              source_reference TEXT NOT NULL, provider_log_time TEXT,
              raw_record_sha256 TEXT NOT NULL, sync_batch_id TEXT NOT NULL,
              created_at TEXT NOT NULL, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,component_id),
              UNIQUE(tenant_id,workspace_id,environment_id,provider_log_id));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(realtime_log_sync_jobs)")}
            migrations = {
                "current_safe_url": "TEXT",
                "out_of_range_count": "INTEGER NOT NULL DEFAULT 0",
                "missing_timestamp_count": "INTEGER NOT NULL DEFAULT 0",
                "date_from_utc": "TEXT",
                "date_to_utc": "TEXT",
                "timezone": "TEXT",
                "schema_status": "TEXT NOT NULL DEFAULT 'pending_observation'",
                "sync_mode": "TEXT NOT NULL DEFAULT 'temporary'",
                "persistent_session_id": "TEXT",
                "lease_generation": "INTEGER",
                "stop_reason": "TEXT",
                "error_at": "TEXT",
                "suggested_next_action": "TEXT",
                "http_read_count": "INTEGER NOT NULL DEFAULT 0",
                "maximum_http_reads": "INTEGER NOT NULL DEFAULT 20",
                "maximum_records_observed": "INTEGER NOT NULL DEFAULT 500",
                "maximum_records_accepted": "INTEGER NOT NULL DEFAULT 500",
                "maximum_elapsed_seconds": "INTEGER NOT NULL DEFAULT 600",
                "elapsed_seconds": "INTEGER NOT NULL DEFAULT 0",
                "periodic_polling": "INTEGER NOT NULL DEFAULT 1",
                "page_size": "INTEGER NOT NULL DEFAULT 100",
                "maximum_pages": "INTEGER NOT NULL DEFAULT 10",
                "current_page": "INTEGER NOT NULL DEFAULT 1",
                "pages_read": "INTEGER NOT NULL DEFAULT 0",
                "consecutive_empty_reads": "INTEGER NOT NULL DEFAULT 0",
                "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
                "last_response_envelope_sha256": "TEXT",
                "last_successful_read_at": "TEXT",
                "source_cursor": "TEXT",
                "watermark_utc": "TEXT",
                "latest_metric_snapshot_id": "TEXT",
                "latest_confidence_snapshot_id": "TEXT",
                "provenance_manifest_sha256": "TEXT",
                "service_credential_id": "TEXT",
            }
            for column, declaration in migrations.items():
                if column not in columns:
                    db.execute(
                        f"ALTER TABLE realtime_log_sync_jobs ADD COLUMN {column} {declaration}")
            tenant_tables = (
                "realtime_log_sync_jobs", "realtime_log_sync_cursors",
                "realtime_log_evidence", "realtime_log_schema_audit",
                "realtime_log_rejections", "realtime_log_records",
                "realtime_log_correlations", "realtime_log_sync_events",
                "realtime_log_amendments",
                "provider_log_sync_batches", "provider_log_sync_pages",
                "provider_cost_components",
            )
            local_scope = TenantScope.local_development()
            for table in tenant_tables:
                table_columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, value in (
                    ("tenant_id", local_scope.tenant_id),
                    ("workspace_id", local_scope.workspace_id),
                ):
                    if column not in table_columns:
                        db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{value}'")
            for table, additions in {
                "realtime_log_records": {
                    "provider_request_id": "TEXT",
                    "provider_response_id": "TEXT",
                    "provider_trace_id": "TEXT",
                    "client_correlation_id": "TEXT",
                },
                "realtime_log_correlations": {
                    "matched_field": "TEXT",
                    "matched_value_hash": "TEXT",
                    "match_confidence": "TEXT",
                    "provider_log_ids": "TEXT",
                    "matched_at": "TEXT",
                },
            }.items():
                existing = {row[1] for row in db.execute(
                    f"PRAGMA table_info({table})")}
                for name, declaration in additions.items():
                    if name not in existing:
                        db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            db.execute("""CREATE INDEX IF NOT EXISTS
              idx_realtime_provider_request
              ON realtime_log_records(tenant_id,workspace_id,provider_request_id)""")
            db.execute("""CREATE INDEX IF NOT EXISTS
              idx_realtime_provider_response
              ON realtime_log_records(tenant_id,workspace_id,provider_response_id)""")
            db.execute("""CREATE INDEX IF NOT EXISTS
              idx_realtime_provider_trace
              ON realtime_log_records(tenant_id,workspace_id,provider_trace_id)""")
            for table, additions in {
                "uat_executions": {
                    "actual_cost_amount": "TEXT", "currency": "TEXT",
                    "cost_status": "TEXT", "cost_type": "TEXT",
                    "provider_sync_batch_id": "TEXT",
                    "provider_cost_updated_at": "TEXT",
                },
                "call_attempt_logs": {
                    "cost_status": "TEXT", "cost_type": "TEXT",
                    "provider_cost_amount_exact": "TEXT",
                    "provider_sync_batch_id": "TEXT",
                },
            }.items():
                if not db.execute("""SELECT 1 FROM sqlite_master
                  WHERE type='table' AND name=?""", (table,)).fetchone():
                    continue
                existing = {row[1] for row in db.execute(
                    f"PRAGMA table_info({table})")}
                for name, declaration in additions.items():
                    if name not in existing:
                        db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            # Composite foreign keys used by the tenant-scoped cursor table
            # require an explicit matching UNIQUE parent key.  The legacy
            # global sync_job_id primary key alone is not a valid SQLite
            # parent for (tenant_id, workspace_id, sync_job_id).
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS
              uq_realtime_log_sync_jobs_enterprise_identity
              ON realtime_log_sync_jobs(tenant_id,workspace_id,sync_job_id)""")
            # SQLite reports columns in declaration order, not primary-key
            # ordinal order.  Tenant migration v2 appends the scope columns to
            # legacy tables while correctly assigning them PK ordinals 1/2.
            # Sorting by the PK ordinal avoids treating that valid v2 schema as
            # legacy and starting an unsafe second rebuild.
            cursor_pk = [row[1] for row in sorted(
                (row for row in db.execute(
                    "PRAGMA table_info(realtime_log_sync_cursors)") if int(row[5])),
                key=lambda row: int(row[5]))]
            # A failed legacy upgrade can leave only this empty, non-authoritative
            # scratch table behind.  It must not be seen as tenant-owned data by
            # the authoritative migration inventory.
            db.execute("DROP TABLE IF EXISTS realtime_log_sync_cursors_enterprise")
            if cursor_pk != ["tenant_id", "workspace_id", "environment_id"]:
                db.executescript("""
                CREATE TABLE realtime_log_sync_cursors_enterprise(
                  environment_id TEXT NOT NULL, cursor_kind TEXT,
                  cursor_value TEXT, updated_at TEXT NOT NULL,
                  sync_job_id TEXT,
                  tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,environment_id),
                  FOREIGN KEY(tenant_id,workspace_id,sync_job_id)
                    REFERENCES realtime_log_sync_jobs(
                      tenant_id,workspace_id,sync_job_id));
                INSERT INTO realtime_log_sync_cursors_enterprise(
                  environment_id,cursor_kind,cursor_value,updated_at,sync_job_id,
                  tenant_id,workspace_id)
                  SELECT environment_id,cursor_kind,cursor_value,updated_at,sync_job_id,
                         tenant_id,workspace_id FROM realtime_log_sync_cursors;
                DROP TABLE realtime_log_sync_cursors;
                ALTER TABLE realtime_log_sync_cursors_enterprise
                  RENAME TO realtime_log_sync_cursors;
                """)
            schema_columns = {
                row[1] for row in db.execute(
                    "PRAGMA table_info(realtime_log_schema_audit)")}
            for column in ("pagination_json", "adapter_metadata_json"):
                if column not in schema_columns:
                    db.execute(
                        f"ALTER TABLE realtime_log_schema_audit "
                        f"ADD COLUMN {column} TEXT")
            db.execute("DROP INDEX IF EXISTS uq_realtime_active_environment")
            db.execute("""CREATE UNIQUE INDEX uq_realtime_active_environment
              ON realtime_log_sync_jobs(tenant_id,workspace_id,environment_id)
              WHERE state IN ('browser_starting','waiting_for_manual_login',
                'waiting_for_operator_confirmation','syncing','created',
                'decrypting_session','launching_headless_browser',
                'validating_authentication','synchronizing','stopping')""")
            for table in tenant_tables:
                db.execute(f"""CREATE INDEX IF NOT EXISTS ix_{table}_enterprise_scope
                  ON {table}(tenant_id,workspace_id)""")

    def resolve_environment(self, environment_id: str) -> dict[str, Any]:
        if environment_id not in SOURCE_TYPES:
            raise RealtimeLogSyncError("log_sync_concrete_environment_required")
        environment = load_platform_environments().require(environment_id)
        if environment_id == "overseas":
            setting = self.runtime_settings.active("overseas", "logs_page_url") if self.runtime_settings else None
            log_url = setting["setting_value"] if setting else None
            if not log_url:
                raise RealtimeLogSyncError("log_sync_log_page_unconfirmed")
        else:
            log_url = environment.logs_page_url
        parsed = urlsplit(str(log_url or ""))
        allowed = set(environment.allowed_console_hosts)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed:
            raise RealtimeLogSyncError("log_sync_log_page_not_allowed")
        return {
            "environment_id": environment_id, "log_page_url": safe_url(str(log_url)),
            "console_url": environment.console_base_url,
            "allowed_hosts": sorted(allowed), "source_type": SOURCE_TYPES[environment_id],
        }

    def event(
        self, job_id: str | None, environment_id: str, event_type: str,
        details: dict[str, Any] | None = None,
        lease_generation: int | None = None,
        *, principal: PrincipalContext | None = None,
        permission: str = "evidence.import",
    ):
        scope = self._scope(principal, permission)
        safe = sanitize(details or {})
        with self.connect() as db:
            if lease_generation is not None:
                db.execute("BEGIN IMMEDIATE")
                self._assert_worker_fence(
                    db, str(job_id), int(lease_generation))
            db.execute("""INSERT INTO realtime_log_sync_events(
              sync_job_id,environment_id,event_type,created_at,details_json,
              tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?)""", (job_id, environment_id, event_type, utcnow(),
                                      json.dumps(safe, ensure_ascii=False, sort_keys=True),
                                      *scope.sql_parameters()))

    @staticmethod
    def _parse_explicit_timestamp(value: str, field: str) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise RealtimeLogSyncError(f"log_sync_{field}_required")
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise RealtimeLogSyncError(f"log_sync_{field}_invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RealtimeLogSyncError(f"log_sync_{field}_timezone_required")
        return parsed.astimezone(timezone.utc)

    def normalize_range(self, date_from: str, date_to: str,
                        timezone_name: str) -> tuple[str, str, str]:
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise RealtimeLogSyncError("log_sync_timezone_invalid") from exc
        start = self._parse_explicit_timestamp(date_from, "date_from")
        end = self._parse_explicit_timestamp(date_to, "date_to")
        if start >= end:
            raise RealtimeLogSyncError("log_sync_range_order_invalid")
        if end - start > self.maximum_range:
            raise RealtimeLogSyncError("log_sync_range_too_large")
        if end > datetime.now(timezone.utc) + self.clock_skew:
            raise RealtimeLogSyncError("log_sync_date_to_in_future")
        return start.isoformat(), end.isoformat(), timezone_name

    def create_job(self, environment_id: str, date_from: str, date_to: str,
                   timezone_name: str, poll_interval_seconds: int = 7,
                   maximum_records: int = 500,
                   maximum_http_reads: int = 20,
                   maximum_records_observed: int | None = None,
                   maximum_records_accepted: int | None = None,
                   maximum_elapsed_seconds: int = 600,
                   periodic_polling: bool = True,
                   page_size: int = 100,
                   maximum_pages: int = 10,
                   principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.oneshot.execute")
        if not 3 <= poll_interval_seconds <= 30:
            raise RealtimeLogSyncError("log_sync_poll_interval_invalid")
        if not 1 <= maximum_records <= 1000:
            raise RealtimeLogSyncError("log_sync_maximum_records_invalid")
        observed_limit = (
            maximum_records if maximum_records_observed is None
            else int(maximum_records_observed))
        accepted_limit = (
            maximum_records if maximum_records_accepted is None
            else int(maximum_records_accepted))
        if not 1 <= int(maximum_http_reads) <= 50:
            raise RealtimeLogSyncError("log_sync_maximum_http_reads_invalid")
        if not 1 <= observed_limit <= 1000:
            raise RealtimeLogSyncError(
                "log_sync_maximum_records_observed_invalid")
        if not 1 <= accepted_limit <= observed_limit:
            raise RealtimeLogSyncError(
                "log_sync_maximum_records_accepted_invalid")
        if not 10 <= int(maximum_elapsed_seconds) <= 600:
            raise RealtimeLogSyncError("log_sync_maximum_elapsed_invalid")
        if not isinstance(periodic_polling, bool):
            raise RealtimeLogSyncError("log_sync_periodic_polling_invalid")
        if not 1 <= int(page_size) <= 100:
            raise RealtimeLogSyncError("log_sync_page_size_invalid")
        if not 1 <= int(maximum_pages) <= 50:
            raise RealtimeLogSyncError("log_sync_maximum_pages_invalid")
        if not periodic_polling and int(maximum_http_reads) != 1:
            raise RealtimeLogSyncError("log_sync_one_shot_requires_one_http_read")
        date_from_utc, date_to_utc, normalized_timezone = self.normalize_range(
            date_from, date_to, timezone_name)
        contract = self.resolve_environment(environment_id)
        job_id = f"LSYNC-{uuid.uuid4().hex[:20].upper()}"
        try:
            with self.connect() as db:
                db.execute("""INSERT INTO realtime_log_sync_jobs(
                  sync_job_id,environment_id,state,created_at,safe_log_page_url,
                  poll_interval_seconds,maximum_records,source_type,date_from_utc,
                  date_to_utc,timezone,allowed_hosts_json,maximum_http_reads,
                  maximum_records_observed,maximum_records_accepted,
                  maximum_elapsed_seconds,periodic_polling,page_size,
                  maximum_pages,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    job_id, environment_id, "browser_starting", utcnow(),
                    contract["log_page_url"], poll_interval_seconds,
                    accepted_limit,
                    contract["source_type"], date_from_utc, date_to_utc,
                    normalized_timezone, json.dumps(contract["allowed_hosts"]),
                    int(maximum_http_reads), observed_limit, accepted_limit,
                    int(maximum_elapsed_seconds), int(periodic_polling),
                    int(page_size), int(maximum_pages),
                    *scope.sql_parameters(),
                ))
                cursor_row = db.execute("""SELECT cursor_kind,cursor_value,
                  updated_at,sync_job_id FROM realtime_log_sync_cursors
                  WHERE environment_id=? AND tenant_id=? AND workspace_id=?""",
                  (environment_id, *scope.sql_parameters())).fetchone()
                pending_count = 0
                call_log_columns = {row[1] for row in db.execute(
                    "PRAGMA table_info(standardized_call_logs)")}
                if {"cost_status", "traffic_class", "request_id"}.issubset(
                        call_log_columns):
                    pending_count = int(db.execute("""SELECT COUNT(*)
                      FROM standardized_call_logs WHERE tenant_id=? AND workspace_id=?
                      AND environment_id=? AND source_type='realtime_execution'
                      AND cost_status='pending_provider_sync'
                      AND traffic_class='business' AND request_id IS NOT NULL
                      AND TRIM(request_id)<>''""", (
                        *scope.sql_parameters(), environment_id)).fetchone()[0])
                db.execute("""INSERT INTO provider_log_sync_batches(
                  sync_batch_id,sync_job_id,environment_id,source_type,
                  started_at,previous_watermark_json,initial_pending_count,
                  tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?)""", (
                    job_id, job_id, environment_id, contract["source_type"],
                    utcnow(), json.dumps(dict(cursor_row) if cursor_row else {},
                                         ensure_ascii=False, sort_keys=True),
                    pending_count, *scope.sql_parameters()))
        except sqlite3.IntegrityError as exc:
            raise RealtimeLogSyncError("log_sync_job_already_active") from exc
        self.event(job_id, environment_id, "job_created", {
            "log_page_url": contract["log_page_url"],
            "date_from_utc": date_from_utc, "date_to_utc": date_to_utc,
            "timezone": normalized_timezone,
            "periodic_polling": periodic_polling,
            "page_size": int(page_size),
            "maximum_pages": int(maximum_pages),
        }, principal=principal, permission="collector.oneshot.execute")
        return self.get_job(job_id, private=True, principal=principal,
                            permission="collector.oneshot.execute")

    def get_job(self, job_id: str, private: bool = False, *,
                principal: PrincipalContext | None = None,
                permission: str = "collector.session.read") -> dict[str, Any]:
        scope = self._scope(principal, permission)
        with self.connect() as db:
            row = db.execute("""SELECT * FROM realtime_log_sync_jobs WHERE sync_job_id=?
              AND tenant_id=? AND workspace_id=?""",
              (job_id, *scope.sql_parameters())).fetchone()
        if not row:
            raise LookupError("log_sync_job_not_found")
        item = dict(row)
        for key in ("browser_context_active", "cookies_persisted",
                    "credentials_persisted", "network_called",
                    "periodic_polling"):
            item[key] = bool(item[key])
        if not private:
            item.pop("allowed_hosts_json", None)
            item.pop("worker_pid", None)
        return item

    def list_jobs(self, environment_id: str | None = None, *,
                  principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._scope(principal, "collector.session.read")
        with self.connect() as db:
            if environment_id:
                rows = db.execute("""SELECT sync_job_id FROM realtime_log_sync_jobs
                  WHERE environment_id=? AND tenant_id=? AND workspace_id=?
                  ORDER BY created_at DESC LIMIT 100""",
                  (environment_id, *scope.sql_parameters()))
            else:
                rows = db.execute("""SELECT sync_job_id FROM realtime_log_sync_jobs
                  WHERE tenant_id=? AND workspace_id=?
                  ORDER BY created_at DESC LIMIT 100""", scope.sql_parameters())
            ids = [row[0] for row in rows]
        return [self.get_job(job_id, principal=principal) for job_id in ids]

    def cursor(self, environment_id: str, *,
               principal: PrincipalContext | None = None) -> dict[str, Any] | None:
        scope = self._scope(principal, "collector.session.read")
        with self.connect() as db:
            row = db.execute("""SELECT environment_id,cursor_kind,cursor_value,
              updated_at,sync_job_id FROM realtime_log_sync_cursors
              WHERE environment_id=? AND tenant_id=? AND workspace_id=?""",
              (environment_id,*scope.sql_parameters())).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, *, principal: PrincipalContext | None = None,
                   permission: str = "evidence.import", **updates: Any) -> dict[str, Any]:
        scope = self._scope(principal, permission)
        lease_generation = updates.pop("_lease_generation", None)
        if updates.get("error_code"):
            updates.setdefault("stop_reason", updates["error_code"])
            updates.setdefault("error_at", utcnow())
            updates.setdefault(
                "suggested_next_action",
                ERROR_NEXT_ACTIONS.get(
                    str(updates["error_code"]),
                    "查看安全错误说明与最近事件，修正本地条件后再重试。"))
        elif updates.get("state") == "stopped":
            updates.setdefault("stop_reason", "operator_requested")
            updates.setdefault("suggested_next_action", "如需继续采集，请创建新的受控同步任务。")
        allowed = {
            "state", "started_at", "stopped_at", "last_poll_at",
            "current_safe_url",
            "last_successful_poll_at", "last_observed_log_time",
            "collected_count", "inserted_count", "duplicate_count",
            "rejected_count", "out_of_range_count", "missing_timestamp_count",
            "correlated_count", "ambiguous_count", "schema_status",
            "error_code", "safe_error_message", "browser_context_active",
            "stop_reason", "error_at", "suggested_next_action",
            "network_called", "worker_pid",
            "http_read_count", "elapsed_seconds",
            "sync_mode", "persistent_session_id",
            "periodic_polling", "page_size", "maximum_pages",
            "current_page", "pages_read", "consecutive_empty_reads",
            "consecutive_failures", "last_response_envelope_sha256",
            "last_successful_read_at", "source_cursor", "watermark_utc",
            "latest_metric_snapshot_id", "latest_confidence_snapshot_id",
            "provenance_manifest_sha256",
        }
        if set(updates) - allowed:
            raise RealtimeLogSyncError("log_sync_update_not_allowed")
        if "state" in updates and updates["state"] not in ALLOWED_STATES:
            raise RealtimeLogSyncError("log_sync_state_invalid")
        state_changed = False
        with self.connect() as db:
            if lease_generation is not None or "state" in updates:
                db.execute("BEGIN IMMEDIATE")
            if lease_generation is not None:
                self._assert_worker_fence(
                    db, job_id, int(lease_generation))
            current = db.execute("""SELECT * FROM realtime_log_sync_jobs
              WHERE sync_job_id=? AND tenant_id=? AND workspace_id=?""",
              (job_id, *scope.sql_parameters())).fetchone()
            if not current:
                raise LookupError("log_sync_job_not_found")
            requested_state = updates.get("state")
            if requested_state in ACTIVE_STATES and \
                    current["state"] in TERMINAL_STATES:
                raise RealtimeLogSyncError("log_sync_invalid_state_transition")
            elif requested_state and current["state"] in TERMINAL_STATES:
                updates = {}
            elif current["state"] == "stopping" and \
                    updates.get("error_code") == \
                    "persistent_session_lease_lost":
                updates = {}
            if updates:
                db.execute("UPDATE realtime_log_sync_jobs SET " +
                           ",".join(f"{key}=?" for key in updates) +
                           " WHERE sync_job_id=? AND tenant_id=? AND workspace_id=?",
                           (*updates.values(), job_id, *scope.sql_parameters()))
                state_changed = "state" in updates
            current = db.execute("""SELECT * FROM realtime_log_sync_jobs
              WHERE sync_job_id=? AND tenant_id=? AND workspace_id=?""",
              (job_id, *scope.sql_parameters())).fetchone()
            lease_table = db.execute("""SELECT 1 FROM sqlite_master
              WHERE type='table' AND name='persistent_session_leases'""").fetchone()
            lease_event = None
            if lease_table and current["persistent_session_id"]:
                lease = db.execute("""SELECT * FROM persistent_session_leases
                  WHERE lease_owner_job_id=? AND generation=?
                    AND state='active' AND expires_at>?""",
                                   (job_id, current["lease_generation"],
                                    utcnow())).fetchone()
                if lease and current["state"] in TERMINAL_STATES:
                    released_at = utcnow()
                    release_reason = str(
                        updates.get("stop_reason")
                        or updates.get("error_code")
                        or current["state"])[:80]
                    changed = db.execute("""UPDATE persistent_session_leases SET
                      state='released',released_at=?,release_reason=?,
                      version=version+1
                      WHERE lease_id=? AND state='active'""", (
                        released_at, release_reason, lease["lease_id"])).rowcount
                    if changed:
                        db.execute("""UPDATE persistent_browser_sessions SET
                          state='active',revision=revision+1
                          WHERE persistent_session_id=? AND state='in_use'""",
                                   (current["persistent_session_id"],))
                        lease_event = (
                            "persistent_session_lease_released", {
                                "lease_id": lease["lease_id"],
                                "release_reason": release_reason,
                            })
                elif lease and current["state"] in ACTIVE_STATES and \
                        lease_generation is not None:
                    heartbeat = datetime.now(timezone.utc)
                    session = db.execute("""SELECT expires_at
                      FROM persistent_browser_sessions
                      WHERE persistent_session_id=?""",
                                         (current["persistent_session_id"],)).fetchone()
                    if session:
                        expires = min(
                            datetime.fromisoformat(session["expires_at"]),
                            heartbeat + timedelta(
                                seconds=int(lease["timeout_seconds"])),
                        ).isoformat()
                        changed = db.execute("""UPDATE persistent_session_leases SET
                          heartbeat_at=?,expires_at=?,version=version+1
                          WHERE lease_id=? AND state='active' AND expires_at>?""", (
                            heartbeat.isoformat(), expires, lease["lease_id"],
                            heartbeat.isoformat())).rowcount
                        if changed:
                            lease_event = (
                                "persistent_session_lease_heartbeat", {
                                    "lease_id": lease["lease_id"],
                                    "expires_at": expires,
                                })
                if lease_event:
                    db.execute("""INSERT INTO realtime_log_sync_events(
                      sync_job_id,environment_id,event_type,created_at,details_json,
                      tenant_id,workspace_id)
                      VALUES(?,?,?,?,?,?,?)""", (
                        job_id, current["environment_id"], lease_event[0],
                        utcnow(), json.dumps(
                            lease_event[1], ensure_ascii=False, sort_keys=True),
                        *scope.sql_parameters(),
                    ))
        item = self.get_job(job_id, private=True, principal=principal,
                            permission=permission)
        if state_changed:
            self.event(job_id, item["environment_id"], "state_changed", {"state": updates["state"]},
                       principal=principal, permission=permission)
        return item

    def confirm_login(self, job_id: str, explicit_confirmation: bool,
                      current_url: str | None = None, *,
                      principal: PrincipalContext | None = None) -> dict[str, Any]:
        if not explicit_confirmation:
            raise RealtimeLogSyncError("log_sync_explicit_confirmation_required")
        job = self.get_job(job_id, private=True, principal=principal,
                           permission="collector.oneshot.execute")
        if job["state"] not in {"waiting_for_manual_login", "waiting_for_operator_confirmation"}:
            raise RealtimeLogSyncError("log_sync_invalid_state")
        contract = self.resolve_environment(job["environment_id"])
        visible = current_url or job.get("current_safe_url") or ""
        if safe_url(visible) != contract["log_page_url"]:
            raise RealtimeLogSyncError("log_sync_log_page_not_visible")
        return self.update_job(job_id, principal=principal,
                               permission="collector.oneshot.execute",
                               state="syncing", started_at=utcnow())

    def stop(self, job_id: str, *, principal: PrincipalContext | None = None,
             permission: str = "collector.oneshot.execute") -> dict[str, Any]:
        job = self.get_job(job_id, private=True, principal=principal,
                           permission=permission)
        if job["state"] in TERMINAL_STATES:
            return self.update_job(job_id, principal=principal, permission=permission)
        return self.update_job(job_id, principal=principal, permission=permission,
                               state="stopped", stopped_at=utcnow(),
                               browser_context_active=0)

    def mark_session_expired(self, job_id: str, reason: str = "browser_session_expired", *,
                             principal: PrincipalContext | None = None):
        return self.update_job(
            job_id, principal=principal, state="session_expired", stopped_at=utcnow(),
            browser_context_active=0, error_code="log_sync_session_expired",
            safe_error_message=reason[:300],
        )

    def _executions(self) -> list[dict[str, Any]]:
        if not self.execution_path.exists():
            return []
        rows = []
        for line in self.execution_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    @staticmethod
    def _records_from_payload(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("data", "items", "records", "list"):
                if isinstance(payload.get(key), list):
                    return [row for row in payload[key] if isinstance(row, dict)]
            return [payload]
        return []

    @staticmethod
    def normalize(row: dict[str, Any], job: dict[str, Any],
                  collection_id: str, evidence_sha: str,
                  billing_context: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = {key: None for key in NORMALIZED_FIELDS}
        for target, aliases in ALIASES.items():
            for alias in aliases:
                if alias in row and row[alias] not in ("", None):
                    normalized[target] = row[alias]
                    break
        other = row.get("other")
        if isinstance(other, str):
            try:
                other = json.loads(other)
            except (TypeError, ValueError):
                other = {}
        if not isinstance(other, dict):
            other = {}
        if normalized["cached_tokens"] is None:
            normalized["cached_tokens"] = other.get("cache_tokens")
        if normalized["ttft_ms"] is None and other.get("frt") is not None:
            try:
                normalized["ttft_ms"] = float(other["frt"]) * 1000
            except (TypeError, ValueError):
                pass
        if normalized["total_tokens"] is None:
            try:
                normalized["total_tokens"] = (
                    int(normalized["input_tokens"] or 0) +
                    int(normalized["output_tokens"] or 0))
            except (TypeError, ValueError):
                pass
        if normalized["latency_ms"] is not None and "use_time" in row:
            try:
                normalized["latency_ms"] = float(normalized["latency_ms"]) * 1000
            except (TypeError, ValueError):
                pass
        if row.get("quota") is not None:
            quota_evidence = RealtimeLogSyncService._quota_evidence(
                row.get("quota"), billing_context)
            normalized.update(quota_evidence)
            if normalized["actual_cost"] is None:
                normalized["actual_cost"] = quota_evidence["converted_amount"]
                normalized["currency"] = (
                    normalized["currency"] or quota_evidence["currency"])
        normalized.update({
            "environment_id": job["environment_id"], "observed_at": utcnow(),
            "source_type": job["source_type"], "collection_id": collection_id,
            "sync_job_id": job["sync_job_id"], "evidence_sha256": evidence_sha,
            "is_mock": False, "actual_cost_source": "platform_log"
            if normalized["actual_cost"] is not None else None,
            "actual_cost_status": (
                "confirmed" if normalized["actual_cost"] is not None
                else "unknown"),
            "actual_channel_status": (
                "confirmed" if normalized["actual_channel_id"] is not None
                or normalized["actual_channel_name"] is not None
                else "unknown"),
        })
        return normalized

    @staticmethod
    def _quota_cost(
        quota: Any, billing_context: dict[str, Any] | None,
    ) -> tuple[str | None, str | None]:
        evidence = RealtimeLogSyncService._quota_evidence(
            quota, billing_context)
        return evidence["converted_amount"], evidence["currency"]

    @staticmethod
    def _quota_evidence(
        quota: Any, billing_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        empty = {
            "raw_quota": None, "quota_unit": None,
            "quota_conversion_rate": None, "converted_amount": None,
            "currency": None, "cost_precision": None,
            "rounding_mode": None, "cost_source_reference": None,
            "billing_scope": None,
        }
        if not billing_context:
            return empty
        try:
            units = Decimal(str(quota))
            per_unit = Decimal(str(billing_context["quota_per_unit"]))
            display = str(billing_context["quota_display_type"]).upper()
            if per_unit <= 0 or display == "TOKENS":
                return empty
            conversion_rate = Decimal("1") / per_unit
            currency = "USD"
            if display == "CNY":
                rate = Decimal(str(billing_context["usd_exchange_rate"]))
                if rate <= 0:
                    return empty
                conversion_rate *= rate
                currency = "CNY"
            elif display != "USD":
                return empty
            cost = units * conversion_rate
            return {
                "raw_quota": format(units, "f"),
                "quota_unit": "provider_internal_quota",
                "quota_conversion_rate": format(conversion_rate, "f"),
                # Keep the unrounded amount as the accounting value. The UI
                # applies the provider's six-decimal HALF_UP display rule.
                "converted_amount": format(cost, "f"),
                "currency": currency,
                "cost_precision": 6,
                "rounding_mode": "ROUND_HALF_UP",
                "cost_source_reference": "provider_public_status:/api/status",
                "billing_scope": "provider_quota_includes_input_output_cache",
                "display_amount": format(
                    cost.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
                    "f"),
            }
        except (KeyError, InvalidOperation, TypeError, ValueError):
            return empty

    @staticmethod
    def _record_timestamp(normalized: dict[str, Any]) -> datetime | None:
        value = normalized.get("platform_created_at")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                parsed = datetime.fromtimestamp(float(value), timezone.utc)
                normalized["original_safe_timestamp_text"] = str(value)
                normalized["platform_created_at"] = parsed.isoformat()
                normalized["normalized_utc_timestamp"] = parsed.isoformat()
                return parsed
            except (OSError, OverflowError, ValueError):
                return None
        if not isinstance(value, str) or not value.strip():
            return None
        normalized["original_safe_timestamp_text"] = sanitize(value)
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        parsed = parsed.astimezone(timezone.utc)
        normalized["normalized_utc_timestamp"] = parsed.isoformat()
        return parsed

    @staticmethod
    def identity(record: dict[str, Any]) -> tuple[str, str]:
        for kind in (
            "platform_log_id", "provider_request_id", "provider_response_id",
            "provider_trace_id", "client_correlation_id",
        ):
            if record.get(kind):
                return kind, str(record[kind])
        stable = {
            key: record.get(key) for key in (
                "platform_created_at", "requested_model", "actual_model",
                "http_status", "input_tokens", "output_tokens", "total_tokens",
            )
        }
        if not any(value is not None for value in stable.values()):
            raise RealtimeLogSyncError("schema_mapping_required")
        digest = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
        return "composite", digest

    @staticmethod
    def _assert_worker_fence(
        db: sqlite3.Connection, job_id: str, generation: int | None,
    ) -> None:
        if generation is None:
            return
        row = db.execute("""SELECT 1
          FROM persistent_session_leases l
          JOIN realtime_log_sync_jobs j
            ON j.sync_job_id=l.lease_owner_job_id
          WHERE l.lease_owner_job_id=? AND l.generation=?
            AND l.state='active' AND l.expires_at>?
            AND j.lease_generation=l.generation
            AND j.state IN
            ('created','decrypting_session','launching_headless_browser',
             'validating_authentication','synchronizing')""",
                         (job_id, int(generation), utcnow())).fetchone()
        if not row:
            raise RealtimeLogSyncError("persistent_session_lease_lost")

    def correlate(
        self, record_id: str, normalized: dict[str, Any],
        job_id: str | None = None, lease_generation: int | None = None,
        *, principal: PrincipalContext | None = None,
    ) -> tuple[str, str | None]:
        scope = self._scope(principal, "evidence.import")
        state, execution_id = "unmatched", None
        method = None
        matched_field = matched_value = matched_value_hash = None
        matched: list[sqlite3.Row] = []
        exact_fields = (
            "provider_request_id", "provider_response_id",
            "provider_trace_id", "client_correlation_id",
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if job_id is not None:
                self._assert_worker_fence(db, job_id, lease_generation)
            unified_exists = db.execute("""SELECT 1 FROM sqlite_master
              WHERE type='table' AND name='standardized_call_logs'""").fetchone()
            if unified_exists:
                unified_columns = {row[1] for row in db.execute(
                    "PRAGMA table_info(standardized_call_logs)")}
                for field in exact_fields:
                    value = normalized.get(field)
                    if not value or field not in unified_columns:
                        continue
                    candidates = db.execute(f"""SELECT record_id,request_id,
                      local_request_id,decision_id,channel_source
                      FROM standardized_call_logs WHERE {field}=?
                      AND tenant_id=? AND workspace_id=?""", (
                        str(value), *scope.sql_parameters())).fetchall()
                    if len(candidates) == 1:
                        matched = candidates
                        matched_field = field
                        matched_value = str(value)
                        matched_value_hash = hashlib.sha256(
                            matched_value.encode()).hexdigest()
                        state = method = f"exact_{field}"
                        local_id = candidates[0]["local_request_id"] or \
                            candidates[0]["request_id"]
                        has_uat_executions = db.execute("""SELECT 1
                          FROM sqlite_master WHERE type='table'
                          AND name='uat_executions'""").fetchone()
                        if local_id and has_uat_executions:
                            execution = db.execute("""SELECT execution_id
                              FROM uat_executions WHERE local_request_id=?
                              AND tenant_id=? AND workspace_id=?""", (
                                local_id, *scope.sql_parameters())).fetchone()
                            execution_id = execution[0] if execution else None
                        break
                    if len(candidates) > 1:
                        state, method = "ambiguous", f"{field}_not_unique"
                        break
            db.execute("""INSERT INTO realtime_log_correlations(
              correlation_id,execution_id,record_id,environment_id,state,method,
              matched_field,matched_value_hash,match_confidence,provider_log_ids,
              matched_at,created_at,details_json,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT DO UPDATE SET
                execution_id=excluded.execution_id,state=excluded.state,
                method=excluded.method,matched_field=excluded.matched_field,
                matched_value_hash=excluded.matched_value_hash,
                match_confidence=excluded.match_confidence,
                provider_log_ids=excluded.provider_log_ids,
                matched_at=excluded.matched_at,
                details_json=excluded.details_json""", (
                str(uuid.uuid4()), execution_id, record_id, normalized["environment_id"],
                state, method, matched_field, matched_value_hash,
                "exact" if state.startswith("exact_") else None,
                json.dumps([str(normalized.get("platform_log_id"))])
                if normalized.get("platform_log_id") else "[]",
                utcnow() if state.startswith("exact_") else None,
                utcnow(), json.dumps({
                    "actual_cost": normalized.get("actual_cost"),
                    "currency": normalized.get("currency"),
                    "actual_channel_id": normalized.get("actual_channel_id"),
                    "actual_channel_name": normalized.get("actual_channel_name"),
                    "platform_log_id": normalized.get("platform_log_id"),
                }, ensure_ascii=False, sort_keys=True),
                *scope.sql_parameters(),
            ))
            # Enrich the unified execution ledger only for an exact identifier
            # match. Composite/ambiguous matches never authorize channel backfill.
            authoritative_channel = normalized.get("actual_channel_id")
            if unified_exists and state.startswith("exact_"):
                identifier_column = matched_field
                identifier_value = matched_value
                if identifier_column and identifier_value:
                    matched = db.execute(f"""SELECT record_id,request_id,
                      local_request_id,channel_source FROM standardized_call_logs
                      WHERE {identifier_column}=? AND tenant_id=? AND workspace_id=?""",
                      (identifier_value, *scope.sql_parameters())).fetchall()
                    if len(matched) == 1:
                        local_request_id = (matched[0]["local_request_id"] or
                                            matched[0]["request_id"])
                        now = utcnow()
                        platform_log_id = str(
                            normalized.get("platform_log_id") or "")
                        component_rows: list[sqlite3.Row] = []
                        if platform_log_id and normalized.get(
                                "actual_cost") is not None:
                            raw_quota = str(normalized.get("raw_quota") or "")
                            try:
                                component_role = (
                                    "reversal" if Decimal(raw_quota) < 0
                                    else "billing_component")
                            except (InvalidOperation, ValueError):
                                component_role = "billing_component"
                            raw_record_sha = hashlib.sha256(json.dumps({
                                "provider_log_id": platform_log_id,
                                "request_id": local_request_id,
                                "provider_log_time": normalized.get(
                                    "platform_created_at"),
                                "raw_quota": raw_quota,
                                "converted_amount": normalized.get(
                                    "actual_cost"),
                            }, sort_keys=True, separators=(",", ":")).encode()
                            ).hexdigest()
                            component_id = "PLC-" + hashlib.sha256(
                                f"{normalized['environment_id']}:{platform_log_id}"
                                .encode()).hexdigest()[:24].upper()
                            db.execute("""INSERT OR IGNORE INTO provider_cost_components(
                              component_id,provider_log_id,request_id,environment_id,
                              component_role,raw_quota,quota_unit,conversion_rate,
                              converted_amount,currency,precision,rounding_mode,
                              source_reference,provider_log_time,raw_record_sha256,
                              sync_batch_id,created_at,tenant_id,workspace_id)
                              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                                component_id, platform_log_id, local_request_id,
                                normalized["environment_id"], component_role,
                                raw_quota,
                                str(normalized.get("quota_unit") or
                                    "provider_internal_quota"),
                                str(normalized.get("quota_conversion_rate") or ""),
                                str(normalized["actual_cost"]),
                                str(normalized.get("currency") or "CNY"),
                                int(normalized.get("cost_precision") or 6),
                                str(normalized.get("rounding_mode") or
                                    "ROUND_HALF_UP"),
                                str(normalized.get("cost_source_reference") or
                                    "provider_billing_log"),
                                normalized.get("platform_created_at"),
                                raw_record_sha, str(job_id or normalized.get(
                                    "sync_job_id") or "unbatched"), now,
                                *scope.sql_parameters()))
                        component_rows = db.execute("""SELECT component_id,
                          provider_log_id,component_role,raw_quota,converted_amount,
                          currency,provider_log_time FROM provider_cost_components
                          WHERE request_id=? AND environment_id=? AND tenant_id=?
                          AND workspace_id=? ORDER BY provider_log_time,provider_log_id""", (
                            local_request_id, normalized["environment_id"],
                            *scope.sql_parameters())).fetchall()
                        currencies = {str(row["currency"]) for row in component_rows}
                        aggregate_cost = (format(sum((Decimal(str(
                            row["converted_amount"])) for row in component_rows),
                            Decimal("0")), "f") if component_rows and
                            len(currencies) == 1 else None)
                        component_json = json.dumps([{
                            "component_id": row["component_id"],
                            "provider_log_id": row["provider_log_id"],
                            "role": row["component_role"],
                            "raw_quota": row["raw_quota"],
                            "converted_amount": row["converted_amount"],
                            "provider_log_time": row["provider_log_time"],
                        } for row in component_rows], ensure_ascii=False,
                            sort_keys=True, separators=(",", ":"))
                        db.execute(f"""UPDATE standardized_call_logs SET
                          provider_log_id=COALESCE(?,provider_log_id),
                          provider_request_id=COALESCE(provider_request_id,?),
                          provider_response_id=COALESCE(provider_response_id,?),
                          provider_trace_id=COALESCE(provider_trace_id,?),
                          client_correlation_id=COALESCE(client_correlation_id,?),
                          match_method=?,matched_field=?,matched_value_hash=?,
                          match_confidence='exact',provider_log_ids=?,matched_at=?,
                          cost_amount=COALESCE(?,cost_amount),
                          provider_cost_amount_exact=COALESCE(?,provider_cost_amount_exact),
                          currency=COALESCE(?,currency),
                          cost_source=CASE WHEN ? IS NOT NULL
                            THEN 'provider_log' ELSE cost_source END,
                          cost_status=CASE WHEN ? IS NOT NULL
                            THEN 'provider_actual' ELSE cost_status END,
                          cost_type=CASE WHEN ? IS NOT NULL
                            THEN 'provider_actual' ELSE cost_type END,
                          price_source=CASE WHEN ? IS NOT NULL
                            THEN 'provider_log_quota' ELSE price_source END,
                          provider_raw_quota=COALESCE(?,provider_raw_quota),
                          provider_quota_unit=COALESCE(?,provider_quota_unit),
                          provider_conversion_rate=COALESCE(?,provider_conversion_rate),
                          provider_cost_precision=COALESCE(?,provider_cost_precision),
                          provider_rounding_mode=COALESCE(?,provider_rounding_mode),
                          provider_cost_source_reference=COALESCE(?,provider_cost_source_reference),
                          provider_sync_batch_id=COALESCE(?,provider_sync_batch_id),
                          provider_cost_components_json=?,provider_cost_updated_at=?,
                          channel_id=CASE WHEN channel_id IS NULL THEN ? ELSE channel_id END,
                          channel_name=CASE WHEN channel_name IS NULL THEN ? ELSE channel_name END,
                          channel_source=CASE
                            WHEN channel_source='scheduler_decision' THEN channel_source
                            WHEN ? IS NOT NULL THEN 'provider_log'
                            ELSE channel_source END,
                          evidence_scope=CASE WHEN COALESCE(channel_id,?) IS NOT NULL
                            THEN 'channel_eligible' ELSE evidence_scope END,
                          updated_at=? WHERE {identifier_column}=? AND tenant_id=? AND workspace_id=?""", (
                            normalized.get("platform_log_id"),
                            normalized.get("provider_request_id"),
                            normalized.get("provider_response_id"),
                            normalized.get("provider_trace_id"),
                            normalized.get("client_correlation_id"),
                            method, matched_field, matched_value_hash,
                            json.dumps([platform_log_id]) if platform_log_id else "[]",
                            now, aggregate_cost,
                            aggregate_cost,
                            next(iter(currencies)) if len(currencies) == 1 else None,
                            aggregate_cost, aggregate_cost, aggregate_cost,
                            aggregate_cost, normalized.get("raw_quota"),
                            normalized.get("quota_unit"),
                            normalized.get("quota_conversion_rate"),
                            normalized.get("cost_precision"),
                            normalized.get("rounding_mode"),
                            normalized.get("cost_source_reference"),
                            str(job_id or normalized.get("sync_job_id") or "unbatched"),
                            component_json, now, authoritative_channel,
                            normalized.get("actual_channel_name"), authoritative_channel,
                            authoritative_channel, now, identifier_value,
                            *scope.sql_parameters()))
                        if aggregate_cost is not None and matched[0]["request_id"]:
                            attempts = db.execute("""SELECT attempt_id FROM call_attempt_logs
                              WHERE request_id=? AND tenant_id=? AND workspace_id=?
                              ORDER BY attempt_number""", (local_request_id,
                                *scope.sql_parameters())).fetchall()
                            if len(attempts) == 1:
                                db.execute("""UPDATE call_attempt_logs SET
                                  cost_amount=?,provider_cost_amount_exact=?,currency=?,
                                  cost_source='provider_log',cost_status='provider_actual',
                                  cost_type='provider_actual',provider_sync_batch_id=?,
                                  provider_log_id=COALESCE(provider_log_id,?),
                                  provider_request_id=COALESCE(provider_request_id,?),
                                  provider_response_id=COALESCE(provider_response_id,?),
                                  provider_trace_id=COALESCE(provider_trace_id,?)
                                  WHERE attempt_id=? AND tenant_id=? AND workspace_id=?""", (
                                    aggregate_cost, aggregate_cost,
                                    next(iter(currencies)),
                                    str(job_id or "unbatched"),
                                    platform_log_id,
                                    normalized.get("provider_request_id"),
                                    normalized.get("provider_response_id"),
                                    normalized.get("provider_trace_id"),
                                    attempts[0]["attempt_id"],
                                    *scope.sql_parameters()))
                            elif len(attempts) == len(component_rows):
                                for attempt, component in zip(attempts, component_rows):
                                    db.execute("""UPDATE call_attempt_logs SET
                                      cost_amount=?,provider_cost_amount_exact=?,currency=?,
                                      cost_source='provider_log',cost_status='provider_actual',
                                      cost_type='provider_actual',provider_sync_batch_id=?,
                                      provider_log_id=COALESCE(provider_log_id,?),
                                      provider_request_id=COALESCE(provider_request_id,?),
                                      provider_response_id=COALESCE(provider_response_id,?),
                                      provider_trace_id=COALESCE(provider_trace_id,?)
                                      WHERE attempt_id=? AND tenant_id=? AND workspace_id=?""", (
                                        component["converted_amount"],
                                        component["converted_amount"],
                                        component["currency"], str(job_id or "unbatched"),
                                        component["provider_log_id"],
                                        normalized.get("provider_request_id"),
                                        normalized.get("provider_response_id"),
                                        normalized.get("provider_trace_id"),
                                        attempt["attempt_id"],
                                        *scope.sql_parameters()))
                            elif attempts:
                                db.execute("""UPDATE call_attempt_logs SET
                                  cost_status='duplicate_or_reversal_pending',
                                  provider_sync_batch_id=? WHERE request_id=?
                                  AND tenant_id=? AND workspace_id=?""", (
                                    str(job_id or "unbatched"), local_request_id,
                                    *scope.sql_parameters()))
                            if db.execute("""SELECT 1 FROM sqlite_master WHERE
                              type='table' AND name='uat_executions'""").fetchone():
                                db.execute("""UPDATE uat_executions SET
                                  actual_cost_amount=?,currency=?,
                                  cost_status='provider_actual',cost_type='provider_actual',
                                  provider_sync_batch_id=?,provider_cost_updated_at=?
                                  WHERE local_request_id=? AND tenant_id=? AND workspace_id=?""", (
                                    aggregate_cost, next(iter(currencies)),
                                    str(job_id or "unbatched"), now, local_request_id,
                                    *scope.sql_parameters()))
                            decision_rows = db.execute("""SELECT decision_id,evidence_json
                              FROM routing_decision_logs WHERE request_id=?
                              AND tenant_id=? AND workspace_id=?""", (
                                local_request_id,*scope.sql_parameters())).fetchall()
                            for decision in decision_rows:
                                evidence = json.loads(decision["evidence_json"])
                                result = evidence.setdefault("execution_result", {})
                                result.update({
                                    "cost_amount": aggregate_cost,
                                    "currency": next(iter(currencies)),
                                    "cost_status": "provider_actual",
                                    "cost_type": "provider_actual",
                                    "provider_sync_batch_id": str(job_id or "unbatched"),
                                })
                                db.execute("""UPDATE routing_decision_logs SET
                                  evidence_json=?,updated_at=? WHERE decision_id=?
                                  AND tenant_id=? AND workspace_id=?""", (
                                    json.dumps(evidence, ensure_ascii=False,
                                               sort_keys=True), now,
                                    decision["decision_id"],
                                    *scope.sql_parameters()))
                        if db.execute("""SELECT 1 FROM sqlite_master WHERE type='table'
                          AND name='call_log_changes'""").fetchone():
                            db.execute("""INSERT INTO call_log_changes(record_id,changed_at,tenant_id,workspace_id)
                              VALUES(?,?,?,?)""", (matched[0]["record_id"], now,
                                                    *scope.sql_parameters()))
        return state, execution_id

    def manually_confirm_correlation(
        self, record_id: str, local_request_id: str, reason: str, *,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        """Confirm a Provider billing row when the protocol has no shared ID.

        This is an audited operator action and is intentionally excluded from
        automatic exact-match coverage.  It never selects candidates by model,
        time, token count, cost, IP, or API-key display name.
        """
        scope = self._scope(principal, "evidence.import")
        reason = str(reason or "").strip()
        if len(reason) < 8 or len(reason) > 500:
            raise RealtimeLogSyncError("manual_correlation_reason_required")
        local_request_id = str(local_request_id or "").strip()
        if not local_request_id:
            raise RealtimeLogSyncError("manual_correlation_request_required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            provider_row = db.execute("""SELECT normalized_json
              FROM realtime_log_records WHERE record_id=? AND tenant_id=?
              AND workspace_id=?""", (
                record_id, *scope.sql_parameters())).fetchone()
            local_rows = db.execute("""SELECT record_id,provider_request_id,
              provider_response_id,provider_trace_id,client_correlation_id
              FROM standardized_call_logs WHERE local_request_id=?
              AND tenant_id=? AND workspace_id=?""", (
                local_request_id, *scope.sql_parameters())).fetchall()
            if not provider_row or len(local_rows) != 1:
                raise RealtimeLogSyncError("manual_correlation_target_not_found")
            normalized = json.loads(provider_row["normalized_json"])
            chosen_field = next((field for field in (
                "provider_request_id", "provider_response_id",
                "provider_trace_id", "client_correlation_id",
            ) if normalized.get(field)), None)
            if not chosen_field:
                raise RealtimeLogSyncError(
                    "manual_correlation_provider_identifier_missing")
            chosen_value = str(normalized[chosen_field])
            existing = local_rows[0][chosen_field]
            if existing and str(existing) != chosen_value:
                raise RealtimeLogSyncError("manual_correlation_identifier_conflict")
            db.execute(f"""UPDATE standardized_call_logs SET
              {chosen_field}=?,updated_at=? WHERE record_id=?
              AND tenant_id=? AND workspace_id=?""", (
                chosen_value, utcnow(), local_rows[0]["record_id"],
                *scope.sql_parameters()))
        # Reuse the transactionally consistent cost-component backfill path,
        # then label the result as a human decision rather than an exact auto
        # match so automatic precision metrics remain honest.
        self.correlate(record_id, normalized, principal=principal)
        now = utcnow()
        value_hash = hashlib.sha256(chosen_value.encode()).hexdigest()
        operator_id = principal.principal_id if principal else "local-operator"
        audit_id = f"AUD-{uuid.uuid4().hex[:20].upper()}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE realtime_log_correlations SET
              state='manually_confirmed',method='manually_confirmed',
              matched_field=?,matched_value_hash=?,match_confidence='manual',
              matched_at=?,details_json=? WHERE record_id=? AND tenant_id=?
              AND workspace_id=?""", (
                chosen_field, value_hash, now, json.dumps({
                    "audit_id": audit_id, "operator_id": operator_id,
                    "reason": reason,
                }, ensure_ascii=False, sort_keys=True), record_id,
                *scope.sql_parameters()))
            db.execute("""UPDATE standardized_call_logs SET
              match_method='manually_confirmed',matched_field=?,
              matched_value_hash=?,match_confidence='manual',matched_at=?,
              updated_at=? WHERE local_request_id=? AND tenant_id=?
              AND workspace_id=?""", (
                chosen_field, value_hash, now, now, local_request_id,
                *scope.sql_parameters()))
            db.execute("""INSERT INTO realtime_log_sync_events(
              sync_job_id,environment_id,event_type,created_at,details_json,
              tenant_id,workspace_id) VALUES(NULL,'china_uat',?,?,?,?,?)""", (
                "manual_provider_cost_correlation", now, json.dumps({
                    "audit_id": audit_id, "operator_id": operator_id,
                    "local_request_id": local_request_id,
                    "provider_record_id": record_id,
                    "matched_field": chosen_field,
                    "matched_value_hash": value_hash,
                    "reason": reason,
                }, ensure_ascii=False, sort_keys=True),
                *scope.sql_parameters()))
        return {
            "status": "manually_confirmed", "audit_id": audit_id,
            "local_request_id": local_request_id,
            "provider_record_id": record_id, "matched_field": chosen_field,
            "matched_value_hash": value_hash,
        }

    def ingest(
        self, job_id: str, payload: Any, source_url: str,
        lease_generation: int | None = None,
        billing_context: dict[str, Any] | None = None,
        *, principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        scope = self._scope(principal, "evidence.import")
        job = self.get_job(job_id, private=True, principal=principal,
                           permission="evidence.import")
        if job["state"] not in {"syncing", "synchronizing"}:
            raise RealtimeLogSyncError("log_sync_manual_login_required")
        parsed = urlsplit(source_url)
        allowed = set(json.loads(job["allowed_hosts_json"]))
        try:
            source_port = parsed.port
        except ValueError as exc:
            raise RealtimeLogSyncError(
                "log_sync_source_not_allowed") from exc
        if parsed.scheme != "https" or parsed.username or parsed.password or \
                source_port not in {None, 443} or \
                (parsed.hostname or "").lower() not in allowed or \
                (job["environment_id"] == "china_uat" and
                 parsed.path != "/api/log/self"):
            raise RealtimeLogSyncError("log_sync_source_not_allowed")
        collection_id = f"LSCOL-{uuid.uuid4().hex[:16].upper()}"
        adapter = None
        observation = None
        mapping_error = None
        pagination = None
        domestic_uat = job["environment_id"] == "china_uat"
        persistent_domestic = (
            domestic_uat and
            job.get("sync_mode") == "persistent_encrypted_session")
        if persistent_domestic and job.get("lease_generation") is not None:
            if lease_generation is None or int(lease_generation) != int(
                    job["lease_generation"]):
                raise RealtimeLogSyncError("persistent_session_lease_lost")
        sanitized = sanitize(payload)
        records = self._records_from_payload(sanitized)
        if domestic_uat:
            try:
                observation = observe_schema(
                    payload, job["environment_id"], source_url)
                adapter = self.schema_registry.select(observation)
                if adapter is None:
                    mapping_error = "schema_mapping_required"
                    records = []
                else:
                    records, pagination = self.schema_registry.extract(
                        adapter, payload)
            except SchemaObservationError as exc:
                mapping_error = str(exc)
                records = []
        observed_remaining = max(
            0, int(job.get("maximum_records_observed",
                           job["maximum_records"])) -
            int(job["collected_count"]))
        accepted_remaining = max(
            0, int(job.get("maximum_records_accepted",
                           job["maximum_records"])) -
            int(job["inserted_count"]))
        raw_candidate_count = (
            int(observation.candidate_record_count)
            if domestic_uat and observation and
            observation.candidate_record_count is not None
            else len(records))
        bounded_overflow_count = max(
            0, raw_candidate_count - observed_remaining)
        if len(records) > observed_remaining:
            records = records[:observed_remaining]
        summary = (
            {
                "fingerprint_version": "uat_billing_shape_v1",
                "schema_fingerprint": observation.fingerprint,
                "candidate_record_count": observation.candidate_record_count,
                "sensitive_field_count": observation.sensitive_field_count,
                "shape": observation.shape,
            }
            if observation else schema_summary(sanitized)
        )
        if domestic_uat:
            safe_evidence = {
                "sync_job_id": job_id,
                "environment_id": job["environment_id"],
                "schema_fingerprint": (
                    observation.fingerprint if observation
                    else "shape_observation_failed"),
                "shape": observation.shape if observation else None,
            }
            stored_payload = json.dumps(
                safe_evidence, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"))
        else:
            stored_payload = json.dumps(
                sanitized, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"))
        evidence_sha = hashlib.sha256(stored_payload.encode()).hexdigest()
        with self.connect() as db:
            if lease_generation is not None:
                db.execute("BEGIN IMMEDIATE")
            self._assert_worker_fence(db, job_id, lease_generation)
            db.execute("""INSERT OR IGNORE INTO realtime_log_evidence(
              evidence_sha256,sync_job_id,environment_id,sanitized_payload,
              schema_summary,source_url,created_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?)""", (
                evidence_sha, job_id, job["environment_id"], stored_payload,
                json.dumps(summary, ensure_ascii=False, sort_keys=True),
                safe_url(source_url), utcnow(),
                *scope.sql_parameters(),
            ))
            if domestic_uat:
                adapter_id = adapter.get("schema_adapter_id") if adapter else None
                adapter_version = adapter.get("adapter_version") if adapter else None
                fingerprint = (
                    observation.fingerprint if observation
                    else "shape_observation_failed")
                db.execute("""INSERT INTO realtime_log_schema_audit(
                  selection_id,sync_job_id,environment_id,observed_at,
                  schema_fingerprint,schema_adapter_id,adapter_version,
                  mapping_version,selection_status,candidate_record_count,
                  sensitive_field_count,accepted_field_coverage,
                  rejected_field_count,evidence_sha256,source_url,rejection_reason)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    f"LSSA-{uuid.uuid4().hex[:24].upper()}", job_id,
                    job["environment_id"], utcnow(), fingerprint,
                    adapter_id, adapter_version, adapter_version,
                    "selected" if adapter and not mapping_error else "rejected",
                    observation.candidate_record_count if observation else None,
                    observation.sensitive_field_count if observation else 0,
                    1.0 if adapter and not mapping_error else 0.0,
                    max(
                        observation.sensitive_field_count
                        if observation else 0,
                        1 if mapping_error else 0),
                    evidence_sha, safe_url(source_url), mapping_error,
                ))
                db.execute("""UPDATE realtime_log_schema_audit
                  SET tenant_id=?,workspace_id=? WHERE sync_job_id=?
                  AND evidence_sha256=?""",
                  (*scope.sql_parameters(),job_id,evidence_sha))
                db.execute("""UPDATE realtime_log_schema_audit SET
                  pagination_json=?,adapter_metadata_json=?
                  WHERE selection_id=(
                    SELECT selection_id FROM realtime_log_schema_audit
                    WHERE sync_job_id=? AND tenant_id=? AND workspace_id=?
                    ORDER BY observed_at DESC LIMIT 1)""", (
                        json.dumps(
                            pagination, ensure_ascii=False, sort_keys=True)
                        if pagination is not None else None,
                        json.dumps({
                            "source_reliability_classification":
                                adapter.get(
                                    "source_reliability_classification")
                                if adapter else None,
                            "provenance_contract":
                                adapter.get("provenance_contract")
                                if adapter else None,
                            "reviewed_at": adapter.get("reviewed_at")
                                if adapter else None,
                            "review_evidence_sha256":
                                adapter.get("review_evidence_sha256")
                                if adapter else None,
                        }, ensure_ascii=False, sort_keys=True),
                        job_id,*scope.sql_parameters()))
                if mapping_error:
                    rejection_count = max(
                        1, observation.candidate_record_count
                        if observation and observation.candidate_record_count
                        is not None else 1)
                    for index in range(rejection_count):
                        db.execute("""INSERT INTO realtime_log_rejections(
                          rejection_id,sync_job_id,evidence_sha256,
                          source_record_index,schema_fingerprint,
                          schema_adapter_id,adapter_version,rejection_reason,
                          created_at,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                            f"LSRJ-{uuid.uuid4().hex[:24].upper()}", job_id,
                            evidence_sha, index if observation and
                            observation.candidate_record_count else None,
                            fingerprint, adapter_id, adapter_version,
                            mapping_error, utcnow(),
                            *scope.sql_parameters(),
                        ))
        inserted = duplicates = correlated = ambiguous = 0
        rejected = bounded_overflow_count
        out_of_range = missing_timestamp = schema_rejected = 0
        if mapping_error:
            schema_rejected = max(
                1, observation.candidate_record_count
                if observation and observation.candidate_record_count
                is not None else 1)
            rejected += schema_rejected
        range_start = datetime.fromisoformat(job["date_from_utc"])
        range_end = datetime.fromisoformat(job["date_to_utc"])
        last_time = None
        for row in records:
            if inserted >= accepted_remaining:
                rejected += 1
                continue
            normalized = self.normalize(
                row, job, collection_id, evidence_sha, billing_context)
            try:
                cursor_kind, cursor_value = self.identity(normalized)
            except RealtimeLogSyncError:
                schema_rejected += 1
                rejected += 1
                continue
            observed_timestamp = self._record_timestamp(normalized)
            if observed_timestamp is None:
                missing_timestamp += 1
                rejected += 1
                continue
            if observed_timestamp < range_start or observed_timestamp > range_end:
                out_of_range += 1
                rejected += 1
                continue
            identity_key = scope.resource_key(
                resource_type=f"log:{cursor_kind}", resource_id=cursor_value,
                version="realtime_log_v1")
            record_id = f"LSREC-{uuid.uuid4().hex[:20].upper()}"
            try:
                with self.connect() as db:
                    if lease_generation is not None:
                        db.execute("BEGIN IMMEDIATE")
                    self._assert_worker_fence(db, job_id, lease_generation)
                    db.execute("""INSERT INTO realtime_log_records(
                      record_id,identity_key,environment_id,platform_log_id,request_id,
                      response_id,provider_request_id,provider_response_id,
                      provider_trace_id,client_correlation_id,observed_at,
                      platform_created_at,source_type,sync_job_id,evidence_sha256,
                      normalized_json,created_at,updated_at,tenant_id,workspace_id)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        record_id, identity_key, job["environment_id"],
                        normalized["platform_log_id"], normalized["request_id"],
                        normalized["response_id"], normalized["provider_request_id"],
                        normalized["provider_response_id"],
                        normalized["provider_trace_id"],
                        normalized["client_correlation_id"], normalized["observed_at"],
                        normalized["platform_created_at"], normalized["source_type"],
                        job_id, evidence_sha, json.dumps(normalized, ensure_ascii=False, sort_keys=True),
                        utcnow(), utcnow(),
                        *scope.sql_parameters(),
                    ))
                    db.execute("""INSERT INTO realtime_log_sync_cursors(
                      environment_id,cursor_kind,cursor_value,updated_at,sync_job_id,
                      tenant_id,workspace_id)
                      VALUES(?,?,?,?,?,?,?)
                      ON CONFLICT(tenant_id,workspace_id,environment_id) DO UPDATE SET
                      cursor_kind=excluded.cursor_kind,cursor_value=excluded.cursor_value,
                      updated_at=excluded.updated_at,sync_job_id=excluded.sync_job_id""", (
                        job["environment_id"], cursor_kind, cursor_value, utcnow(), job_id,
                        *scope.sql_parameters(),
                    ))
                inserted += 1
                state, _ = self.correlate(
                    record_id, normalized, job_id, lease_generation,
                    principal=principal)
                if state.startswith("exact_"):
                    correlated += 1
                elif state in {"ambiguous", "strong_candidate"}:
                    ambiguous += 1
                accepted_time = observed_timestamp.astimezone(
                    timezone.utc).isoformat()
                last_time = max(last_time, accepted_time) \
                    if last_time else accepted_time
            except sqlite3.IntegrityError:
                duplicates += 1
                # A Provider row can arrive before the execution has persisted
                # its response identifiers.  On an idempotent re-read, retry
                # exact correlation against the existing immutable row rather
                # than treating the duplicate as permanently unmatchable.
                with self.connect() as db:
                    existing_record = db.execute("""SELECT record_id
                      FROM realtime_log_records WHERE environment_id=?
                      AND identity_key=? AND tenant_id=? AND workspace_id=?""", (
                        job["environment_id"], identity_key,
                        *scope.sql_parameters())).fetchone()
                if existing_record:
                    duplicate_state, _ = self.correlate(
                        existing_record["record_id"], normalized, job_id,
                        lease_generation, principal=principal)
                    if duplicate_state.startswith("exact_"):
                        correlated += 1
                    elif duplicate_state in {"ambiguous", "strong_candidate"}:
                        ambiguous += 1
        collected = min(raw_candidate_count, observed_remaining)
        response_envelope_sha256 = hashlib.sha256(
            json.dumps(
                sanitize(payload), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        empty_read = raw_candidate_count == 0
        page_number = int((pagination or {}).get(
            "page", job.get("current_page") or 1))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_worker_fence(db, job_id, lease_generation)
            db.execute("""INSERT INTO provider_log_sync_pages(
              sync_batch_id,page_number,provider_count,accepted_count,
              duplicate_count,response_sha256,provider_cursor,
              provider_log_time,read_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(tenant_id,workspace_id,sync_batch_id,page_number)
              DO UPDATE SET provider_count=excluded.provider_count,
                accepted_count=excluded.accepted_count,
                duplicate_count=excluded.duplicate_count,
                response_sha256=excluded.response_sha256,
                provider_cursor=excluded.provider_cursor,
                provider_log_time=excluded.provider_log_time,
                read_at=excluded.read_at""", (
                job_id, page_number, raw_candidate_count, inserted,
                duplicates, response_envelope_sha256,
                str((pagination or {}).get("page") or page_number),
                last_time, utcnow(), *scope.sql_parameters()))
            page_totals = db.execute("""SELECT COUNT(*) pages,
              COALESCE(SUM(provider_count),0) records,
              COALESCE(SUM(accepted_count),0) accepted,
              COALESCE(SUM(duplicate_count),0) duplicates
              FROM provider_log_sync_pages WHERE sync_batch_id=?
              AND tenant_id=? AND workspace_id=?""", (
                job_id, *scope.sql_parameters())).fetchone()
            cost_backfilled = db.execute("""SELECT COUNT(DISTINCT request_id)
              FROM provider_cost_components WHERE sync_batch_id=?
              AND tenant_id=? AND workspace_id=?""", (
                job_id, *scope.sql_parameters())).fetchone()[0]
            batch_matched = db.execute("""SELECT COUNT(*)
              FROM realtime_log_correlations c JOIN realtime_log_records r
              ON r.record_id=c.record_id AND r.tenant_id=c.tenant_id
              AND r.workspace_id=c.workspace_id WHERE r.sync_job_id=?
              AND c.state LIKE 'exact_%' AND c.tenant_id=?
              AND c.workspace_id=?""", (
                job_id, *scope.sql_parameters())).fetchone()[0]
            db.execute("""UPDATE provider_log_sync_batches SET
              provider_records_read=?,provider_records_deduplicated=?,
              matched_request_count=?,unmatched_request_count=?,
              cost_backfilled_count=?,pages_read=?,billing_context_json=?
              WHERE sync_batch_id=? AND tenant_id=? AND workspace_id=?""", (
                int(page_totals["records"]),
                int(page_totals["duplicates"]), int(batch_matched),
                max(0, int(page_totals["accepted"]) - int(batch_matched)),
                int(cost_backfilled), int(page_totals["pages"]),
                json.dumps({
                    "quota_per_unit": (billing_context or {}).get(
                        "quota_per_unit"),
                    "quota_display_type": (billing_context or {}).get(
                        "quota_display_type"),
                    "usd_exchange_rate": (billing_context or {}).get(
                        "usd_exchange_rate"),
                    "quota_unit": "provider_internal_quota",
                    "precision": 6, "rounding_mode": "ROUND_HALF_UP",
                    "billing_scope":
                        "provider_quota_includes_input_output_cache",
                }, ensure_ascii=False, sort_keys=True),
                job_id, *scope.sql_parameters()))
        updated = self.update_job(
            job_id, last_poll_at=utcnow(), last_successful_poll_at=utcnow(),
            last_successful_read_at=utcnow(),
            last_observed_log_time=last_time,
            collected_count=job["collected_count"] + collected,
            inserted_count=job["inserted_count"] + inserted,
            duplicate_count=job["duplicate_count"] + duplicates,
            rejected_count=job["rejected_count"] + rejected,
            out_of_range_count=job["out_of_range_count"] + out_of_range,
            missing_timestamp_count=job["missing_timestamp_count"] + missing_timestamp,
            correlated_count=job["correlated_count"] + correlated,
            ambiguous_count=job["ambiguous_count"] + ambiguous,
            schema_status=(
                mapping_error or (
                    "adapter_ready_empty" if persistent_domestic and adapter
                    and not records else "observed")),
            pages_read=int(job.get("pages_read") or 0) + 1,
            current_page=(
                int(pagination.get("page", job.get("current_page") or 1))
                if isinstance(pagination, dict)
                else int(job.get("current_page") or 1)
            ),
            consecutive_empty_reads=(
                int(job.get("consecutive_empty_reads") or 0) + 1
                if empty_read else 0
            ),
            consecutive_failures=0,
            last_response_envelope_sha256=response_envelope_sha256,
            source_cursor=(
                json.dumps(pagination, ensure_ascii=False, sort_keys=True)
                if pagination is not None else job.get("source_cursor")
            ),
            watermark_utc=(
                max(str(job.get("watermark_utc") or ""), last_time)
                if last_time else job.get("watermark_utc")
            ),
            network_called=1,
            _lease_generation=lease_generation,
            principal=principal,
        )
        self.event(job_id, job["environment_id"], "poll_completed", {
            "collected": collected, "inserted": inserted, "duplicates": duplicates,
            "rejected": rejected, "correlated": correlated, "ambiguous": ambiguous,
            "out_of_range": out_of_range,
            "missing_timestamp": missing_timestamp,
            "schema_mapping_required": bool(schema_rejected),
            "schema_fingerprint": (
                observation.fingerprint if observation else None),
            "schema_adapter_id": (
                adapter.get("schema_adapter_id") if adapter else None),
            "adapter_version": (
                adapter.get("adapter_version") if adapter else None),
            "last_rejection_reason": mapping_error,
            "bounded_overflow_count": bounded_overflow_count,
        }, lease_generation=lease_generation, principal=principal)
        return {
            **updated,
            "schema_summary": summary,
            "schema_mapping_required": bool(schema_rejected),
            "schema_fingerprint": (
                observation.fingerprint if observation else None),
            "schema_adapter_id": (
                adapter.get("schema_adapter_id") if adapter else None),
            "adapter_version": (
                adapter.get("adapter_version") if adapter else None),
            "last_rejection_reason": mapping_error,
            "bounded_overflow_count": bounded_overflow_count,
            "poll_out_of_range_count": out_of_range,
            "poll_missing_timestamp_count": missing_timestamp,
            "pagination": pagination,
            "response_envelope_sha256": response_envelope_sha256,
        }

    def finalize_batch(
        self, job_id: str, end_reason: str,
        *, principal: PrincipalContext | None = None,
        permission: str = "evidence.import",
    ) -> dict[str, Any]:
        """Seal an incremental Provider batch without changing source evidence."""
        scope = self._scope(principal, permission)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT * FROM realtime_log_sync_jobs
              WHERE sync_job_id=? AND tenant_id=? AND workspace_id=?""", (
                job_id, *scope.sql_parameters())).fetchone()
            batch = db.execute("""SELECT * FROM provider_log_sync_batches
              WHERE sync_batch_id=? AND tenant_id=? AND workspace_id=?""", (
                job_id, *scope.sql_parameters())).fetchone()
            if not job or not batch:
                raise LookupError("provider_sync_batch_not_found")
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(standardized_call_logs)")}
            final_pending = new_during = 0
            if {"cost_status", "traffic_class",
                    "provider_sync_failure_reason"}.issubset(columns):
                final_pending = int(db.execute("""SELECT COUNT(*) FROM
                  standardized_call_logs WHERE tenant_id=? AND workspace_id=?
                  AND environment_id=? AND source_type='realtime_execution'
                  AND traffic_class='business'
                  AND cost_status='pending_provider_sync'
                  AND request_id IS NOT NULL AND TRIM(request_id)<>''""", (
                    *scope.sql_parameters(), job["environment_id"])).fetchone()[0])
                new_during = int(db.execute("""SELECT COUNT(*) FROM
                  standardized_call_logs WHERE tenant_id=? AND workspace_id=?
                  AND environment_id=? AND source_type='realtime_execution'
                  AND traffic_class='business' AND created_at>=?""", (
                    *scope.sql_parameters(), job["environment_id"],
                    batch["started_at"])).fetchone()[0])
                provider_max = db.execute("""SELECT MAX(platform_created_at)
                  FROM realtime_log_records WHERE sync_job_id=? AND tenant_id=?
                  AND workspace_id=?""", (
                    job_id, *scope.sql_parameters())).fetchone()[0]
                if str(job["error_code"] or "").endswith(
                        "reauthentication_required"):
                    reason = "authentication_failed"
                elif end_reason in {"log_sync_maximum_pages_reached",
                                    "pagination_incomplete"}:
                    reason = "pagination_incomplete"
                else:
                    reason = "provider_exact_identifier_not_found"
                db.execute("""UPDATE standardized_call_logs SET
                  provider_sync_failure_reason=CASE
                    WHEN request_id IS NULL OR TRIM(request_id)='' THEN
                      'request_id_missing'
                    WHEN COALESCE(provider_request_id,provider_response_id,
                                  provider_trace_id,client_correlation_id) IS NULL THEN
                      'historical_provider_identifier_not_captured'
                    WHEN ? IS NOT NULL AND occurred_at>? THEN
                      'log_not_yet_generated'
                    ELSE ? END,
                  provider_sync_batch_id=COALESCE(provider_sync_batch_id,?),
                  updated_at=? WHERE tenant_id=? AND workspace_id=?
                  AND environment_id=? AND source_type='realtime_execution'
                  AND traffic_class='business'
                  AND cost_status='pending_provider_sync'""", (
                    provider_max, provider_max, reason, job_id, utcnow(),
                    *scope.sql_parameters(), job["environment_id"]))
            cursor = db.execute("""SELECT cursor_kind,cursor_value,updated_at,
              sync_job_id FROM realtime_log_sync_cursors WHERE environment_id=?
              AND tenant_id=? AND workspace_id=?""", (
                job["environment_id"], *scope.sql_parameters())).fetchone()
            db.execute("""UPDATE provider_log_sync_batches SET finished_at=?,
              final_watermark_json=?,pending_created_during_sync=?,
              final_pending_count=?,end_reason=? WHERE sync_batch_id=?
              AND tenant_id=? AND workspace_id=?""", (
                utcnow(), json.dumps({
                    "provider_log_id": (cursor["cursor_value"] if cursor and
                        cursor["cursor_kind"] == "platform_log_id" else None),
                    "provider_log_time": job["watermark_utc"],
                    "pagination_cursor": job["source_cursor"],
                    "cursor_kind": cursor["cursor_kind"] if cursor else None,
                    "cursor_value": cursor["cursor_value"] if cursor else None,
                }, ensure_ascii=False, sort_keys=True), new_during,
                final_pending, str(end_reason)[:120], job_id,
                *scope.sql_parameters()))
            result = db.execute("""SELECT * FROM provider_log_sync_batches
              WHERE sync_batch_id=? AND tenant_id=? AND workspace_id=?""", (
                job_id, *scope.sql_parameters())).fetchone()
        return dict(result)

    def events(self, environment_id: str | None = None, after_id: int = 0, *,
               principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._scope(principal, "collector.session.read")
        with self.connect() as db:
            if environment_id:
                rows = db.execute("""SELECT * FROM realtime_log_sync_events
                  WHERE event_id>? AND environment_id=? AND tenant_id=? AND workspace_id=?
                  ORDER BY event_id LIMIT 200""",
                                  (after_id, environment_id, *scope.sql_parameters())).fetchall()
            else:
                rows = db.execute("""SELECT * FROM realtime_log_sync_events
                  WHERE event_id>? AND tenant_id=? AND workspace_id=?
                  ORDER BY event_id LIMIT 200""",
                  (after_id, *scope.sql_parameters())).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])}
                for row in rows]

    def reconciliation(self, environment_id: str, *,
                       principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._scope(principal, "evidence.read")
        executions = {row["execution_id"]: row for row in self._executions()}
        with self.connect() as db:
            rows = db.execute("""SELECT c.*,r.platform_log_id,r.normalized_json
              FROM realtime_log_correlations c JOIN realtime_log_records r
              ON r.record_id=c.record_id AND r.tenant_id=c.tenant_id
                AND r.workspace_id=c.workspace_id
              WHERE c.environment_id=? AND c.tenant_id=? AND c.workspace_id=?
              ORDER BY c.created_at DESC""",
              (environment_id,*scope.sql_parameters())).fetchall()
        items = []
        for row in rows:
            normalized = json.loads(row["normalized_json"])
            execution = executions.get(row["execution_id"] or "", {})
            estimated = execution.get("estimated_cost")
            if estimated is None:
                estimated = execution.get("estimated_cost_cny")
            actual = normalized.get("actual_cost")
            difference = actual - estimated if isinstance(actual, (int, float)) and isinstance(estimated, (int, float)) else None
            status = ("ambiguous" if row["state"] in {"ambiguous", "strong_candidate"}
                      else "actual_cost_unavailable" if actual is None
                      else "cost_confirmed" if difference is None
                      or abs(difference) <= self.cost_tolerance
                      else "cost_mismatch")
            items.append({
                "execution_id": row["execution_id"], "platform_log_id": row["platform_log_id"],
                "estimated_cost": estimated, "actual_cost": actual,
                "currency": normalized.get("currency"), "difference": difference,
                "difference_percent": (difference / estimated * 100)
                if difference is not None and estimated else None,
                "reconciliation_status": status,
                "evidence_source": normalized.get("source_type"),
                "actual_channel_id": normalized.get("actual_channel_id"),
                "actual_channel_name": normalized.get("actual_channel_name"),
                "channel_evidence_status": "confirmed" if normalized.get("actual_channel_id")
                or normalized.get("actual_channel_name") else "channel_evidence_unavailable",
                "scheduler_recommendation": execution.get("scheduler_recommendation"),
            })
        return items

    def health(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.session.read")
        with self.connect() as db:
            jobs = db.execute("""SELECT COUNT(*) FROM realtime_log_sync_jobs
              WHERE tenant_id=? AND workspace_id=?""",scope.sql_parameters()).fetchone()[0]
            records = db.execute("""SELECT COUNT(*) FROM realtime_log_records
              WHERE tenant_id=? AND workspace_id=?""",scope.sql_parameters()).fetchone()[0]
        return {
            "status": "ready", "jobs": jobs, "records": records,
            "completion_api_calls": 0, "cookies_persisted": False,
            "credentials_persisted": False,
        }
