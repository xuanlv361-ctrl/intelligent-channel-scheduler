"""Read-only adapters from immutable/local evidence into metric projections."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.incremental_metrics_service import (
    AGGREGATION_VERSION,
    IncrementalMetricsService,
    MetricsError,
    bind_enterprise_service_security,
)
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext

PROTECTED_V3_ROWS = 60
PROTECTED_V3_BYTES = 68_620
PROTECTED_V3_SHA256 = (
    "E61D6CDD596BB8963F4BE9A1C42724D31A2A54E33765CEB5EF60AC372EACB627"
)


class MetricsSourceError(ValueError):
    """Stable source integrity/configuration error without raw evidence."""


def _json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


class MetricsEvidenceAdapter:
    """Incrementally projects whitelisted fields from existing evidence stores.

    Cursor updates occur only after idempotent metric ingestion succeeds.  A
    crash between ingestion and cursor commit safely replays the same evidence.
    """

    def __init__(
        self,
        database_path: Path,
        metrics: IncrementalMetricsService,
        protected_v3_path: Path,
        source_allowlist: set[str] | None = None,
        principal: PrincipalContext | None = None,
        authorization: AuthorizationService | None = None,
        development_mode: bool = False,
    ):
        self.database_path = Path(database_path)
        self.metrics = metrics
        self.protected_v3_path = Path(protected_v3_path)
        self.security = (
            metrics.security
            if principal is None and authorization is None and not development_mode
            else bind_enterprise_service_security(
                principal=principal,
                authorization=authorization,
                development_mode=development_mode,
                development_role="metrics_service",
            )
        )
        self.scope = self.security.scope
        self.scope.assert_same(metrics.scope)
        self.source_allowlist = (
            frozenset(source_allowlist)
            if source_allowlist is not None
            else frozenset({
                "protected_v3",
                "confirmed_imports",
                "uat_executions",
                "realtime_logs",
            })
        )
        unknown = self.source_allowlist - {
            "protected_v3", "confirmed_imports",
            "uat_executions", "realtime_logs", "standardized_call_logs",
        }
        if unknown:
            raise MetricsSourceError("metric_source_allowlist_invalid")
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metric_source_cursors(
                  source_name TEXT NOT NULL,
                  cursor_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  aggregation_version TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,source_name)
                );
                CREATE TABLE IF NOT EXISTS metric_source_sync_audit(
                  sync_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  started_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL,
                  aggregation_version TEXT NOT NULL,
                  rebuild INTEGER NOT NULL,
                  accepted_count INTEGER NOT NULL,
                  rejected_count INTEGER NOT NULL,
                  source_counts_json TEXT NOT NULL,
                  rejection_counts_json TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metric_refresh_runtime(
                  singleton_id INTEGER NOT NULL CHECK(singleton_id=1),
                  last_started_at TEXT,
                  last_completed_at TEXT,
                  last_status TEXT NOT NULL,
                  last_error_code TEXT,
                  consecutive_failures INTEGER NOT NULL DEFAULT 0,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,singleton_id)
                );
                """
            )
            db.execute(
                """INSERT OR IGNORE INTO metric_refresh_runtime(
                     singleton_id,last_status,consecutive_failures,
                     tenant_id,workspace_id) VALUES(1,'never_run',0,?,?)""",
                self.scope.sql_parameters(),
            )

    def _cursor(self, source: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """SELECT cursor_json FROM metric_source_cursors
                   WHERE source_name=? AND tenant_id=? AND workspace_id=?""",
                (source, *self.scope.sql_parameters()),
            ).fetchone()
        return _json(row["cursor_json"]) if row else {}

    def _table_exists(self, table: str) -> bool:
        with self.connect() as db:
            return db.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name=?""",
                (table,),
            ).fetchone() is not None

    def _set_cursors(
        self, cursors: dict[str, dict[str, Any]], now: datetime
    ) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for source, cursor in sorted(cursors.items()):
                db.execute(
                    """INSERT INTO metric_source_cursors(
                         source_name,cursor_json,updated_at,aggregation_version,
                         tenant_id,workspace_id) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(tenant_id,workspace_id,source_name) DO UPDATE SET
                         cursor_json=excluded.cursor_json,
                         updated_at=excluded.updated_at,
                         aggregation_version=excluded.aggregation_version""",
                    (
                        source,
                        json.dumps(cursor, sort_keys=True),
                        now.isoformat(),
                        AGGREGATION_VERSION,
                        *self.scope.sql_parameters(),
                    ),
                )

    def _protected(self, rebuild: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.protected_v3_path.is_file():
            raise MetricsSourceError("protected_v3_missing")
        raw = self.protected_v3_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest().upper()
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
        if (
            len(raw) != PROTECTED_V3_BYTES
            or len(lines) != PROTECTED_V3_ROWS
            or digest != PROTECTED_V3_SHA256
        ):
            raise MetricsSourceError("protected_v3_integrity_mismatch")
        cursor = {"sha256": digest, "rows": len(lines), "bytes": len(raw)}
        if not rebuild and self._cursor("protected_v3") == cursor:
            return [], cursor
        events = []
        for line in lines:
            item = json.loads(line)
            events.append(
                {
                    "evidence_id": f"protected_v3:{item.get('execution_id')}",
                    "environment_id": "china_uat",
                    "requested_model": item.get("requested_model"),
                    "actual_model": item.get("actual_model"),
                    "actual_channel": item.get("actual_channel"),
                    "request_profile_id": item.get("request_profile_id"),
                    "observed_at": item.get("completed_at"),
                    "http_status": item.get("http_status"),
                    "latency_ms": item.get("latency_ms"),
                    "ttft_ms": item.get("ttft_ms"),
                    "stream": item.get("stream"),
                    "sse_complete": item.get("sse_complete"),
                    "error_category": item.get("error_category"),
                    "source_type": item.get("source_type"),
                }
            )
        return events, cursor

    def _imports(self, rebuild: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cursor = {} if rebuild else self._cursor("confirmed_imports")
        if not self._table_exists("import_batches"):
            return [], cursor
        after = str(cursor.get("created_at") or "")
        after_id = str(cursor.get("batch_id") or "")
        with self.connect() as db:
            rows = db.execute(
                """SELECT batch_id,created_at,payload FROM import_batches
                   WHERE tenant_id=? AND workspace_id=? AND
                     (created_at>? OR (created_at=? AND batch_id>?))
                   ORDER BY created_at,batch_id""",
                (*self.scope.sql_parameters(), after, after, after_id),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            batch = _json(row["payload"])
            source_type = batch.get("source_type") or "unknown"
            environment = batch.get("environment_id") or "china_uat"
            records = batch.get("normalized_rows") or batch.get("normalized_preview") or []
            for index, item in enumerate(records, 1):
                if not isinstance(item, dict) or item.get("_classification") == "rejected":
                    continue
                events.append(
                    {
                        **item,
                        "evidence_id": (
                            f"import:{row['batch_id']}:"
                            f"{item.get('_row_number') or index}"
                        ),
                        "environment_id": item.get("environment_id") or environment,
                        "source_type": item.get("source_type") or source_type,
                    }
                )
        next_cursor = (
            {"created_at": rows[-1]["created_at"], "batch_id": rows[-1]["batch_id"]}
            if rows
            else cursor
        )
        return events, next_cursor

    def _uat(self, rebuild: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cursor = {} if rebuild else self._cursor("uat_executions")
        if not self._table_exists("uat_executions"):
            return [], cursor
        after = str(cursor.get("created_at") or "")
        after_id = str(cursor.get("execution_id") or "")
        with self.connect() as db:
            rows = db.execute(
                """SELECT e.execution_id,e.created_at,e.environment_id,
                          e.estimated_cost_cny,e.payload AS execution_payload,
                          r.payload AS response_payload
                   FROM uat_executions e
                   LEFT JOIN uat_response_evidence r
                     ON r.execution_id=e.execution_id
                    AND r.tenant_id=e.tenant_id
                    AND r.workspace_id=e.workspace_id
                   WHERE e.tenant_id=? AND e.workspace_id=? AND
                     (e.created_at>? OR
                         (e.created_at=? AND e.execution_id>?))
                   ORDER BY e.created_at,e.execution_id""",
                (*self.scope.sql_parameters(), after, after, after_id),
            ).fetchall()
        events = []
        for row in rows:
            execution = _json(row["execution_payload"])
            response = _json(row["response_payload"])
            events.append(
                {
                    "evidence_id": f"uat:{row['execution_id']}",
                    "environment_id": row["environment_id"],
                    "requested_model": execution.get("requested_model"),
                    "actual_model": response.get("actual_model"),
                    "actual_channel": execution.get("platform_actual_channel"),
                    "request_profile_id": execution.get("prompt_profile_id"),
                    "observed_at": (
                        execution.get("request_completed_at") or row["created_at"]
                    ),
                    "http_status": response.get("http_status"),
                    "latency_ms": (
                        response.get("elapsed_ms") or execution.get("elapsed_ms")
                    ),
                    "stream": execution.get("stream"),
                    "sse_complete": (
                        response.get("response_completeness") == "complete"
                        if execution.get("stream") is True
                        else None
                    ),
                    "estimated_cost": row["estimated_cost_cny"],
                    "currency": "CNY",
                    "error_category": response.get("error_category"),
                    "source_type": execution.get("source_type") or "measured_uat",
                }
            )
        next_cursor = (
            {
                "created_at": rows[-1]["created_at"],
                "execution_id": rows[-1]["execution_id"],
            }
            if rows
            else cursor
        )
        return events, next_cursor

    def _realtime(self, rebuild: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cursor = {} if rebuild else self._cursor("realtime_logs")
        if not self._table_exists("realtime_log_records"):
            return [], cursor
        after = str(cursor.get("created_at") or "")
        after_id = str(cursor.get("record_id") or "")
        with self.connect() as db:
            rows = db.execute(
                """SELECT record_id,created_at,normalized_json
                   FROM realtime_log_records
                   WHERE tenant_id=? AND workspace_id=? AND
                     (created_at>? OR (created_at=? AND record_id>?))
                   ORDER BY created_at,record_id""",
                (*self.scope.sql_parameters(), after, after, after_id),
            ).fetchall()
        events = []
        for row in rows:
            item = _json(row["normalized_json"])
            events.append(
                {
                    **item,
                    "evidence_id": f"realtime:{row['record_id']}",
                    "observed_at": (
                        item.get("normalized_utc_timestamp")
                        or item.get("platform_created_at")
                    ),
                    "actual_channel": (
                        item.get("actual_channel_id")
                        or item.get("actual_channel_name")
                    ),
                }
            )
        next_cursor = (
            {"created_at": rows[-1]["created_at"], "record_id": rows[-1]["record_id"]}
            if rows
            else cursor
        )
        return events, next_cursor

    def _standardized_calls(
        self, rebuild: bool
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Project the canonical unified ledger, never legacy parallel stores."""
        cursor = {} if rebuild else self._cursor("standardized_call_logs")
        if not self._table_exists("standardized_call_logs"):
            return [], cursor
        after = int(cursor.get("cursor_id") or 0)
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM standardized_call_logs
                   WHERE tenant_id=? AND workspace_id=?
                     AND duplicate_of IS NULL AND cursor_id>?
                   ORDER BY cursor_id""",
                (*self.scope.sql_parameters(), after),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            status = str(item.get("request_status") or "").lower()
            cost_type = str(item.get("cost_type") or item.get("cost_source") or "")
            cost = item.get("provider_cost_amount_exact")
            if cost in (None, ""):
                cost = item.get("cost_amount")
            actual_cost = cost if cost_type in {"actual_provider_cost", "provider_actual"} else None
            estimated_cost = cost if cost_type in {"estimated_versioned_price", "versioned_estimate"} else None
            source_type = str(item.get("source_type") or "unknown")
            traffic_class = str(item.get("traffic_class") or (
                "historical" if source_type == "historical_uat_csv" else "business"
            )).lower()
            events.append({
                "evidence_id": f"call-log:{item['record_id']}",
                "environment_id": item.get("environment_id") or "china_uat",
                "requested_model": item.get("requested_model"),
                "actual_model": item.get("actual_model") or item.get("billed_model"),
                "actual_channel": item.get("channel_id") or item.get("channel_name") or "unknown",
                "request_profile_id": item.get("strategy_variant") or item.get("endpoint_type") or "default",
                "observed_at": item.get("occurred_at"),
                "success": status in {"success", "succeeded", "completed"} if status else None,
                "http_status": item.get("http_status"),
                "latency_ms": item.get("total_latency_ms"),
                "ttft_ms": item.get("first_token_latency_ms"),
                "stream": item.get("stream"),
                "error_category": item.get("error_category"),
                "estimated_cost": estimated_cost,
                "actual_cost": actual_cost,
                "currency": item.get("currency"),
                "fallback": item.get("fallback_used"),
                "schedulable": traffic_class not in {"uat_fault_injection"},
                "source_type": source_type,
                "source_reliability": 1.0 if source_type == "realtime_execution" else 0.85,
                "traffic_class": traffic_class,
                "input_tokens": item.get("input_tokens"),
                "cached_input_tokens": item.get("cached_input_tokens"),
                "output_tokens": item.get("output_tokens"),
                "cost_status": item.get("cost_status"),
            })
        next_cursor = {"cursor_id": int(rows[-1]["cursor_id"])} if rows else cursor
        return events, next_cursor

    def synchronize(
        self, *, rebuild: bool = False, as_of: datetime | None = None
    ) -> dict[str, Any]:
        self.security.authorize("snapshot.generate", required_role="metrics_service")
        # One-time contract migration: replace the legacy parallel projections
        # with the canonical unified ledger. Source evidence remains untouched.
        if "standardized_call_logs" in self.source_allowlist and not rebuild:
            if not self._cursor("standardized_call_logs"):
                with self.connect() as db:
                    existing = db.execute(
                        """SELECT COUNT(*) FROM metric_evidence_events
                           WHERE tenant_id=? AND workspace_id=?""",
                        self.scope.sql_parameters(),
                    ).fetchone()[0]
                rebuild = bool(existing)
        now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        started = now.isoformat()
        with self.connect() as db:
            db.execute(
                """UPDATE metric_refresh_runtime
                   SET last_started_at=?,last_status='running',last_error_code=NULL
                   WHERE singleton_id=1 AND tenant_id=? AND workspace_id=?""",
                (started, *self.scope.sql_parameters()),
            )
        source_loaders = {
            "protected_v3": self._protected,
            "confirmed_imports": self._imports,
            "uat_executions": self._uat,
            "realtime_logs": self._realtime,
            "standardized_call_logs": self._standardized_calls,
        }
        sources = {
            name: loader(rebuild)
            for name, loader in source_loaders.items()
            if name in self.source_allowlist
        }
        accepted: list[dict[str, Any]] = []
        rejection_counts: dict[str, int] = {}
        source_counts: dict[str, dict[str, int]] = {}
        cursors: dict[str, dict[str, Any]] = {}
        for source_name, (events, cursor) in sources.items():
            cursors[source_name] = cursor
            valid = 0
            rejected = 0
            for event in events:
                try:
                    self.metrics.normalize(event)
                except (MetricsError, TypeError, ValueError) as exc:
                    code = str(exc) or "invalid_metric_evidence"
                    rejection_counts[code] = rejection_counts.get(code, 0) + 1
                    rejected += 1
                else:
                    accepted.append(event)
                    valid += 1
            source_counts[source_name] = {
                "observed": len(events),
                "accepted": valid,
                "rejected": rejected,
            }
        result = (
            self.metrics.rebuild(accepted, as_of=now)
            if rebuild
            else self.metrics.ingest(accepted, as_of=now)
        )
        self._set_cursors(cursors, now)
        with self.connect() as db:
            completed = datetime.now(timezone.utc).isoformat()
            db.execute(
                """INSERT INTO metric_source_sync_audit(
                     started_at,completed_at,aggregation_version,rebuild,
                     accepted_count,rejected_count,source_counts_json,
                     rejection_counts_json,tenant_id,workspace_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    started,
                    completed,
                    AGGREGATION_VERSION,
                    int(rebuild),
                    len(accepted),
                    sum(rejection_counts.values()),
                    json.dumps(source_counts, sort_keys=True),
                    json.dumps(rejection_counts, sort_keys=True),
                    *self.scope.sql_parameters(),
                ),
            )
            db.execute(
                """UPDATE metric_refresh_runtime
                   SET last_completed_at=?,last_status='ready',
                       last_error_code=NULL,consecutive_failures=0
                   WHERE singleton_id=1 AND tenant_id=? AND workspace_id=?""",
                (completed, *self.scope.sql_parameters()),
            )
        return {
            **result,
            "rebuild": rebuild,
            "source_counts": source_counts,
            "rejected_count": sum(rejection_counts.values()),
            "rejection_counts": rejection_counts,
            "protected_evidence_modified": False,
            "real_external_access_count": 0,
        }

    def synchronize_safely(
        self, *, rebuild: bool = False, as_of: datetime | None = None
    ) -> dict[str, Any]:
        try:
            return self.synchronize(rebuild=rebuild, as_of=as_of)
        except Exception as exc:
            allowed = {
                "protected_v3_missing",
                "protected_v3_integrity_mismatch",
                "invalid_evidence_timestamp",
                "missing_evidence_timestamp",
                "timezone_required",
                "evidence_id_payload_conflict",
            }
            code = str(exc) if str(exc) in allowed else "metric_refresh_failed"
            with self.connect() as db:
                db.execute(
                    """UPDATE metric_refresh_runtime
                       SET last_completed_at=?,last_status='failed',
                           last_error_code=?,
                           consecutive_failures=consecutive_failures+1
                       WHERE singleton_id=1 AND tenant_id=? AND workspace_id=?""",
                    (
                        datetime.now(timezone.utc).isoformat(), code,
                        *self.scope.sql_parameters(),
                    ),
                )
            raise

    def status(self) -> dict[str, Any]:
        self.security.authorize("snapshot.read")
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM metric_refresh_runtime WHERE singleton_id=1
                   AND tenant_id=? AND workspace_id=?""",
                self.scope.sql_parameters(),
            ).fetchone()
        return dict(row) if row else {
            "last_status": "unknown",
            "last_error_code": "metric_refresh_state_unavailable",
            "consecutive_failures": 0,
        }
