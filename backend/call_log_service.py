"""Tenant-scoped historical and realtime model-call ledger.

The import boundary intentionally never persists raw API-key or IP values.
Historical rows without authoritative channel identifiers are model-usage
evidence only and are excluded from every channel-health query.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import secrets
import sqlite3
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from backend.tenant_security import TenantScope

CHINA_ZONE = ZoneInfo("Asia/Shanghai")
EXPECTED_HEADERS = (
    "时间", "API Key", "类型", "模型", "用时/首字", "输入", "输出", "花费", "IP", "详情",
)
MODEL_PAIR = re.compile(
    r"^请求并计费模型:\s*(?P<requested>[^;]+);\s*实际模型:\s*(?P<actual>.+)$"
)
LATENCY = re.compile(
    r"^(?P<total>\d+(?:\.\d+)?)s(?:\s*/\s*(?P<ttft>\d+(?:\.\d+)?)s)?\s*\((?P<mode>流|非流)\)$"
)
INPUT_TOKENS = re.compile(r"^(?P<input>\d+)(?:\s*\(缓存读:\s*(?P<cached>\d+)\))?$")
COST = re.compile(r"^¥(?P<amount>\d+(?:\.\d+)?)$")
SENSITIVE = re.compile(
    r"(?i)(authorization|bearer\s+[A-Za-z0-9._~+/-]+|api[_ -]?key|cookie|password|token\s*[:=])"
)


class CallLogError(ValueError):
    """Stable fail-closed error without source-row content."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _safe_detail(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if SENSITIVE.search(text):
        return "[REDACTED]"
    return text[:500]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


class CallLogService:
    schema_version = "standardized_call_log_v3"

    def __init__(self, path: Path, scope: TenantScope | None = None):
        self.path = Path(path)
        self.scope = scope or TenantScope.local_development()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS call_log_secrets(
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              digest_salt TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id));
            CREATE TABLE IF NOT EXISTS call_log_import_batches(
              import_batch_id TEXT NOT NULL, file_sha256 TEXT NOT NULL,
              file_size INTEGER NOT NULL, source_row_count INTEGER NOT NULL,
              imported_count INTEGER NOT NULL, rejected_count INTEGER NOT NULL,
              report_json TEXT NOT NULL, imported_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,import_batch_id),
              UNIQUE(tenant_id,workspace_id,file_sha256));
            CREATE TABLE IF NOT EXISTS standardized_call_logs(
              cursor_id INTEGER PRIMARY KEY AUTOINCREMENT,
              record_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
              request_id TEXT, response_id TEXT, decision_id TEXT,
              local_request_id TEXT, provider_request_id TEXT,
              provider_response_id TEXT, provider_trace_id TEXT,
              client_correlation_id TEXT,
              environment_id TEXT NOT NULL, requested_model TEXT NOT NULL,
              actual_model TEXT, provider TEXT, channel_id TEXT, channel_name TEXT,
              endpoint_type TEXT, stream INTEGER NOT NULL,
              request_status TEXT NOT NULL, http_status INTEGER,
              error_code TEXT, error_category TEXT, retryable INTEGER,
              attempt_number INTEGER NOT NULL, total_attempts INTEGER NOT NULL,
              total_latency_ms REAL, first_token_latency_ms REAL,
              input_tokens INTEGER, cached_input_tokens INTEGER,
              output_tokens INTEGER, cost_amount TEXT, currency TEXT,
              configuration_version TEXT, metric_snapshot_id TEXT,
              source_type TEXT NOT NULL, evidence_scope TEXT NOT NULL,
              is_historical INTEGER NOT NULL, api_key_alias TEXT,
              source_ip TEXT, source_ip_digest TEXT, raw_detail TEXT,
              source_record_id TEXT, billed_model TEXT, pricing_detail TEXT,
              price_source TEXT, price_version TEXT, duration_type TEXT,
              evidence_level TEXT, provider_log_id TEXT,
              dedup_fingerprint TEXT, duplicate_of TEXT,
              duplicate_status TEXT NOT NULL DEFAULT 'canonical',
              duplicate_reason TEXT, channel_source TEXT NOT NULL DEFAULT 'unknown',
              import_batch_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              acceptance_run_id TEXT, error_source TEXT,
              is_fault_injected INTEGER, fault_id TEXT, fallback_used INTEGER,
              output_started INTEGER, traffic_proposal_id TEXT,
              strategy_variant TEXT, cost_source TEXT,
              traffic_class TEXT, cost_status TEXT, cost_type TEXT,
              provider_cost_amount_exact TEXT, provider_raw_quota TEXT,
              provider_quota_unit TEXT, provider_conversion_rate TEXT,
              provider_cost_precision INTEGER, provider_rounding_mode TEXT,
              provider_cost_source_reference TEXT, provider_sync_batch_id TEXT,
              provider_cost_components_json TEXT, provider_cost_updated_at TEXT,
              provider_sync_failure_reason TEXT,
              provider_identifiers_json TEXT,
              match_method TEXT, matched_field TEXT,
              matched_value_hash TEXT, match_confidence TEXT,
              provider_log_ids TEXT, matched_at TEXT,
              strategy_effect_run_id TEXT, probe_run_id TEXT,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              UNIQUE(tenant_id,workspace_id,record_id));
            CREATE TABLE IF NOT EXISTS call_attempt_logs(
              attempt_id TEXT NOT NULL, request_id TEXT NOT NULL,
              local_request_id TEXT, provider_request_id TEXT,
              provider_response_id TEXT, provider_trace_id TEXT,
              client_correlation_id TEXT, provider_log_id TEXT,
              attempt_number INTEGER NOT NULL, requested_model TEXT NOT NULL,
              actual_model TEXT, channel_id TEXT, channel_name TEXT,
              status TEXT NOT NULL, http_status INTEGER, error_code TEXT,
              error_category TEXT, retryable INTEGER, total_latency_ms REAL,
              first_token_latency_ms REAL, input_tokens INTEGER,
              cached_input_tokens INTEGER, output_tokens INTEGER,
              cost_amount TEXT, currency TEXT, started_at TEXT NOT NULL,
              completed_at TEXT, acceptance_run_id TEXT, error_source TEXT,
              is_fault_injected INTEGER, fault_id TEXT, fallback_used INTEGER,
              output_started INTEGER, traffic_proposal_id TEXT,
              strategy_variant TEXT, cost_source TEXT,
              cost_status TEXT, cost_type TEXT,
              provider_cost_amount_exact TEXT, provider_sync_batch_id TEXT,
              strategy_effect_run_id TEXT, probe_run_id TEXT, traffic_class TEXT,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,attempt_id),
              UNIQUE(tenant_id,workspace_id,request_id,attempt_number));
            CREATE TABLE IF NOT EXISTS call_log_changes(
              change_id INTEGER PRIMARY KEY AUTOINCREMENT,
              record_id TEXT NOT NULL, changed_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS routing_decision_logs(
              decision_id TEXT NOT NULL, request_id TEXT NOT NULL,
              decision_type TEXT NOT NULL, evidence_json TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,decision_id),
              UNIQUE(tenant_id,workspace_id,request_id));
            CREATE INDEX IF NOT EXISTS ix_call_logs_scope_time
              ON standardized_call_logs(tenant_id,workspace_id,occurred_at,cursor_id);
            CREATE INDEX IF NOT EXISTS ix_call_logs_scope_model
              ON standardized_call_logs(tenant_id,workspace_id,environment_id,requested_model);
            CREATE INDEX IF NOT EXISTS ix_call_logs_scope_channel
              ON standardized_call_logs(tenant_id,workspace_id,environment_id,channel_id,occurred_at);
            CREATE INDEX IF NOT EXISTS ix_call_log_changes_scope
              ON call_log_changes(tenant_id,workspace_id,change_id);
            CREATE INDEX IF NOT EXISTS ix_routing_decisions_request
              ON routing_decision_logs(tenant_id,workspace_id,request_id);
            """)
            call_log_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(standardized_call_logs)"
            )}
            for name in (
                "source_record_id", "billed_model", "pricing_detail", "price_source",
                "price_version", "duration_type", "evidence_level", "provider_log_id",
                "dedup_fingerprint", "duplicate_of", "duplicate_status",
                "duplicate_reason", "channel_source",
                "acceptance_run_id", "error_source", "is_fault_injected",
                "fault_id", "fallback_used", "output_started",
                "traffic_proposal_id", "strategy_variant", "cost_source",
                "traffic_class", "cost_status", "cost_type",
                "provider_cost_amount_exact", "provider_raw_quota",
                "provider_quota_unit", "provider_conversion_rate",
                "provider_cost_precision", "provider_rounding_mode",
                "provider_cost_source_reference", "provider_sync_batch_id",
                "provider_cost_components_json", "provider_cost_updated_at",
                "provider_sync_failure_reason",
                "provider_identifiers_json",
                "local_request_id", "provider_request_id",
                "provider_response_id", "provider_trace_id",
                "client_correlation_id", "match_method", "matched_field",
                "matched_value_hash", "match_confidence", "provider_log_ids",
                "matched_at",
                "strategy_effect_run_id", "probe_run_id",
            ):
                if name not in call_log_columns:
                    default = " NOT NULL DEFAULT 'canonical'" if name == "duplicate_status" else (
                        " NOT NULL DEFAULT 'unknown'" if name == "channel_source" else "")
                    sql_type = "INTEGER" if name in {
                        "is_fault_injected", "fallback_used", "output_started",
                        "provider_cost_precision",
                    } else "TEXT"
                    db.execute(
                        f"ALTER TABLE standardized_call_logs ADD COLUMN {name} {sql_type}{default}"
                    )
            attempt_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(call_attempt_logs)"
            )}
            for name, sql_type in (
                ("acceptance_run_id", "TEXT"), ("error_source", "TEXT"),
                ("is_fault_injected", "INTEGER"), ("fault_id", "TEXT"),
                ("fallback_used", "INTEGER"), ("output_started", "INTEGER"),
                ("traffic_proposal_id", "TEXT"), ("strategy_variant", "TEXT"),
                ("cost_source", "TEXT"),
                ("cost_status", "TEXT"), ("cost_type", "TEXT"),
                ("provider_cost_amount_exact", "TEXT"),
                ("provider_sync_batch_id", "TEXT"),
                ("strategy_effect_run_id", "TEXT"), ("probe_run_id", "TEXT"),
                ("traffic_class", "TEXT"),
                ("local_request_id", "TEXT"),
                ("provider_request_id", "TEXT"),
                ("provider_response_id", "TEXT"),
                ("provider_trace_id", "TEXT"),
                ("client_correlation_id", "TEXT"),
                ("provider_log_id", "TEXT"),
            ):
                if name not in attempt_columns:
                    db.execute(f"ALTER TABLE call_attempt_logs ADD COLUMN {name} {sql_type}")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_call_logs_acceptance_run
              ON standardized_call_logs(tenant_id,workspace_id,acceptance_run_id)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_call_logs_provider_request
              ON standardized_call_logs(tenant_id,workspace_id,provider_request_id)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_call_logs_provider_response
              ON standardized_call_logs(tenant_id,workspace_id,provider_response_id)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_call_logs_provider_trace
              ON standardized_call_logs(tenant_id,workspace_id,provider_trace_id)""")
            db.execute("""UPDATE standardized_call_logs SET
              local_request_id=COALESCE(local_request_id,request_id)
              WHERE source_type='realtime_execution' AND request_id IS NOT NULL
              AND tenant_id=? AND workspace_id=?""", self.scope.sql_parameters())
            db.execute("""UPDATE call_attempt_logs SET
              local_request_id=COALESCE(local_request_id,request_id)
              WHERE request_id IS NOT NULL AND tenant_id=? AND workspace_id=?""",
              self.scope.sql_parameters())
            db.execute("""UPDATE standardized_call_logs SET
              cost_status=COALESCE(cost_status,'pending_provider_sync'),
              cost_type=COALESCE(cost_type,'pending_provider_sync'),
              provider_sync_failure_reason=CASE
                WHEN COALESCE(provider_request_id,provider_response_id,
                              provider_trace_id,client_correlation_id) IS NULL
                  THEN COALESCE(provider_sync_failure_reason,
                                'historical_provider_identifier_not_captured')
                ELSE provider_sync_failure_reason END
              WHERE source_type='realtime_execution'
              AND COALESCE(traffic_class,'business')='business'
              AND cost_amount IS NULL AND tenant_id=? AND workspace_id=?""",
              self.scope.sql_parameters())
            db.execute("""UPDATE call_attempt_logs SET
              cost_status=COALESCE(cost_status,'pending_provider_sync'),
              cost_type=COALESCE(cost_type,'pending_provider_sync')
              WHERE cost_amount IS NULL AND COALESCE(traffic_class,'business')='business'
              AND tenant_id=? AND workspace_id=?""",
              self.scope.sql_parameters())
            db.execute("""UPDATE standardized_call_logs SET
              source_record_id=COALESCE(source_record_id,record_id),
              billed_model=COALESCE(billed_model,requested_model),
              pricing_detail=CASE WHEN source_type='historical_uat_csv'
                THEN COALESCE(pricing_detail,raw_detail) ELSE pricing_detail END,
              price_source=CASE WHEN source_type='historical_uat_csv'
                THEN COALESCE(price_source,'historical_log_detail') ELSE price_source END,
              price_version=CASE WHEN source_type='historical_uat_csv'
                THEN COALESCE(price_version,'unversioned_historical_snapshot') ELSE price_version END,
              duration_type=COALESCE(duration_type,
                CASE WHEN stream=1 AND first_token_latency_ms IS NOT NULL
                  THEN 'total_and_first_token' ELSE 'total_duration' END),
              evidence_level=COALESCE(evidence_level,
                CASE WHEN request_id IS NOT NULL AND decision_id IS NOT NULL
                  THEN 'exact_decision' WHEN source_type='historical_uat_csv'
                  THEN 'historical_statistics' ELSE 'execution_only' END)
              WHERE tenant_id=? AND workspace_id=?""", self.scope.sql_parameters())
            db.execute("""UPDATE standardized_call_logs SET channel_source=CASE
              WHEN channel_id IS NULL THEN 'unknown'
              WHEN source_type='realtime_execution' AND decision_id IS NOT NULL THEN 'scheduler_decision'
              ELSE 'unknown' END
              WHERE tenant_id=? AND workspace_id=? AND
                (channel_source IS NULL OR channel_source NOT IN
                 ('scheduler_decision','provider_log','request_id_exact_match','manually_confirmed','unknown'))""",
              self.scope.sql_parameters())
            db.execute("""UPDATE standardized_call_logs SET
              traffic_class=COALESCE(traffic_class,CASE
                WHEN COALESCE(is_fault_injected,0)=1 THEN 'uat_fault_injection'
                WHEN source_type='realtime_execution' THEN 'business'
                WHEN source_type='historical_uat_csv' THEN 'historical'
                ELSE 'other' END),
              cost_status=COALESCE(cost_status,CASE
                WHEN cost_amount IS NULL AND source_type='realtime_execution'
                  THEN 'pending_provider_sync'
                WHEN cost_source IN ('provider_log','provider_usage','actual_provider_cost')
                  THEN 'provider_actual'
                WHEN source_type='historical_uat_csv' AND cost_amount IS NOT NULL
                  THEN 'provider_actual'
                WHEN cost_source='estimated_versioned_price'
                  THEN 'estimated_versioned_price'
                WHEN cost_amount IS NOT NULL THEN 'available'
                ELSE 'not_available' END),
              cost_type=COALESCE(cost_type,CASE
                WHEN cost_source IN ('provider_log','provider_usage','actual_provider_cost')
                  OR (source_type='historical_uat_csv' AND cost_amount IS NOT NULL)
                  THEN 'provider_actual'
                WHEN cost_source='estimated_versioned_price'
                  THEN 'estimated_versioned_price'
                WHEN cost_amount IS NULL THEN 'pending_provider_sync'
                ELSE 'recorded_cost' END),
              provider_cost_amount_exact=CASE
                WHEN cost_status='provider_actual' AND provider_cost_amount_exact IS NULL
                  THEN cost_amount ELSE provider_cost_amount_exact END
              WHERE tenant_id=? AND workspace_id=?""", self.scope.sql_parameters())
            existing_changes = db.execute(
                "SELECT COUNT(*) FROM call_log_changes WHERE tenant_id=? AND workspace_id=?",
                self.scope.sql_parameters(),
            ).fetchone()[0]
            if not existing_changes:
                db.execute("""INSERT INTO call_log_changes(record_id,changed_at,tenant_id,workspace_id)
                  SELECT record_id,updated_at,tenant_id,workspace_id FROM standardized_call_logs
                  WHERE tenant_id=? AND workspace_id=? ORDER BY cursor_id""",
                  self.scope.sql_parameters())
            # Workbench versions before 2026-08-05 could attach an automatic
            # catalog model to non-model GET/HEAD/OPTIONS requests.  Correct
            # only that derived classification; request IDs and raw timing
            # evidence remain untouched and a new change cursor is emitted.
            misclassified = db.execute("""SELECT record_id FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND source_type='realtime_execution'
              AND endpoint_type IN ('uat_http_get','uat_http_head','uat_http_options')
              AND requested_model<>'non_model_uat_http'""",self.scope.sql_parameters()).fetchall()
            if misclassified:
                changed_at=_utc_now()
                for row in misclassified:
                    db.execute("""UPDATE standardized_call_logs SET requested_model='non_model_uat_http',
                      actual_model=NULL,updated_at=? WHERE record_id=? AND tenant_id=? AND workspace_id=?""",
                      (changed_at,row["record_id"],*self.scope.sql_parameters()))
                    self._record_change(db,str(row["record_id"]),changed_at)
        self.reconcile_duplicates()

    @staticmethod
    def _dedup_fingerprint(row: dict[str, Any] | sqlite3.Row) -> str:
        def normalized_number(value: Any) -> str | None:
            if value is None or value == "":
                return None
            try:
                return format(Decimal(str(value)).normalize(), "f")
            except (InvalidOperation, ValueError):
                return str(value).strip()
        payload = {
            "occurred_at": str(row["occurred_at"] or "").strip(),
            "actual_model": str(row["actual_model"] or row["requested_model"] or "").strip(),
            "stream": bool(row["stream"]),
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "actual_cost": normalized_number(row["cost_amount"]),
            "duration_ms": normalized_number(row["total_latency_ms"]),
            "status": str(row["request_status"] or "").upper(),
            "error_category": str(row["error_category"] or "").strip() or None,
        }
        return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest().upper()

    @staticmethod
    def _evidence_rank(row: sqlite3.Row) -> tuple[int, int, int, int]:
        return (
            int(str(row["source_type"]) == "realtime_execution"),
            sum(bool(row[name]) for name in ("provider_log_id", "request_id", "response_id", "decision_id")),
            int(bool(row["channel_id"])),
            int(row["cursor_id"]),
        )

    def reconcile_duplicates(self) -> dict[str, int]:
        """Link exact duplicates without deleting evidence; candidates remain counted."""
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? ORDER BY cursor_id""",
              self.scope.sql_parameters()).fetchall()
            for row in rows:
                db.execute("""UPDATE standardized_call_logs SET dedup_fingerprint=?,
                  duplicate_of=NULL,duplicate_status='canonical',duplicate_reason=NULL
                  WHERE cursor_id=?""", (self._dedup_fingerprint(row), row["cursor_id"]))
            rows = db.execute("""SELECT * FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? ORDER BY cursor_id""",
              self.scope.sql_parameters()).fetchall()
            groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
            for row in rows:
                identities = []
                for key in ("provider_log_id", "request_id", "response_id"):
                    if row[key]: identities.append((key, str(row[key])))
                # The stable fingerprint is the final fallback only when the
                # provider supplied no authoritative identifier. Two distinct
                # Request IDs with the same usage values are separate calls.
                identities.append(("fingerprint", str(row["dedup_fingerprint"])))
                for identity in identities:
                    groups.setdefault(identity, []).append(row)
            linked: set[int] = set()
            for (kind, _), members in groups.items():
                unique = {int(row["cursor_id"]): row for row in members}
                if len(unique) < 2:
                    continue
                if kind == "fingerprint":
                    authoritative = {
                        (str(row["provider_log_id"] or ""),
                         str(row["request_id"] or ""),
                         str(row["response_id"] or ""))
                        for row in unique.values()
                        if any(row[key] for key in
                               ("provider_log_id", "request_id", "response_id"))
                    }
                    # A fingerprint may bridge one identifier-free historical
                    # row to one richer realtime row. It must never collapse
                    # two calls carrying different authoritative identities.
                    if len(authoritative) > 1:
                        continue
                canonical = max(unique.values(), key=self._evidence_rank)
                for row in unique.values():
                    cursor_id = int(row["cursor_id"])
                    if cursor_id == int(canonical["cursor_id"]) or cursor_id in linked:
                        continue
                    db.execute("""UPDATE standardized_call_logs SET duplicate_of=?,
                      duplicate_status='exact_duplicate',duplicate_reason=? WHERE cursor_id=?""",
                      (canonical["record_id"], f"exact_{kind}", cursor_id))
                    linked.add(cursor_id)
            # A cross-source near match is only flagged for review. It remains
            # canonical and participates in statistics until an exact identity
            # or full stable fingerprint proves duplication.
            canonical_rows = [row for row in rows if int(row["cursor_id"]) not in linked]
            possible: dict[tuple[Any, ...], list[sqlite3.Row]] = {}
            for row in canonical_rows:
                key = (
                    str(row["actual_model"] or row["requested_model"] or "").strip(),
                    bool(row["stream"]), row["input_tokens"], row["output_tokens"],
                    str(row["cost_amount"] or ""), str(row["request_status"] or ""),
                    str(row["error_category"] or ""),
                )
                possible.setdefault(key, []).append(row)
            candidate_ids: set[int] = set()
            for members in possible.values():
                if len(members) < 2 or len({str(row["source_type"]) for row in members}) < 2:
                    continue
                for row in members:
                    try:
                        moment = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if any(other["cursor_id"] != row["cursor_id"] and
                           str(other["dedup_fingerprint"]) != str(row["dedup_fingerprint"]) and
                           abs((moment - datetime.fromisoformat(str(other["occurred_at"]).replace("Z", "+00:00"))).total_seconds()) <= 300
                           for other in members):
                        candidate_ids.add(int(row["cursor_id"]))
            for cursor_id in candidate_ids:
                db.execute("""UPDATE standardized_call_logs SET
                  duplicate_status='duplicate_candidate',duplicate_reason='cross_source_near_match'
                  WHERE cursor_id=?""", (cursor_id,))
            exact = int(db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND duplicate_status='exact_duplicate'""",
              self.scope.sql_parameters()).fetchone()[0])
            candidates = int(db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND duplicate_status='duplicate_candidate'""",
              self.scope.sql_parameters()).fetchone()[0])
            return {"raw_count": len(rows), "exact_duplicate_count": exact,
                    "duplicate_candidate_count": candidates, "effective_count": len(rows) - exact}

    def _record_change(self, db: sqlite3.Connection, record_id: str, changed_at: str) -> None:
        db.execute("""INSERT INTO call_log_changes(
          record_id,changed_at,tenant_id,workspace_id) VALUES(?,?,?,?)""",
          (record_id, changed_at, *self.scope.sql_parameters()))

    def _salt(self, db: sqlite3.Connection) -> str:
        row = db.execute(
            "SELECT digest_salt FROM call_log_secrets WHERE tenant_id=? AND workspace_id=?",
            self.scope.sql_parameters(),
        ).fetchone()
        if row:
            return str(row[0])
        salt = secrets.token_hex(32)
        db.execute(
            "INSERT INTO call_log_secrets(tenant_id,workspace_id,digest_salt) VALUES(?,?,?)",
            (*self.scope.sql_parameters(), salt),
        )
        return salt

    @staticmethod
    def _digest(salt: str, value: str, prefix: str) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        digest = hashlib.sha256(f"{salt}|{text}".encode("utf-8")).hexdigest()[:24].upper()
        return f"{prefix}-{digest}"

    @staticmethod
    def _parse_model(text: str) -> tuple[str, str, str]:
        value = text.strip()
        pair = MODEL_PAIR.fullmatch(value)
        if pair:
            requested = pair.group("requested").strip()
            return requested, requested, pair.group("actual").strip()
        if not value or ";" in value or ":" in value:
            raise CallLogError("historical_model_format_unknown")
        return value, value, value

    @staticmethod
    def _parse_historical(row: dict[str, str]) -> dict[str, Any]:
        try:
            local = datetime.strptime(row["时间"].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA_ZONE)
        except (ValueError, KeyError) as exc:
            raise CallLogError("historical_timestamp_invalid") from exc
        kind = row["类型"].strip()
        if kind not in {"消费", "错误"}:
            raise CallLogError("historical_record_type_unknown")
        requested, billed, actual = CallLogService._parse_model(row["模型"])
        latency = LATENCY.fullmatch(row["用时/首字"].strip())
        if not latency:
            raise CallLogError("historical_latency_invalid")
        stream = latency.group("mode") == "流"
        if stream != bool(latency.group("ttft")):
            raise CallLogError("historical_stream_ttft_inconsistent")
        tokens = INPUT_TOKENS.fullmatch(row["输入"].strip())
        if not tokens or not row["输出"].strip().isdigit():
            raise CallLogError("historical_tokens_invalid")
        cost = COST.fullmatch(row["花费"].strip())
        if not cost:
            raise CallLogError("historical_cost_invalid")
        try:
            amount = Decimal(cost.group("amount"))
        except InvalidOperation as exc:
            raise CallLogError("historical_cost_invalid") from exc
        if not amount.is_finite() or amount < 0:
            raise CallLogError("historical_cost_invalid")
        return {
            "occurred_at": local.astimezone(timezone.utc).isoformat(),
            "requested_model": requested, "billed_model": billed, "actual_model": actual,
            "stream": stream, "request_status": "FAILED" if kind == "错误" else "SUCCESS",
            "error_category": "historical_platform_error" if kind == "错误" else None,
            "total_latency_ms": float(Decimal(latency.group("total")) * 1000),
            "first_token_latency_ms": (
                float(Decimal(latency.group("ttft")) * 1000) if latency.group("ttft") else None
            ),
            "input_tokens": int(tokens.group("input")),
            "cached_input_tokens": int(tokens.group("cached") or 0),
            "output_tokens": int(row["输出"].strip()),
            "cost_amount": format(amount, "f"), "currency": "CNY",
            "raw_detail": _safe_detail(row.get("详情", "")),
            "pricing_detail": _safe_detail(row.get("详情", "")),
            "price_source": "historical_log_detail",
            "price_version": "unversioned_historical_snapshot",
            "duration_type": "total_and_first_token" if stream else "total_duration",
            "evidence_level": "historical_statistics",
        }

    def initialize_historical_csv(self, source_path: Path) -> dict[str, Any]:
        source = Path(source_path)
        raw = source.read_bytes()
        sha = hashlib.sha256(raw).hexdigest().upper()
        batch_id = f"HIST-CHINA-UAT-{sha[:20]}"
        imported_at = _utc_now()
        with self.connect() as db:
            existing = db.execute(
                "SELECT report_json FROM call_log_import_batches WHERE tenant_id=? AND workspace_id=? AND file_sha256=?",
                (*self.scope.sql_parameters(), sha),
            ).fetchone()
            if existing:
                result = json.loads(existing[0])
                return {**result, "idempotent": True, "inserted_count": 0}
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(text.splitlines())
            if tuple(reader.fieldnames or ()) != EXPECTED_HEADERS:
                raise CallLogError("historical_csv_header_mismatch")
            rows = list(reader)
            rejected: list[dict[str, Any]] = []
            prepared: list[tuple[Any, ...]] = []
            db.execute("BEGIN IMMEDIATE")
            salt = self._salt(db)
            for index, row in enumerate(rows, start=1):
                try:
                    parsed = self._parse_historical(row)
                except CallLogError as exc:
                    rejected.append({"row_number": index + 1, "reason_code": str(exc)})
                    continue
                record_id = f"HIST-{hashlib.sha256(f'{sha}:{index}'.encode()).hexdigest()[:24].upper()}"
                prepared.append((
                    record_id, parsed["occurred_at"], None, None, None, "china_uat",
                    parsed["requested_model"], parsed["actual_model"], None, None, None, None,
                    int(parsed["stream"]), parsed["request_status"], None, None,
                    parsed["error_category"], 0, 1, 1, parsed["total_latency_ms"],
                    parsed["first_token_latency_ms"], parsed["input_tokens"],
                    parsed["cached_input_tokens"], parsed["output_tokens"],
                    parsed["cost_amount"], "CNY", None, None, "historical_uat_csv",
                    "model_usage_only", 1,
                    self._digest(salt, row.get("API Key", ""), "AKA"), None,
                    self._digest(salt, row.get("IP", ""), "IPD"), parsed["raw_detail"],
                    record_id, parsed["billed_model"], parsed["pricing_detail"],
                    parsed["price_source"], parsed["price_version"],
                    parsed["duration_type"], parsed["evidence_level"],
                    batch_id, imported_at, imported_at, *self.scope.sql_parameters(),
                ))
            if rejected:
                db.rollback()
                raise CallLogError(f"historical_csv_rejected_rows:{len(rejected)}")
            db.executemany("""INSERT INTO standardized_call_logs(
              record_id,occurred_at,request_id,response_id,decision_id,environment_id,
              requested_model,actual_model,provider,channel_id,channel_name,endpoint_type,
              stream,request_status,http_status,error_code,error_category,retryable,
              attempt_number,total_attempts,total_latency_ms,first_token_latency_ms,
              input_tokens,cached_input_tokens,output_tokens,cost_amount,currency,
              configuration_version,metric_snapshot_id,source_type,evidence_scope,is_historical,
              api_key_alias,source_ip,source_ip_digest,raw_detail,
              source_record_id,billed_model,pricing_detail,price_source,price_version,
              duration_type,evidence_level,import_batch_id,
              created_at,updated_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", prepared)
            db.executemany("""INSERT INTO call_log_changes(
              record_id,changed_at,tenant_id,workspace_id) VALUES(?,?,?,?)""",
              [(row[0], imported_at, *self.scope.sql_parameters()) for row in prepared])
            report = {
                "status": "imported", "import_batch_id": batch_id, "file_sha256": sha,
                "file_size": len(raw), "source_row_count": len(rows),
                "imported_count": len(prepared), "rejected_count": 0,
                "schema_version": self.schema_version,
                "sensitive_fields": {"api_key": "salted_alias_only", "ip": "salted_digest_only"},
                "imported_at": imported_at,
            }
            db.execute("""INSERT INTO call_log_import_batches(
              import_batch_id,file_sha256,file_size,source_row_count,imported_count,
              rejected_count,report_json,imported_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                batch_id, sha, len(raw), len(rows), len(prepared), 0,
                _canonical(report), imported_at, *self.scope.sql_parameters(),
            ))
        return {**report, "idempotent": False, "inserted_count": len(prepared)}

    def import_normalized_rows(self, rows: list[dict[str, Any]], *,
                               environment_id: str, source_type: str,
                               import_batch_id: str) -> int:
        """Project confirmed generic imports into the standardized ledger.

        These offline rows remain model-usage evidence even when the source
        contains a channel-looking field. Only realtime execution can create
        authoritative channel evidence for the dashboard and health pipeline.
        """
        now = _utc_now()
        inserted = 0
        with self.connect() as db:
            for index, row in enumerate(rows, start=1):
                if row.get("_classification") == "rejected":
                    continue
                requested_model = str(row.get("requested_model") or "").strip()
                occurred_at = str(row.get("timestamp") or "").strip()
                if not requested_model or not occurred_at:
                    continue
                try:
                    parsed_time = datetime.fromisoformat(
                        occurred_at.replace("Z", "+00:00"))
                    if parsed_time.tzinfo is None:
                        raise ValueError("timezone required")
                    occurred_at = parsed_time.astimezone(timezone.utc).isoformat()
                except ValueError:
                    continue
                http_status = int(row["http_status"]) if str(
                    row.get("http_status") or "").isdigit() else None
                explicit_status = str(row.get("request_status") or "").upper()
                if explicit_status in {"SUCCESS", "FAILED", "TIMEOUT", "INTERRUPTED", "CANCELLED"}:
                    request_status = explicit_status
                elif http_status is not None:
                    request_status = "SUCCESS" if 200 <= http_status < 400 else "FAILED"
                else:
                    request_status = "UNKNOWN"
                stream = str(row.get("stream") or "").strip().lower() in {
                    "1", "true", "yes", "stream", "streaming",
                }
                record_id = "IMPORT-" + hashlib.sha256(
                    f"{import_batch_id}:{index}".encode("utf-8")).hexdigest()[:24].upper()
                values = (
                    record_id, occurred_at, row.get("request_id") or None,
                    row.get("response_id") or None, row.get("decision_id") or None,
                    environment_id, requested_model, row.get("actual_model") or requested_model,
                    row.get("provider") or None, row.get("channel_id") or None,
                    row.get("channel_name") or None, row.get("endpoint_type") or None,
                    int(stream), request_status, http_status,
                    row.get("error_code") or None, row.get("error_category") or None,
                    None, 1, 1,
                    float(row["latency_ms"]) if row.get("latency_ms") not in (None, "") else None,
                    float(row["ttft_ms"]) if row.get("ttft_ms") not in (None, "") else None,
                    int(row["input_tokens"]) if row.get("input_tokens") not in (None, "") else None,
                    int(row.get("cached_input_tokens") or 0),
                    int(row["output_tokens"]) if row.get("output_tokens") not in (None, "") else None,
                    str(row["cost_cny"]) if row.get("cost_cny") not in (None, "") else None,
                    "CNY" if row.get("cost_cny") not in (None, "") else None,
                    None, None, source_type, "model_usage_only", 1,
                    None, None, None, None, import_batch_id, now, now,
                    *self.scope.sql_parameters(),
                )
                cursor = db.execute("""INSERT OR IGNORE INTO standardized_call_logs(
                  record_id,occurred_at,request_id,response_id,decision_id,environment_id,
                  requested_model,actual_model,provider,channel_id,channel_name,endpoint_type,
                  stream,request_status,http_status,error_code,error_category,retryable,
                  attempt_number,total_attempts,total_latency_ms,first_token_latency_ms,
                  input_tokens,cached_input_tokens,output_tokens,cost_amount,currency,
                  configuration_version,metric_snapshot_id,source_type,evidence_scope,is_historical,
                  api_key_alias,source_ip,source_ip_digest,raw_detail,import_batch_id,
                  created_at,updated_at,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  values)
                if cursor.rowcount:
                    evidence_level = ("exact_decision" if row.get("request_id") and
                                      row.get("decision_id") else
                                      "historical_statistics" if source_type == "historical_uat_csv"
                                      else "execution_only")
                    db.execute("""UPDATE standardized_call_logs SET
                      source_record_id=?,billed_model=?,pricing_detail=?,price_source=?,
                      price_version=?,duration_type=?,evidence_level=?
                      WHERE record_id=? AND tenant_id=? AND workspace_id=?""", (
                        str(row.get("source_record_id") or record_id),
                        row.get("billed_model") or requested_model,
                        row.get("pricing_detail") or row.get("raw_detail") or None,
                        row.get("price_source") or (
                            "historical_log_detail" if source_type == "historical_uat_csv" else None),
                        row.get("price_version") or (
                            "unversioned_historical_snapshot" if source_type == "historical_uat_csv" else None),
                        row.get("duration_type") or (
                            "total_and_first_token" if stream and row.get("ttft_ms") not in (None, "")
                            else "total_duration"),
                        evidence_level, record_id, *self.scope.sql_parameters(),
                    ))
                    self._record_change(db, record_id, now)
                    inserted += 1
        return inserted

    def start_execution(self, *, request_id: str, decision_id: str | None,
                        environment_id: str, requested_model: str, stream: bool,
                        channel_id: str | None = None, channel_name: str | None = None,
                        provider: str | None = None, endpoint_type: str = "openai_compatible",
                        configuration_version: str | None = None,
                        metric_snapshot_id: str | None = None,
                        channel_source: str | None = None,
                        acceptance_run_id: str | None = None,
                        error_source: str | None = None,
                        is_fault_injected: bool | None = None,
                        fault_id: str | None = None,
                        fallback_used: bool | None = None,
                        output_started: bool | None = None,
                        traffic_proposal_id: str | None = None,
                        strategy_variant: str | None = None,
                        cost_source: str | None = None,
                        traffic_class: str = "business",
                        strategy_effect_run_id: str | None = None,
                        probe_run_id: str | None = None) -> str:
        now = _utc_now()
        record_id = f"CALL-{uuid.uuid4().hex[:24].upper()}"
        with self.connect() as db:
            db.execute("""INSERT INTO standardized_call_logs(
              record_id,occurred_at,request_id,local_request_id,decision_id,environment_id,requested_model,
              provider,channel_id,channel_name,endpoint_type,stream,request_status,
              attempt_number,total_attempts,configuration_version,metric_snapshot_id,
              source_type,evidence_scope,is_historical,
              acceptance_run_id,error_source,is_fault_injected,fault_id,fallback_used,
              output_started,traffic_proposal_id,strategy_variant,cost_source,
              cost_status,cost_type,
              traffic_class,strategy_effect_run_id,probe_run_id,
              created_at,updated_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                record_id, now, request_id, request_id, decision_id, environment_id, requested_model,
                provider, channel_id, channel_name, endpoint_type, int(stream), "RUNNING",
                1, 1, configuration_version, metric_snapshot_id,
                "realtime_execution", "channel_eligible" if channel_id else "model_usage_only",
                0, acceptance_run_id, error_source,
                None if is_fault_injected is None else int(is_fault_injected), fault_id,
                None if fallback_used is None else int(fallback_used),
                None if output_started is None else int(output_started),
                traffic_proposal_id, strategy_variant, cost_source,
                "pending_provider_sync", "pending_provider_sync",
                traffic_class, strategy_effect_run_id, probe_run_id,
                now, now, *self.scope.sql_parameters(),
            ))
            db.execute("""UPDATE standardized_call_logs SET source_record_id=?,
              billed_model=?,duration_type=?,evidence_level=?,channel_source=? WHERE record_id=?
              AND tenant_id=? AND workspace_id=?""", (
                record_id, requested_model,
                "total_and_first_token" if stream else "total_duration",
                "exact_decision" if decision_id else "execution_only",
                (channel_source or "scheduler_decision") if channel_id else "unknown",
                record_id, *self.scope.sql_parameters(),
            ))
            attempt_id = f"ATT-{uuid.uuid4().hex[:24].upper()}"
            db.execute("""INSERT INTO call_attempt_logs(
              attempt_id,request_id,local_request_id,attempt_number,requested_model,channel_id,channel_name,
              status,started_at,acceptance_run_id,error_source,is_fault_injected,fault_id,
              fallback_used,output_started,traffic_proposal_id,strategy_variant,cost_source,
              cost_status,cost_type,
              strategy_effect_run_id,probe_run_id,traffic_class,
              tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                attempt_id, request_id, request_id, 1, requested_model, channel_id, channel_name,
                "RUNNING", now, acceptance_run_id, error_source,
                None if is_fault_injected is None else int(is_fault_injected), fault_id,
                None if fallback_used is None else int(fallback_used),
                None if output_started is None else int(output_started), traffic_proposal_id,
                strategy_variant, cost_source,
                "pending_provider_sync", "pending_provider_sync",
                strategy_effect_run_id, probe_run_id, traffic_class,
                *self.scope.sql_parameters(),
            ))
            self._record_change(db, record_id, now)
        return record_id

    def finish_execution(self, record_id: str, *, status: str,
                         response_id: str | None = None, actual_model: str | None = None,
                         provider_request_id: str | None = None,
                         provider_response_id: str | None = None,
                         provider_trace_id: str | None = None,
                         client_correlation_id: str | None = None,
                         provider_identifiers: dict[str, str] | None = None,
                         http_status: int | None = None, error_code: str | None = None,
                         error_category: str | None = None, retryable: bool | None = None,
                         total_latency_ms: float | None = None,
                         first_token_latency_ms: float | None = None,
                         input_tokens: int | None = None, cached_input_tokens: int | None = None,
                         output_tokens: int | None = None, cost_amount: Decimal | str | None = None,
                         currency: str | None = None, channel_id: str | None = None,
                         channel_name: str | None = None, provider: str | None = None,
                         total_attempts: int = 1,
                         channel_source: str | None = None,
                         provider_log_id: str | None = None,
                         error_source: str | None = None,
                         is_fault_injected: bool | None = None,
                         fault_id: str | None = None,
                         fallback_used: bool | None = None,
                         output_started: bool | None = None,
                         traffic_proposal_id: str | None = None,
                         strategy_variant: str | None = None,
                         cost_source: str | None = None) -> None:
        terminal = status.upper()
        if terminal not in {"SUCCESS", "FAILED", "TIMEOUT", "INTERRUPTED"}:
            raise CallLogError("call_log_terminal_status_invalid")
        amount = None if cost_amount is None else format(Decimal(str(cost_amount)), "f")
        provider_identifiers_json = (json.dumps(provider_identifiers,
            ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if provider_identifiers else None)
        now = _utc_now()
        with self.connect() as db:
            row = db.execute("""SELECT request_id,request_status FROM standardized_call_logs
              WHERE record_id=? AND tenant_id=? AND workspace_id=?""",
              (record_id, *self.scope.sql_parameters())).fetchone()
            if not row:
                raise CallLogError("call_log_record_not_found")
            if row["request_status"] != "RUNNING":
                raise CallLogError("call_log_already_terminal")
            db.execute("""UPDATE standardized_call_logs SET response_id=?,
              provider_request_id=COALESCE(?,provider_request_id),
              provider_response_id=COALESCE(?,provider_response_id),
              provider_trace_id=COALESCE(?,provider_trace_id),
              client_correlation_id=COALESCE(?,client_correlation_id),actual_model=?,
              provider_identifiers_json=COALESCE(?,provider_identifiers_json),
              billed_model=COALESCE(billed_model,?,requested_model),
              request_status=?,http_status=?,error_code=?,error_category=?,retryable=?,
              total_attempts=?,total_latency_ms=?,first_token_latency_ms=?,input_tokens=?,
              cached_input_tokens=?,output_tokens=?,cost_amount=?,currency=?,
              channel_id=COALESCE(?,channel_id),channel_name=COALESCE(?,channel_name),
              provider=COALESCE(?,provider),
              channel_source=CASE WHEN ? IS NOT NULL THEN ? ELSE channel_source END,
              provider_log_id=COALESCE(?,provider_log_id),
              error_source=COALESCE(?,error_source),
              is_fault_injected=COALESCE(?,is_fault_injected),
              fault_id=COALESCE(?,fault_id),fallback_used=COALESCE(?,fallback_used),
              output_started=COALESCE(?,output_started),
              traffic_proposal_id=COALESCE(?,traffic_proposal_id),
              strategy_variant=COALESCE(?,strategy_variant),cost_source=COALESCE(?,cost_source),
              evidence_scope=CASE WHEN COALESCE(?,channel_id) IS NULL THEN evidence_scope
                ELSE 'channel_eligible' END,updated_at=?
              WHERE record_id=? AND tenant_id=? AND workspace_id=?""", (
                response_id, provider_request_id, provider_response_id,
                provider_trace_id, client_correlation_id,
                actual_model, provider_identifiers_json, actual_model, terminal, http_status, error_code, error_category,
                None if retryable is None else int(retryable), total_attempts, total_latency_ms,
                first_token_latency_ms, input_tokens, cached_input_tokens, output_tokens,
                amount, currency, channel_id, channel_name, provider,
                channel_id, channel_source or ("provider_log" if channel_id else None),
                provider_log_id, error_source,
                None if is_fault_injected is None else int(is_fault_injected), fault_id,
                None if fallback_used is None else int(fallback_used),
                None if output_started is None else int(output_started), traffic_proposal_id,
                strategy_variant, cost_source, channel_id,
                now, record_id, *self.scope.sql_parameters(),
            ))
            db.execute("""UPDATE call_attempt_logs SET
              provider_request_id=COALESCE(?,provider_request_id),
              provider_response_id=COALESCE(?,provider_response_id),
              provider_trace_id=COALESCE(?,provider_trace_id),
              client_correlation_id=COALESCE(?,client_correlation_id),
              provider_log_id=COALESCE(?,provider_log_id),actual_model=?,status=?,http_status=?,
              error_code=?,error_category=?,retryable=?,total_latency_ms=?,first_token_latency_ms=?,
              input_tokens=?,cached_input_tokens=?,output_tokens=?,cost_amount=?,currency=?,completed_at=?
              ,channel_id=COALESCE(?,channel_id),channel_name=COALESCE(?,channel_name)
              ,error_source=COALESCE(?,error_source)
              ,is_fault_injected=COALESCE(?,is_fault_injected),fault_id=COALESCE(?,fault_id)
              ,fallback_used=COALESCE(?,fallback_used),output_started=COALESCE(?,output_started)
              ,traffic_proposal_id=COALESCE(?,traffic_proposal_id)
              ,strategy_variant=COALESCE(?,strategy_variant),cost_source=COALESCE(?,cost_source)
              WHERE request_id=? AND attempt_number=1 AND tenant_id=? AND workspace_id=?""", (
                provider_request_id, provider_response_id, provider_trace_id,
                client_correlation_id, provider_log_id,
                actual_model, terminal, http_status, error_code, error_category,
                None if retryable is None else int(retryable), total_latency_ms,
                first_token_latency_ms, input_tokens, cached_input_tokens, output_tokens,
                amount, currency, now, channel_id, channel_name, error_source,
                None if is_fault_injected is None else int(is_fault_injected), fault_id,
                None if fallback_used is None else int(fallback_used),
                None if output_started is None else int(output_started), traffic_proposal_id,
                strategy_variant, cost_source,
                row["request_id"], *self.scope.sql_parameters(),
            ))
            self._record_change(db, record_id, now)
        self.reconcile_duplicates()

    def record_attempt(self, *, request_id: str, attempt_number: int,
                       requested_model: str, status: str,
                       actual_model: str | None = None,
                       channel_id: str | None = None, channel_name: str | None = None,
                       http_status: int | None = None, error_code: str | None = None,
                       error_category: str | None = None, retryable: bool | None = None,
                       total_latency_ms: float | None = None,
                       first_token_latency_ms: float | None = None,
                       input_tokens: int | None = None,
                       cached_input_tokens: int | None = None,
                       output_tokens: int | None = None,
                       cost_amount: Decimal | str | None = None,
                       currency: str | None = None,
                       started_at: str | None = None,
                       completed_at: str | None = None,
                       acceptance_run_id: str | None = None,
                       error_source: str | None = None,
                       is_fault_injected: bool | None = None,
                       fault_id: str | None = None,
                       fallback_used: bool | None = None,
                       output_started: bool | None = None,
                       traffic_proposal_id: str | None = None,
                       strategy_variant: str | None = None,
                       cost_source: str | None = None) -> str:
        if attempt_number < 1:
            raise CallLogError("attempt_number_invalid")
        terminal = status.upper()
        if terminal not in {"RUNNING", "SUCCESS", "FAILED", "TIMEOUT", "INTERRUPTED"}:
            raise CallLogError("attempt_status_invalid")
        attempt_id = f"ATT-{uuid.uuid4().hex[:24].upper()}"
        amount = None if cost_amount is None else format(Decimal(str(cost_amount)), "f")
        with self.connect() as db:
            db.execute("""INSERT INTO call_attempt_logs(
              attempt_id,request_id,attempt_number,requested_model,actual_model,
              channel_id,channel_name,status,http_status,error_code,error_category,
              retryable,total_latency_ms,first_token_latency_ms,input_tokens,
              cached_input_tokens,output_tokens,cost_amount,currency,started_at,
              completed_at,acceptance_run_id,error_source,is_fault_injected,fault_id,
              fallback_used,output_started,traffic_proposal_id,strategy_variant,cost_source,
              tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                attempt_id, request_id, attempt_number, requested_model, actual_model,
                channel_id, channel_name, terminal, http_status, error_code, error_category,
                None if retryable is None else int(retryable), total_latency_ms,
                first_token_latency_ms, input_tokens, cached_input_tokens, output_tokens,
                amount, currency, started_at or _utc_now(), completed_at,
                acceptance_run_id, error_source,
                None if is_fault_injected is None else int(is_fault_injected), fault_id,
                None if fallback_used is None else int(fallback_used),
                None if output_started is None else int(output_started), traffic_proposal_id,
                strategy_variant, cost_source,
                *self.scope.sql_parameters(),
            ))
            db.execute("""UPDATE standardized_call_logs SET total_attempts=MAX(total_attempts,?),
              updated_at=? WHERE request_id=? AND tenant_id=? AND workspace_id=?""",
              (attempt_number, _utc_now(), request_id, *self.scope.sql_parameters()))
            parent = db.execute("""SELECT record_id FROM standardized_call_logs
              WHERE request_id=? AND tenant_id=? AND workspace_id=?""",
              (request_id, *self.scope.sql_parameters())).fetchone()
            if parent:
                self._record_change(db, str(parent["record_id"]), _utc_now())
        return attempt_id

    def attempts(self, request_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("""SELECT attempt_id,request_id,local_request_id,
              provider_request_id,provider_response_id,provider_trace_id,
              client_correlation_id,provider_log_id,attempt_number,
              requested_model,actual_model,channel_id,channel_name,status,http_status,
              error_code,error_category,retryable,total_latency_ms,first_token_latency_ms,
              input_tokens,cached_input_tokens,output_tokens,cost_amount,currency,
              started_at,completed_at,acceptance_run_id,error_source,is_fault_injected,
              fault_id,fallback_used,output_started,traffic_proposal_id,strategy_variant,
              cost_source FROM call_attempt_logs WHERE request_id=?
              AND tenant_id=? AND workspace_id=? ORDER BY attempt_number""",
              (request_id, *self.scope.sql_parameters())).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            for field in ("is_fault_injected", "fallback_used", "output_started"):
                row[field] = None if row[field] is None else bool(row[field])
        return result

    def save_routing_decision(self, *, result: dict[str, Any],
                              routing_decision: dict[str, Any] | None = None) -> None:
        decision_id = str(result.get("decision_id") or "").strip()
        request_id = str(result.get("request_id") or "").strip()
        if not decision_id or not request_id:
            return
        automatic = isinstance(routing_decision, dict)
        selected_model = (routing_decision or {}).get("selected_model") or result.get("actual_model") or result.get("requested_model")
        selected_channel = result.get("channel_id")
        evidence = {
            "decision_id": decision_id, "request_id": request_id,
            "local_request_id": request_id,
            "response_id": result.get("response_id"),
            "provider_request_id": result.get("provider_request_id"),
            "provider_response_id": result.get("provider_response_id"),
            "provider_trace_id": result.get("provider_trace_id"),
            "client_correlation_id": result.get("client_correlation_id"),
            "decision_type": "automatic_routing" if automatic else "specified_model",
            "environment_id": "china_uat", "method": result.get("method"),
            "path": result.get("path"), "stream": result.get("first_token_latency_ms") is not None,
            "policy_version": (routing_decision or {}).get("policy") or "user_specified_model",
            "configuration_version": None, "metric_snapshot_id": None,
            "candidates": (routing_decision or {}).get("candidates") or [{
                "model_id": selected_model, "channel_id": selected_channel,
                "score": None, "capability_status": "not_evaluated",
                "reason": "user_specified_model"}],
            "exclusions": (routing_decision or {}).get("excluded") or [],
            "selected_model": selected_model, "selected_channel": selected_channel,
            "selection_reason": (routing_decision or {}).get("selection_reason") or "user_specified_model",
            "confidence": (routing_decision or {}).get("confidence") or "not_scored",
            "catalog_candidate_count": (routing_decision or {}).get("catalog_candidate_count") or 1,
        }
        now = _utc_now()
        with self.connect() as db:
            db.execute("""INSERT INTO routing_decision_logs(
              decision_id,request_id,decision_type,evidence_json,created_at,updated_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(tenant_id,workspace_id,decision_id)
              DO UPDATE SET request_id=excluded.request_id,decision_type=excluded.decision_type,
              evidence_json=excluded.evidence_json,updated_at=excluded.updated_at""",
              (decision_id,request_id,evidence["decision_type"],_canonical(evidence),now,now,
               *self.scope.sql_parameters()))

    def routing_decision(self, decision_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            stored = db.execute("""SELECT evidence_json FROM routing_decision_logs
              WHERE decision_id=? AND tenant_id=? AND workspace_id=?""",
              (decision_id,*self.scope.sql_parameters())).fetchone()
            log = db.execute("""SELECT occurred_at,request_id,local_request_id,
              provider_request_id,provider_response_id,provider_trace_id,
              client_correlation_id,response_id,decision_id,environment_id,
              requested_model,actual_model,provider,channel_id,channel_name,endpoint_type,stream,
              request_status,http_status,error_code,error_category,total_attempts,total_latency_ms,
              first_token_latency_ms,input_tokens,cached_input_tokens,output_tokens,cost_amount,currency,
              configuration_version,metric_snapshot_id,created_at,updated_at
              FROM standardized_call_logs WHERE decision_id=? AND tenant_id=? AND workspace_id=?
              ORDER BY cursor_id DESC LIMIT 1""",(decision_id,*self.scope.sql_parameters())).fetchone()
        if not log:
            return None
        row=dict(log)
        evidence=json.loads(stored["evidence_json"]) if stored else {
            "decision_id":decision_id,"request_id":row["request_id"],"response_id":row["response_id"],
            "decision_type":"specified_model","environment_id":row["environment_id"],
            "method":str(row.get("endpoint_type") or "").removeprefix("uat_http_").upper() or None,
            "path":None,"stream":bool(row["stream"]),"policy_version":"user_specified_model",
            "configuration_version":row["configuration_version"],"metric_snapshot_id":row["metric_snapshot_id"],
            "candidates":[{"model_id":row["requested_model"],"channel_id":row["channel_id"],
              "score":None,"capability_status":"not_recorded","reason":"user_specified_model"}],
            "exclusions":[],"selected_model":row["actual_model"] or row["requested_model"],
            "selected_channel":row["channel_id"],"selection_reason":"user_specified_model",
            "confidence":"not_scored","catalog_candidate_count":1,
        }
        evidence["execution_result"]={
            "status":row["request_status"],"http_status":row["http_status"],
            "request_id":row["request_id"],"response_id":row["response_id"],
            "local_request_id":row["local_request_id"] or row["request_id"],
            "provider_request_id":row["provider_request_id"],
            "provider_response_id":row["provider_response_id"],
            "provider_trace_id":row["provider_trace_id"],
            "client_correlation_id":row["client_correlation_id"],
            "model":row["actual_model"] or row["requested_model"],"provider":row["provider"],
            "channel_id":row["channel_id"],"channel_name":row["channel_name"],
            "total_latency_ms":row["total_latency_ms"],"first_token_latency_ms":row["first_token_latency_ms"],
            "input_tokens":row["input_tokens"],"cached_input_tokens":row["cached_input_tokens"],
            "output_tokens":row["output_tokens"],"cost_amount":row["cost_amount"],"currency":row["currency"],
            "total_attempts":row["total_attempts"],"fallback":row["total_attempts"]>1,
            "error_code":row["error_code"],"error_category":row["error_category"],
        }
        evidence["created_at"]=row["occurred_at"]
        evidence["retry_history"]=self.attempts(str(row["request_id"]))
        return evidence

    def list_routing_decisions(self, *, request_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        clauses=["decision_id IS NOT NULL","tenant_id=?","workspace_id=?"]
        params:list[Any]=[*self.scope.sql_parameters()]
        if request_id:
            clauses.append("request_id=?");params.append(request_id)
        with self.connect() as db:
            rows=db.execute(f"""SELECT decision_id FROM standardized_call_logs
              WHERE {' AND '.join(clauses)} ORDER BY cursor_id DESC LIMIT ?""",(*params,min(max(limit,1),500))).fetchall()
        items=[item for row in rows if (item:=self.routing_decision(str(row["decision_id"]))) is not None]
        return {"status":"ready","items":items,"total":len(items)}

    def expire_stale_running(self, timeout_seconds: int = 300) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
        now = _utc_now()
        with self.connect() as db:
            rows = db.execute("""SELECT record_id,request_id FROM standardized_call_logs
              WHERE request_status='RUNNING' AND occurred_at<? AND tenant_id=? AND workspace_id=?""",
              (cutoff, *self.scope.sql_parameters())).fetchall()
            for row in rows:
                db.execute("""UPDATE standardized_call_logs SET request_status='TIMEOUT',
                  error_code='execution_completion_timeout',error_category='timeout',
                  retryable=0,updated_at=? WHERE record_id=? AND tenant_id=? AND workspace_id=?""",
                  (now, row["record_id"], *self.scope.sql_parameters()))
                db.execute("""UPDATE call_attempt_logs SET status='TIMEOUT',
                  error_code='execution_completion_timeout',error_category='timeout',
                  retryable=0,completed_at=? WHERE request_id=? AND status='RUNNING'
                  AND tenant_id=? AND workspace_id=?""",
                  (now, row["request_id"], *self.scope.sql_parameters()))
                self._record_change(db, str(row["record_id"]), now)
        return len(rows)

    def list_records(self, *, after_cursor: int = 0, limit: int = 100,
                     environment_id: str | None = None, source_type: str | None = None) -> dict[str, Any]:
        clauses = ["c.tenant_id=?", "c.workspace_id=?", "c.change_id>?"]
        params: list[Any] = [*self.scope.sql_parameters(), max(0, after_cursor)]
        if environment_id:
            clauses.append("l.environment_id=?"); params.append(environment_id)
        if source_type:
            clauses.append("l.source_type=?"); params.append(source_type)
        with self.connect() as db:
            rows = db.execute(f"""SELECT MAX(c.change_id) AS cursor_id,l.record_id,l.occurred_at,
              l.request_id,l.local_request_id,l.provider_request_id,
              l.provider_response_id,l.provider_trace_id,l.client_correlation_id,
              l.response_id,l.decision_id,l.environment_id,l.requested_model,
              l.actual_model,l.provider,l.channel_id,l.channel_name,l.endpoint_type,l.stream,
              l.request_status,l.http_status,l.error_code,l.error_category,l.retryable,
              l.attempt_number,l.total_attempts,l.total_latency_ms,l.first_token_latency_ms,
              l.input_tokens,l.cached_input_tokens,l.output_tokens,l.cost_amount,l.currency,
              l.configuration_version,l.metric_snapshot_id,l.source_type,l.evidence_scope,
              l.is_historical,l.source_record_id,l.billed_model,l.pricing_detail,
              l.price_source,l.price_version,l.duration_type,l.evidence_level,
              l.provider_log_id,l.dedup_fingerprint,l.duplicate_of,
              l.duplicate_status,l.duplicate_reason,l.channel_source,
              l.acceptance_run_id,l.error_source,l.is_fault_injected,l.fault_id,
              l.fallback_used,l.output_started,l.traffic_proposal_id,
              l.strategy_variant,l.cost_source,l.traffic_class,l.cost_status,
              l.cost_type,l.provider_cost_amount_exact,l.provider_raw_quota,
              l.provider_quota_unit,l.provider_conversion_rate,
              l.provider_cost_precision,l.provider_rounding_mode,
              l.provider_cost_source_reference,l.provider_sync_batch_id,
              l.provider_cost_components_json,l.provider_cost_updated_at,
              l.provider_sync_failure_reason,l.match_method,l.matched_field,
              l.matched_value_hash,l.match_confidence,l.provider_log_ids,l.matched_at,
              l.created_at,l.updated_at
              FROM call_log_changes c JOIN standardized_call_logs l
                ON l.record_id=c.record_id AND l.tenant_id=c.tenant_id
                AND l.workspace_id=c.workspace_id
              WHERE {' AND '.join(clauses)} GROUP BY l.record_id
              ORDER BY cursor_id DESC LIMIT ?""", (*params, min(max(limit, 1), 1000))).fetchall()
            total = db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=?""", self.scope.sql_parameters()).fetchone()[0]
            effective = db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND duplicate_status<>'exact_duplicate'""",
              self.scope.sql_parameters()).fetchone()[0]
            exact_duplicates = total - effective
            duplicate_candidates = db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND duplicate_status='duplicate_candidate'""",
              self.scope.sql_parameters()).fetchone()[0]
            historical = db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND is_historical=1""",
              self.scope.sql_parameters()).fetchone()[0]
        items = [dict(row) for row in rows]
        for item in items:
            item["stream"] = bool(item["stream"])
            item["is_historical"] = bool(item["is_historical"])
            for field in ("is_fault_injected", "fallback_used", "output_started"):
                item[field] = None if item[field] is None else bool(item[field])
        return {
            "status": "ready", "schema_version": self.schema_version,
            "items": items, "total": total, "effective_count": effective,
            "exact_duplicate_count": exact_duplicates,
            "duplicate_candidate_count": duplicate_candidates,
            "historical_count": historical,
            "next_cursor": max((int(item["cursor_id"]) for item in items), default=after_cursor),
            "updated_at": max((str(item["updated_at"]) for item in items), default=None),
        }

    def cost_records(self, *, environment_id: str = "china_uat") -> list[dict[str, Any]]:
        """Return the complete deduplicated call-log cost projection for UI analytics.

        This deliberately projects only normalized, non-secret fields.  It is separate
        from cursor pagination because totals and rankings must cover the full ledger.
        """
        with self.connect() as db:
            rows = db.execute("""SELECT record_id,occurred_at,request_id,requested_model,
              actual_model,channel_id,channel_name,request_status,http_status,
              total_latency_ms,input_tokens,cached_input_tokens,output_tokens,
              cost_amount,currency,source_type,price_version,cost_source,
              traffic_class,cost_status,cost_type,provider_cost_amount_exact,
              provider_sync_batch_id
              FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
                AND request_status<>'RUNNING' AND duplicate_status<>'exact_duplicate'
              ORDER BY occurred_at DESC,record_id DESC""",
              (*self.scope.sql_parameters(), environment_id)).fetchall()
        return [dict(row) for row in rows]

    def acceptance_evidence_counts(self, acceptance_run_id: str) -> dict[str, int]:
        """Count canonical run evidence by provenance without merging categories."""
        with self.connect() as db:
            row = db.execute("""SELECT
              SUM(CASE WHEN is_historical=1 THEN 1 ELSE 0 END) AS historical,
              SUM(CASE WHEN source_type='realtime_execution' THEN 1 ELSE 0 END) AS realtime,
              SUM(CASE WHEN COALESCE(is_fault_injected,0)=1 THEN 1 ELSE 0 END) AS injected,
              SUM(CASE WHEN source_type='realtime_execution' AND provider_log_id IS NOT NULL
                AND COALESCE(is_fault_injected,0)=0 THEN 1 ELSE 0 END) AS provider_live,
              COUNT(*) AS total
              FROM standardized_call_logs WHERE tenant_id=? AND workspace_id=?
              AND acceptance_run_id=? AND duplicate_status<>'exact_duplicate'""",
              (*self.scope.sql_parameters(), acceptance_run_id)).fetchone()
        return {name: int(row[name] or 0) for name in
                ("historical", "realtime", "injected", "provider_live", "total")}

    def analytics(self, *, environment_id: str = "china_uat",
                  model: str | None = None, source_type: str | None = None,
                  channel_id: str | None = None, provider: str | None = None,
                  request_status: str | None = None, stream: bool | None = None,
                  occurred_from: str | None = None, occurred_to: str | None = None,
                  traffic_class: str = "business") -> dict[str, Any]:
        clauses = ["tenant_id=?", "workspace_id=?", "environment_id=?",
                   "request_status<>'RUNNING'", "duplicate_status<>'exact_duplicate'"]
        params: list[Any] = [*self.scope.sql_parameters(), environment_id]
        if traffic_class == "business":
            clauses.append("COALESCE(traffic_class,'business')<>'probe'")
        elif traffic_class == "probe":
            clauses.append("traffic_class='probe'")
        elif traffic_class != "all":
            raise ValueError("traffic_class_invalid")
        for column, value in (("requested_model", model), ("source_type", source_type),
                              ("channel_id", channel_id), ("provider", provider),
                              ("request_status", request_status)):
            if value:
                clauses.append(f"{column}=?"); params.append(value)
        if stream is not None:
            clauses.append("stream=?"); params.append(int(stream))
        if occurred_from:
            clauses.append("occurred_at>=?"); params.append(occurred_from)
        if occurred_to:
            clauses.append("occurred_at<=?"); params.append(occurred_to)
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM standardized_call_logs WHERE {' AND '.join(clauses)}", params
            ).fetchall()]
            raw_count = int(db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=? AND request_status<>'RUNNING'""",
              (*self.scope.sql_parameters(), environment_id)).fetchone()[0])
            exact_duplicate_count = int(db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              AND request_status<>'RUNNING' AND duplicate_status='exact_duplicate'""",
              (*self.scope.sql_parameters(), environment_id)).fetchone()[0])
            duplicate_candidate_count = int(db.execute("""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              AND request_status<>'RUNNING' AND duplicate_status='duplicate_candidate'""",
              (*self.scope.sql_parameters(), environment_id)).fetchone()[0])
        latencies = [float(row["total_latency_ms"]) for row in rows if row["total_latency_ms"] is not None]
        ttfts = [float(row["first_token_latency_ms"]) for row in rows if row["first_token_latency_ms"] is not None]
        success = sum(row["request_status"] == "SUCCESS" for row in rows)
        failure_states = {"FAILED", "TIMEOUT", "INTERRUPTED", "CANCELLED"}
        failures = sum(row["request_status"] in failure_states for row in rows)
        costs = [Decimal(str(row["cost_amount"])) for row in rows if row["cost_amount"] is not None]
        model_counts: dict[str, int] = {}
        model_costs: dict[str, Decimal] = {}
        trend: dict[str, dict[str, Any]] = {}
        mismatch = 0
        for row in rows:
            model_key = str(row["actual_model"] or row["requested_model"])
            model_counts[model_key] = model_counts.get(model_key, 0) + 1
            model_costs[model_key] = model_costs.get(
                model_key, Decimal("0")) + Decimal(str(row["cost_amount"] or "0"))
            mismatch += bool(row["actual_model"] and row["actual_model"] != row["requested_model"])
            day = datetime.fromisoformat(str(row["occurred_at"])).astimezone(CHINA_ZONE).date().isoformat()
            bucket = trend.setdefault(day, {"date": day, "request_count": 0, "success_count": 0,
                                             "total_latency_ms": 0.0, "total_tokens": 0,
                                             "total_cost": Decimal("0"), "cost_count": 0})
            bucket["request_count"] += 1
            bucket["success_count"] += row["request_status"] == "SUCCESS"
            bucket["total_latency_ms"] += float(row["total_latency_ms"] or 0)
            # cached_input_tokens is a subset of input_tokens in the UAT/OpenAI
            # usage contract, not an additional token bucket.
            bucket["total_tokens"] += int(row["input_tokens"] or 0) + int(row["output_tokens"] or 0)
            if row["cost_amount"] is not None:
                bucket["total_cost"] += Decimal(str(row["cost_amount"]))
                bucket["cost_count"] += 1
        trend_items = []
        for day in sorted(trend):
            bucket = trend[day]
            count = bucket["request_count"]
            trend_items.append({
                "date": day, "request_count": count,
                "success_rate": round(bucket["success_count"] / count, 6) if count else None,
                "average_latency_ms": round(bucket["total_latency_ms"] / count, 3) if count else None,
                "total_tokens": bucket["total_tokens"],
                "total_cost": format(bucket["total_cost"], "f") if bucket["cost_count"] else None,
                "currency": "CNY" if bucket["cost_count"] else None,
            })
        today = datetime.now(CHINA_ZONE).date().isoformat()
        today_rows = [row for row in rows if datetime.fromisoformat(
            str(row["occurred_at"])).astimezone(CHINA_ZONE).date().isoformat() == today]
        today_cost = sum((Decimal(str(row["cost_amount"] or "0")) for row in today_rows), Decimal("0"))
        return {
            "status": "ready", "schema_version": self.schema_version,
            "environment_id": environment_id, "traffic_class": traffic_class,
            "request_count": len(rows),
            "raw_record_count": raw_count,
            "exact_duplicate_count": exact_duplicate_count,
            "duplicate_candidate_count": duplicate_candidate_count,
            "success_count": success, "failure_count": failures,
            "success_rate": round(success / len(rows), 6) if rows else None,
            "total_latency_ms": round(sum(latencies), 3),
            "average_latency_ms": round(statistics.fmean(latencies), 3) if latencies else None,
            "p50_latency_ms": _percentile(latencies, .50),
            "p95_latency_ms": _percentile(latencies, .95),
            "p99_latency_ms": _percentile(latencies, .99),
            "average_first_token_latency_ms": round(statistics.fmean(ttfts), 3) if ttfts else None,
            "input_tokens": sum(int(row["input_tokens"] or 0) for row in rows),
            "cached_input_tokens": sum(int(row["cached_input_tokens"] or 0) for row in rows),
            "output_tokens": sum(int(row["output_tokens"] or 0) for row in rows),
            "total_cost": format(sum(costs, Decimal("0")), "f") if costs else None,
            "currency": "CNY" if costs else None,
            "average_cost": format(sum(costs, Decimal("0")) / len(costs), "f") if costs else None,
            "stream_count": sum(bool(row["stream"]) for row in rows),
            "nonstream_count": sum(not bool(row["stream"]) for row in rows),
            "model_mismatch_count": mismatch,
            "model_counts": dict(sorted(model_counts.items(), key=lambda item: (-item[1], item[0]))),
            "model_costs": {key: format(value, "f") for key, value in sorted(
                model_costs.items(), key=lambda item: (-item[1], item[0]))},
            "error_categories": dict(sorted({
                category: sum(row["error_category"] == category for row in rows)
                for category in {row["error_category"] for row in rows if row["error_category"]}
            }.items())),
            "retry_count": sum(max(0, int(row["total_attempts"] or 1) - 1) for row in rows),
            "fallback_count": sum(int(row["total_attempts"] or 1) > 1 for row in rows),
            "today_request_count": len(today_rows),
            "today_tokens": sum(int(row["input_tokens"] or 0)+int(row["output_tokens"] or 0)
                for row in today_rows),
            "today_cost": format(today_cost, "f") if any(
                row["cost_amount"] is not None for row in today_rows) else None,
            "measurement_coverage": {
                "latency": sum(row["total_latency_ms"] is not None for row in rows),
                "tokens": sum(any(row[name] is not None for name in
                    ("input_tokens", "cached_input_tokens", "output_tokens")) for row in rows),
                "cost": len(costs),
            },
            "trend": trend_items,
            "generated_at": _utc_now(),
        }

    def overview_snapshot(self, environment_id: str = "china_uat",
                          recent_limit: int = 10,
                          occurred_from: str | None = None) -> dict[str, Any]:
        """Return a real-evidence overview without mixing demo rows.

        Business requests are represented by the standardized parent ledger;
        attempt rows are never counted as additional requests. Historical rows
        without an authoritative channel remain model-only evidence.
        """
        analytics = self.analytics(
            environment_id=environment_id, occurred_from=occurred_from)
        with self.connect() as db:
            scope = self.scope.sql_parameters()
            time_clause = " AND occurred_at>=?" if occurred_from else ""
            time_params: tuple[Any, ...] = (occurred_from,) if occurred_from else ()
            recent = [dict(row) for row in db.execute(f"""SELECT record_id,
              occurred_at,request_id,response_id,decision_id,requested_model,
              actual_model,provider,channel_id,channel_name,stream,request_status,
              http_status,error_category,total_attempts,total_latency_ms,
              first_token_latency_ms,input_tokens,cached_input_tokens,output_tokens,
              cost_amount,currency,source_type,evidence_scope,is_historical,
              updated_at FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              {time_clause} ORDER BY occurred_at DESC,cursor_id DESC LIMIT ?""",
              (*scope, environment_id, *time_params,
               min(max(recent_limit, 1), 50))).fetchall()]
            channel_rows = [dict(row) for row in db.execute(f"""SELECT channel_id,
              MAX(COALESCE(channel_name,channel_id)) AS channel_name,
              COUNT(*) AS request_count,
              SUM(CASE WHEN request_status='SUCCESS' THEN 1 ELSE 0 END) AS success_count,
              MAX(occurred_at) AS last_observation
              FROM standardized_call_logs WHERE tenant_id=? AND workspace_id=?
              AND environment_id=? AND source_type='realtime_execution'
              AND evidence_scope='channel_eligible' AND channel_id IS NOT NULL
              AND request_status<>'RUNNING' {time_clause} GROUP BY channel_id
              ORDER BY request_count DESC,channel_id""",
              (*scope, environment_id, *time_params)).fetchall()]
            sources = [str(row[0]) for row in db.execute("""SELECT DISTINCT source_type
              FROM standardized_call_logs WHERE tenant_id=? AND workspace_id=?
              AND environment_id=? ORDER BY source_type""",
              (*scope, environment_id)).fetchall()]
            data_as_of = db.execute("""SELECT MAX(updated_at) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?""",
              (*scope, environment_id)).fetchone()[0]
            last_synced_at = db.execute("""SELECT MAX(updated_at)
              FROM standardized_call_logs WHERE tenant_id=? AND workspace_id=?
              AND environment_id=? AND source_type='realtime_execution'""",
              (*scope, environment_id)).fetchone()[0]
            historical_failures = db.execute(f"""SELECT COUNT(*) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              AND is_historical=1 AND request_status IN
              ('FAILED','TIMEOUT','INTERRUPTED','CANCELLED') {time_clause}""",
              (*scope, environment_id, *time_params)).fetchone()[0]
        for row in recent:
            row["stream"] = bool(row["stream"])
            row["is_historical"] = bool(row["is_historical"])
        channel_total = sum(int(row["request_count"]) for row in channel_rows)
        channels = [{
            **row,
            "success_rate": round(int(row["success_count"]) /
                                  int(row["request_count"]), 6),
            "share": round(int(row["request_count"]) / channel_total, 6)
                     if channel_total else None,
        } for row in channel_rows]
        return {
            "status": "ready", "schema_version": "overview_real_evidence_v1",
            "environment_id": environment_id, "data_mode": "real",
            "source_type": sources[0] if len(sources) == 1 else
                ("mixed_real_evidence" if sources else None),
            "source_types": sources, "is_mock": False,
            "data_as_of": data_as_of, "last_synced_at": last_synced_at,
            "metrics": analytics, "channels": channels,
            "recent_executions": recent,
            "historical_failures": {
                "count": int(historical_failures),
                "classification": "historical_observation",
                "blocks_current_execution": False,
            },
            "channel_evidence_note": (
                "仅带权威 channel_id 的实时执行日志进入渠道统计；历史 CSV 只参与模型级统计。"
            ),
        }

    def channel_health_rows(self, environment_id: str = "china_uat") -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              AND source_type='realtime_execution' AND evidence_scope='channel_eligible'
              AND channel_id IS NOT NULL AND request_status<>'RUNNING'""",
              (*self.scope.sql_parameters(), environment_id)).fetchall()]

    def latest_import_report(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("""SELECT report_json FROM call_log_import_batches
              WHERE tenant_id=? AND workspace_id=? ORDER BY imported_at DESC LIMIT 1""",
              self.scope.sql_parameters()).fetchone()
        return json.loads(row[0]) if row else None
