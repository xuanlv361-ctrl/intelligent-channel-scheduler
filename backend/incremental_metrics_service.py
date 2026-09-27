"""Durable, network-free incremental metric snapshots.

Raw evidence remains immutable in its owning store.  This module keeps a
minimal, normalized projection keyed by evidence ID and materializes versioned
snapshots.  Read APIs query snapshots only; they never rescan raw evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from backend.security.authorization import AuthorizationError, AuthorizationService
from backend.security.config import EnterpriseIdentityConfig
from backend.security.identity import PrincipalResolver, VerifiedIdentity
from backend.security.principal import PrincipalContext, PrincipalType
from backend.statistical_confidence_service import StatisticalConfidenceService
from backend.tenant_security import TenantScope
from src.statistical_confidence import (
    CONFIDENCE_VERSION,
    calculate_weighted_wilson,
)

AGGREGATION_VERSION = "incremental_metrics_v1"
WINDOWS = {"5m": 300, "1h": 3600, "24h": 86400}
SOURCE_RELIABILITY = {
    "measured_unified_uat": 1.0,
    "measured_uat": 1.0,
    "measured_uat_browser_collector": 0.95,
    "backend_log_export": 0.9,
    "confirmed_import": 0.85,
    "integration_test_fixture": 0.5,
    "demo_mock": 0.0,
    "historical_uat_csv": 0.85,
    "realtime_execution": 1.0,
    "uat_fault_injection": 0.7,
}

_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class EnterpriseServiceSecurity:
    """Verified, scope-bound identity used by internal enterprise services."""

    principal: PrincipalContext
    authorization: AuthorizationService
    scope: TenantScope

    def authorize(self, permission: str, *, required_role: str | None = None) -> None:
        self.authorization.authorize(
            self.principal,
            permission,
            tenant_id=self.scope.tenant_id,
            workspace_id=self.scope.workspace_id,
            principal_type=PrincipalType.SERVICE if required_role else None,
        )
        if required_role and required_role not in self.principal.roles:
            raise MetricsError("service_identity_role_mismatch")


def bind_enterprise_service_security(
    *,
    principal: PrincipalContext | None,
    authorization: AuthorizationService | None,
    development_mode: bool,
    development_role: str,
) -> EnterpriseServiceSecurity:
    """Bind a verified identity, with an explicit local-development adapter.

    The development adapter is deliberately opt-in and issues only a short-lived
    service Principal through ``PrincipalResolver``.  It is never selected from
    deployment environment heuristics and therefore cannot become a production
    fallback.
    """

    development_mode = development_mode or (
        os.environ.get("ICS_EXPLICIT_LOCAL_DEVELOPMENT") == "1"
    )
    if principal is None or authorization is None:
        if not development_mode or principal is not None or authorization is not None:
            raise MetricsError("enterprise_service_identity_required")
        config = EnterpriseIdentityConfig.load(
            _ROOT / "config" / "enterprise_identity_v1.json"
        )
        if config.deployment_mode != "development":
            raise MetricsError("development_service_identity_forbidden")
        authorization = AuthorizationService(
            _ROOT / "config" / "rbac_permissions_v1.json"
        )
        now = datetime.now(timezone.utc)
        development_scope = TenantScope.local_development()
        principal = PrincipalResolver(config, authorization).resolve_verified(
            VerifiedIdentity(
                principal_id=f"service:local-{development_role}",
                principal_type=PrincipalType.SERVICE,
                tenant_id=development_scope.tenant_id,
                workspace_id=development_scope.workspace_id,
                roles=(development_role,),
                authn_method="explicit_local_development_service_adapter_v1",
                issued_at=now,
                expires_at=now + timedelta(hours=1),
                session_id=None,
                token_id=f"local-{uuid.uuid4().hex}",
                is_development_identity=True,
                security_version=1,
            )
        )
    scope = TenantScope(principal.tenant_id, principal.workspace_id)
    return EnterpriseServiceSecurity(principal, authorization, scope)


class MetricsError(ValueError):
    """Stable, non-secret metrics domain error."""


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise MetricsError("invalid_evidence_timestamp") from exc
    else:
        raise MetricsError("missing_evidence_timestamp")
    # Legacy database rows were documented and persisted as UTC before the
    # timezone suffix became mandatory. Treat only those naive values as UTC;
    # display conversion remains a presentation concern.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MetricsError("invalid_numeric_evidence") from exc
    if not math.isfinite(result) or result < 0:
        raise MetricsError("invalid_numeric_evidence")
    return result


def _bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise MetricsError("invalid_boolean_evidence")


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 6)
    return round(
        ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 6
    )


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 8) if values else None


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class NormalizedMetricEvent:
    evidence_id: str
    environment_id: str
    model: str
    channel: str
    stream: bool | None
    request_profile_id: str
    observed_at: str
    success: bool | None
    latency_ms: float | None
    ttft_ms: float | None
    tpot_ms: float | None
    http_status: int | None
    timeout: bool | None
    incomplete_sse: bool | None
    estimated_cost: float | None
    actual_cost: float | None
    currency: str | None
    mapping_anomaly: bool | None
    fallback: bool | None
    schedulable: bool | None
    source_type: str
    source_reliability: float
    traffic_class: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    cost_status: str | None
    missing_fields: tuple[str, ...]
    payload_hash: str

    def as_row(self) -> tuple[Any, ...]:
        values = self.__dict__.copy()
        values["stream"] = None if self.stream is None else int(self.stream)
        for name in (
            "success",
            "timeout",
            "incomplete_sse",
            "mapping_anomaly",
            "fallback",
            "schedulable",
        ):
            value = values[name]
            values[name] = None if value is None else int(value)
        values["missing_fields"] = json.dumps(self.missing_fields)
        return tuple(values[name] for name in EVENT_COLUMNS)


EVENT_COLUMNS = (
    "evidence_id",
    "environment_id",
    "model",
    "channel",
    "stream",
    "request_profile_id",
    "observed_at",
    "success",
    "latency_ms",
    "ttft_ms",
    "tpot_ms",
    "http_status",
    "timeout",
    "incomplete_sse",
    "estimated_cost",
    "actual_cost",
    "currency",
    "mapping_anomaly",
    "fallback",
    "schedulable",
    "source_type",
    "source_reliability",
    "traffic_class",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "cost_status",
    "missing_fields",
    "payload_hash",
)


class IncrementalMetricsService:
    def __init__(
        self,
        path: Path,
        *,
        minimum_healthy_samples: int = 5,
        freshness_seconds: int = 3600,
        principal: PrincipalContext | None = None,
        authorization: AuthorizationService | None = None,
        development_mode: bool = False,
    ):
        self.path = Path(path)
        self.minimum_healthy_samples = max(1, int(minimum_healthy_samples))
        self.freshness_seconds = max(1, int(freshness_seconds))
        self.security = bind_enterprise_service_security(
            principal=principal,
            authorization=authorization,
            development_mode=development_mode,
            development_role="metrics_service",
        )
        self.scope = self.security.scope
        self._init()
        self.confidence = StatisticalConfidenceService(
            self.path, security=self.security
        )

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS metric_evidence_events(
                  evidence_id TEXT NOT NULL,
                  environment_id TEXT NOT NULL,
                  model TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  stream INTEGER,
                  request_profile_id TEXT NOT NULL,
                  observed_at TEXT NOT NULL,
                  success INTEGER,
                  latency_ms REAL,
                  ttft_ms REAL,
                  tpot_ms REAL,
                  http_status INTEGER,
                  timeout INTEGER,
                  incomplete_sse INTEGER,
                  estimated_cost REAL,
                  actual_cost REAL,
                  currency TEXT,
                  mapping_anomaly INTEGER,
                  fallback INTEGER,
                  schedulable INTEGER,
                  source_type TEXT NOT NULL,
                  source_reliability REAL NOT NULL,
                  traffic_class TEXT NOT NULL DEFAULT 'business',
                  input_tokens INTEGER,
                  cached_input_tokens INTEGER,
                  output_tokens INTEGER,
                  cost_status TEXT,
                  missing_fields TEXT NOT NULL,
                  payload_hash TEXT NOT NULL,
                  ingested_at TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,evidence_id)
                );
                CREATE INDEX IF NOT EXISTS ix_metric_events_observed
                  ON metric_evidence_events(tenant_id,workspace_id,observed_at);
                CREATE INDEX IF NOT EXISTS ix_metric_events_dimensions
                  ON metric_evidence_events(
                    tenant_id,workspace_id,environment_id,model,channel,stream,
                    request_profile_id,currency);
                CREATE TABLE IF NOT EXISTS metric_aggregation_state(
                  aggregation_version TEXT NOT NULL,
                  source_watermark TEXT,
                  last_refresh_at TEXT,
                  event_count INTEGER NOT NULL DEFAULT 0,
                  last_audit_id TEXT,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,aggregation_version)
                );
                CREATE TABLE IF NOT EXISTS metric_snapshots(
                  snapshot_id TEXT NOT NULL,
                  aggregation_version TEXT NOT NULL,
                  window_name TEXT NOT NULL,
                  window_seconds INTEGER NOT NULL,
                  environment_id TEXT NOT NULL,
                  model TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  stream INTEGER,
                  request_profile_id TEXT NOT NULL,
                  traffic_class TEXT NOT NULL DEFAULT 'business',
                  currency TEXT,
                  window_start TEXT NOT NULL,
                  window_end TEXT NOT NULL,
                  generated_at TEXT NOT NULL,
                  last_evidence_at TEXT,
                  evidence_age_seconds REAL,
                  freshness_status TEXT NOT NULL,
                  data_state TEXT NOT NULL,
                  sample_count INTEGER NOT NULL,
                  source_watermark TEXT,
                  metrics_json TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,snapshot_id),
                  UNIQUE(
                    tenant_id,workspace_id,aggregation_version,window_name,
                    environment_id,model,channel,
                    stream,request_profile_id,traffic_class,currency)
                );
                CREATE INDEX IF NOT EXISTS ix_metric_snapshots_query
                  ON metric_snapshots(
                    tenant_id,workspace_id,window_name,environment_id,model,
                    channel,generated_at);
                CREATE TABLE IF NOT EXISTS metric_aggregation_audit(
                  audit_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  aggregation_version TEXT NOT NULL,
                  details_json TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,audit_id)
                );
                """
            )
            event_columns={row[1] for row in db.execute("PRAGMA table_info(metric_evidence_events)")}
            for name,definition in (
                ("traffic_class","TEXT NOT NULL DEFAULT 'business'"),
                ("input_tokens","INTEGER"),("cached_input_tokens","INTEGER"),
                ("output_tokens","INTEGER"),("cost_status","TEXT"),
            ):
                if name not in event_columns:
                    db.execute(f"ALTER TABLE metric_evidence_events ADD COLUMN {name} {definition}")
            snapshot_columns={row[1] for row in db.execute("PRAGMA table_info(metric_snapshots)")}
            if "traffic_class" not in snapshot_columns:
                db.execute("DROP TABLE metric_snapshots")
                db.executescript("""
                CREATE TABLE metric_snapshots(
                  snapshot_id TEXT NOT NULL,aggregation_version TEXT NOT NULL,
                  window_name TEXT NOT NULL,window_seconds INTEGER NOT NULL,
                  environment_id TEXT NOT NULL,model TEXT NOT NULL,channel TEXT NOT NULL,
                  stream INTEGER,request_profile_id TEXT NOT NULL,
                  traffic_class TEXT NOT NULL DEFAULT 'business',currency TEXT,
                  window_start TEXT NOT NULL,window_end TEXT NOT NULL,generated_at TEXT NOT NULL,
                  last_evidence_at TEXT,evidence_age_seconds REAL,freshness_status TEXT NOT NULL,
                  data_state TEXT NOT NULL,sample_count INTEGER NOT NULL,source_watermark TEXT,
                  metrics_json TEXT NOT NULL,tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,snapshot_id),
                  UNIQUE(tenant_id,workspace_id,aggregation_version,window_name,environment_id,
                    model,channel,stream,request_profile_id,traffic_class,currency));
                CREATE INDEX IF NOT EXISTS ix_metric_snapshots_query ON metric_snapshots(
                  tenant_id,workspace_id,window_name,environment_id,model,channel,generated_at);
                """)
            db.execute(
                """INSERT OR IGNORE INTO metric_aggregation_state(
                     aggregation_version,event_count,tenant_id,workspace_id)
                   VALUES(?,0,?,?)""",
                (AGGREGATION_VERSION, *self.scope.sql_parameters()),
            )

    def normalize(self, raw: dict[str, Any]) -> NormalizedMetricEvent:
        evidence_id = str(
            raw.get("evidence_id")
            or raw.get("execution_id")
            or raw.get("request_id")
            or ""
        ).strip()
        if not evidence_id:
            raise MetricsError("missing_evidence_id")
        observed = _utc(
            raw.get("observed_at")
            or raw.get("completed_at")
            or raw.get("timestamp")
            or raw.get("measured_at")
            or raw.get("actual_executed_at")
        )
        environment = str(raw.get("environment_id") or "").strip()
        model = str(
            raw.get("actual_model") or raw.get("requested_model") or raw.get("model") or ""
        ).strip()
        channel = str(raw.get("actual_channel") or raw.get("channel_id") or "").strip()
        profile = str(
            raw.get("request_profile_id") or raw.get("profile") or ""
        ).strip()
        source_type = str(raw.get("source_type") or "unknown").strip()
        currency_raw = raw.get("currency")
        currency = str(currency_raw).strip().upper() if currency_raw else None
        http_status_value = raw.get("http_status")
        http_status = (
            int(http_status_value) if http_status_value not in (None, "") else None
        )
        if http_status is not None and not 100 <= http_status <= 599:
            raise MetricsError("invalid_http_status")
        success = _bool(raw.get("success"))
        if success is None:
            result = str(raw.get("result") or "").strip().lower()
            if result in {"success", "succeeded", "completed"}:
                success = True
            elif result in {"failure", "failed", "error"}:
                success = False
            elif http_status is not None:
                success = 200 <= http_status < 400
        stream = _bool(raw.get("stream"))
        incomplete_sse = _bool(raw.get("incomplete_sse"))
        if incomplete_sse is None and stream is True:
            complete = _bool(raw.get("sse_complete"))
            incomplete_sse = None if complete is None else not complete
        timeout = _bool(raw.get("timeout"))
        if timeout is None:
            category = str(raw.get("error_category") or "").lower()
            timeout = "timeout" in category if category else None
        mapping = _bool(raw.get("mapping_anomaly"))
        if mapping is None and raw.get("requested_model") and raw.get("actual_model"):
            mapping = str(raw["requested_model"]) != str(raw["actual_model"])
        estimated_cost = _number(
            raw.get("estimated_cost")
            if raw.get("estimated_cost") is not None
            else raw.get("estimated_cost_cny")
        )
        actual_cost = _number(
            raw.get("actual_cost")
            if raw.get("actual_cost") is not None
            else raw.get("cost_cny")
        )
        if (estimated_cost is not None or actual_cost is not None) and not currency:
            # Never merge values whose currency is not confirmed.
            estimated_cost = None
            actual_cost = None
        fields = {
            "environment_id": environment,
            "model": model,
            "channel": channel,
            "stream": stream,
            "request_profile_id": profile,
            "success": success,
            "latency_ms": _number(
                raw.get("latency_ms")
                if raw.get("latency_ms") is not None
                else raw.get("total_latency_ms")
            ),
            "ttft_ms": _number(raw.get("ttft_ms")),
            "tpot_ms": _number(raw.get("tpot_ms")),
            "http_status": http_status,
            "timeout": timeout,
            "incomplete_sse": incomplete_sse,
            "estimated_cost": estimated_cost,
            "actual_cost": actual_cost,
            "currency": currency,
            "mapping_anomaly": mapping,
            "fallback": _bool(raw.get("fallback")),
            "schedulable": _bool(raw.get("schedulable")),
            "source_type": source_type,
            "source_reliability": float(
                min(
                    float(raw["source_reliability"]),
                    SOURCE_RELIABILITY.get(source_type, 0.0),
                )
                if raw.get("source_reliability") is not None
                else SOURCE_RELIABILITY.get(source_type, 0.0)
            ),
            "traffic_class": str(raw.get("traffic_class") or "business").strip().lower(),
            "input_tokens": int(raw["input_tokens"]) if raw.get("input_tokens") not in (None, "") else None,
            "cached_input_tokens": int(raw["cached_input_tokens"]) if raw.get("cached_input_tokens") not in (None, "") else None,
            "output_tokens": int(raw["output_tokens"]) if raw.get("output_tokens") not in (None, "") else None,
            "cost_status": str(raw.get("cost_status") or "").strip() or None,
        }
        if not 0 <= fields["source_reliability"] <= 1:
            raise MetricsError("invalid_source_reliability")
        missing = tuple(
            sorted(
                name
                for name in (
                    "environment_id",
                    "model",
                    "channel",
                    "stream",
                    "request_profile_id",
                    "success",
                    "latency_ms",
                    "http_status",
                )
                if fields[name] in (None, "")
            )
        )
        canonical = {
            "evidence_id": evidence_id,
            "observed_at": observed.isoformat(),
            **fields,
            "missing_fields": missing,
        }
        return NormalizedMetricEvent(
            evidence_id=evidence_id,
            observed_at=observed.isoformat(),
            missing_fields=missing,
            payload_hash=_canonical_hash(canonical),
            **fields,
        )

    def ingest(
        self, events: Iterable[dict[str, Any]], *, as_of: datetime | None = None
    ) -> dict[str, Any]:
        self.security.authorize("snapshot.generate", required_role="metrics_service")
        normalized = [self.normalize(event) for event in events]
        now = _utc(as_of or datetime.now(timezone.utc))
        inserted = duplicates = late_arrivals = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = db.execute(
                """SELECT source_watermark FROM metric_aggregation_state
                   WHERE aggregation_version=? AND tenant_id=? AND workspace_id=?""",
                (AGGREGATION_VERSION, *self.scope.sql_parameters()),
            ).fetchone()
            watermark = _utc(state["source_watermark"]) if state and state[0] else None
            placeholders = ",".join("?" for _ in EVENT_COLUMNS) + ",?,?,?"
            columns = ",".join(EVENT_COLUMNS) + ",ingested_at,tenant_id,workspace_id"
            for event in normalized:
                existing = db.execute(
                    """SELECT payload_hash FROM metric_evidence_events
                       WHERE evidence_id=? AND tenant_id=? AND workspace_id=?""",
                    (event.evidence_id, *self.scope.sql_parameters()),
                ).fetchone()
                if existing:
                    if existing["payload_hash"] != event.payload_hash:
                        raise MetricsError("evidence_id_payload_conflict")
                    duplicates += 1
                    continue
                db.execute(
                    f"INSERT INTO metric_evidence_events({columns}) VALUES({placeholders})",
                    event.as_row() + (now.isoformat(), *self.scope.sql_parameters()),
                )
                inserted += 1
                if watermark and _utc(event.observed_at) < watermark:
                    late_arrivals += 1
                if watermark is None or _utc(event.observed_at) > watermark:
                    watermark = _utc(event.observed_at)
            audit_id = self._audit(
                db,
                "metric_events_ingested",
                now,
                {
                    "received": len(normalized),
                    "inserted": inserted,
                    "duplicates": duplicates,
                    "late_arrivals": late_arrivals,
                },
            )
            db.execute(
                """UPDATE metric_aggregation_state
                   SET source_watermark=?,
                       event_count=(SELECT COUNT(*) FROM metric_evidence_events
                         WHERE tenant_id=? AND workspace_id=?),
                       last_audit_id=?
                   WHERE aggregation_version=? AND tenant_id=? AND workspace_id=?""",
                (
                    watermark.isoformat() if watermark else None,
                    *self.scope.sql_parameters(),
                    audit_id,
                    AGGREGATION_VERSION,
                    *self.scope.sql_parameters(),
                ),
            )
            self._refresh(db, now)
        return {
            "status": "ready",
            "aggregation_version": AGGREGATION_VERSION,
            "received": len(normalized),
            "inserted": inserted,
            "duplicates": duplicates,
            "late_arrivals": late_arrivals,
            "network_called": False,
        }

    def rebuild(
        self, events: Iterable[dict[str, Any]], *, as_of: datetime | None = None
    ) -> dict[str, Any]:
        self.security.authorize("snapshot.generate", required_role="metrics_service")
        normalized = [self.normalize(event) for event in events]
        now = _utc(as_of or datetime.now(timezone.utc))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM metric_snapshots WHERE tenant_id=? AND workspace_id=?",
                self.scope.sql_parameters(),
            )
            db.execute(
                "DELETE FROM metric_evidence_events WHERE tenant_id=? AND workspace_id=?",
                self.scope.sql_parameters(),
            )
            placeholders = ",".join("?" for _ in EVENT_COLUMNS) + ",?,?,?"
            columns = ",".join(EVENT_COLUMNS) + ",ingested_at,tenant_id,workspace_id"
            seen: dict[str, str] = {}
            for event in normalized:
                previous = seen.get(event.evidence_id)
                if previous and previous != event.payload_hash:
                    raise MetricsError("evidence_id_payload_conflict")
                if previous:
                    continue
                seen[event.evidence_id] = event.payload_hash
                db.execute(
                    f"INSERT INTO metric_evidence_events({columns}) VALUES({placeholders})",
                    event.as_row() + (now.isoformat(), *self.scope.sql_parameters()),
                )
            watermark = max(
                (_utc(event.observed_at) for event in normalized), default=None
            )
            audit_id = self._audit(
                db,
                "metric_store_rebuilt",
                now,
                {"source_count": len(normalized), "unique_count": len(seen)},
            )
            db.execute(
                """UPDATE metric_aggregation_state
                   SET source_watermark=?,event_count=?,last_audit_id=?
                   WHERE aggregation_version=? AND tenant_id=? AND workspace_id=?""",
                (
                    watermark.isoformat() if watermark else None,
                    len(seen),
                    audit_id,
                    AGGREGATION_VERSION,
                    *self.scope.sql_parameters(),
                ),
            )
            self._refresh(db, now)
        return {
            "status": "ready",
            "aggregation_version": AGGREGATION_VERSION,
            "event_count": len(seen),
            "network_called": False,
        }

    def _refresh(self, db: sqlite3.Connection, now: datetime) -> None:
        state = db.execute(
            """SELECT source_watermark FROM metric_aggregation_state
               WHERE aggregation_version=? AND tenant_id=? AND workspace_id=?""",
            (AGGREGATION_VERSION, *self.scope.sql_parameters()),
        ).fetchone()
        watermark = state["source_watermark"] if state else None
        db.execute(
            """DELETE FROM metric_snapshots WHERE aggregation_version=?
               AND tenant_id=? AND workspace_id=?""",
            (AGGREGATION_VERSION, *self.scope.sql_parameters()),
        )
        db.execute(
            """DELETE FROM statistical_confidence_snapshots
               WHERE confidence_version=? AND tenant_id=? AND workspace_id=?""",
            (CONFIDENCE_VERSION, *self.scope.sql_parameters()),
        )
        for window_name, seconds in WINDOWS.items():
            start = now - timedelta(seconds=seconds)
            rows = db.execute(
                """SELECT * FROM metric_evidence_events
                   WHERE tenant_id=? AND workspace_id=?
                     AND observed_at>? AND observed_at<=?
                   ORDER BY observed_at,evidence_id""",
                (*self.scope.sql_parameters(), start.isoformat(), now.isoformat()),
            ).fetchall()
            groups: dict[tuple[Any, ...], list[sqlite3.Row]] = {}
            for row in rows:
                key = (
                    row["environment_id"],
                    row["model"],
                    row["channel"],
                    row["stream"],
                    row["request_profile_id"],
                    row["traffic_class"],
                    row["currency"],
                )
                groups.setdefault(key, []).append(row)
            for key in sorted(groups, key=lambda item: tuple(str(v) for v in item)):
                self._write_snapshot(
                    db, window_name, seconds, start, now, key, groups[key], watermark
                )
        audit_id = self._audit(
            db,
            "metric_snapshots_refreshed",
            now,
            {
                "snapshot_count": db.execute(
                    """SELECT COUNT(*) FROM metric_snapshots
                       WHERE tenant_id=? AND workspace_id=?""",
                    self.scope.sql_parameters(),
                ).fetchone()[0]
            },
        )
        db.execute(
            """UPDATE metric_aggregation_state
               SET last_refresh_at=?,last_audit_id=?
               WHERE aggregation_version=? AND tenant_id=? AND workspace_id=?""",
            (
                now.isoformat(), audit_id, AGGREGATION_VERSION,
                *self.scope.sql_parameters(),
            ),
        )
        self.confidence.mark_refresh(
            db,
            calculated_at=now,
            snapshot_count=db.execute(
                """SELECT COUNT(*) FROM statistical_confidence_snapshots
                   WHERE confidence_version=? AND tenant_id=? AND workspace_id=?""",
                (CONFIDENCE_VERSION, *self.scope.sql_parameters()),
            ).fetchone()[0],
        )

    def _write_snapshot(
        self,
        db: sqlite3.Connection,
        window_name: str,
        seconds: int,
        start: datetime,
        now: datetime,
        key: tuple[Any, ...],
        rows: list[sqlite3.Row],
        watermark: str | None,
    ) -> None:
        def numbers(name: str) -> list[float]:
            return [float(row[name]) for row in rows if row[name] is not None]

        def booleans(name: str) -> list[bool]:
            return [bool(row[name]) for row in rows if row[name] is not None]

        statuses = [row["http_status"] for row in rows if row["http_status"] is not None]
        last = max(_utc(row["observed_at"]) for row in rows)
        evidence_age = max(0.0, (now - last).total_seconds())
        schedulable = booleans("schedulable")
        if schedulable and not any(schedulable):
            data_state = "blocked"
        elif evidence_age > self.freshness_seconds:
            data_state = "stale"
        elif len(rows) < self.minimum_healthy_samples:
            data_state = "insufficient_data"
        else:
            data_state = "healthy_evidence"
        freshness = "fresh" if evidence_age <= self.freshness_seconds else "stale"
        dimensions = {
            "environment_id": key[0],
            "model": key[1],
            "channel": key[2],
            "stream": None if key[3] is None else bool(key[3]),
            "request_profile_id": key[4],
            "traffic_class": key[5],
            "currency": key[6],
        }
        identity = _canonical_hash(
            {
                "version": AGGREGATION_VERSION,
                "window": window_name,
                "dimensions": dimensions,
                "window_end": now.isoformat(),
            }
        )
        metric_snapshot_id = f"MS-{identity[:24]}"
        metrics = {
            "request_count": len(rows),
            "success_rate": _rate(booleans("success")),
            "latency_p50_ms": _percentile(numbers("latency_ms"), 0.50),
            "latency_p95_ms": _percentile(numbers("latency_ms"), 0.95),
            "latency_p99_ms": _percentile(numbers("latency_ms"), 0.99),
            "ttft_p50_ms": _percentile(numbers("ttft_ms"), 0.50),
            "ttft_p95_ms": _percentile(numbers("ttft_ms"), 0.95),
            "ttft_p99_ms": _percentile(numbers("ttft_ms"), 0.99),
            "tpot_p50_ms": _percentile(numbers("tpot_ms"), 0.50),
            "tpot_p95_ms": _percentile(numbers("tpot_ms"), 0.95),
            "tpot_p99_ms": _percentile(numbers("tpot_ms"), 0.99),
            "http_429_rate": _rate([status == 429 for status in statuses]),
            "http_5xx_rate": _rate([500 <= status <= 599 for status in statuses]),
            "timeout_rate": _rate(booleans("timeout")),
            "incomplete_sse_rate": _rate(booleans("incomplete_sse")),
            "average_estimated_cost": (
                round(sum(numbers("estimated_cost")) / len(numbers("estimated_cost")), 8)
                if numbers("estimated_cost")
                else None
            ),
            "estimated_cost_p95": _percentile(numbers("estimated_cost"), 0.95),
            "average_actual_cost": (
                round(sum(numbers("actual_cost")) / len(numbers("actual_cost")), 8)
                if numbers("actual_cost")
                else None
            ),
            "actual_cost_p95": _percentile(numbers("actual_cost"), 0.95),
            "actual_cost_total": round(sum(numbers("actual_cost")), 8) if numbers("actual_cost") else None,
            "estimated_cost_total": round(sum(numbers("estimated_cost")), 8) if numbers("estimated_cost") else None,
            "input_tokens_total": int(sum(numbers("input_tokens"))),
            "cached_input_tokens_total": int(sum(numbers("cached_input_tokens"))),
            "output_tokens_total": int(sum(numbers("output_tokens"))),
            "total_tokens": int(sum(numbers("input_tokens"))+sum(numbers("cached_input_tokens"))+sum(numbers("output_tokens"))),
            "pending_provider_sync_count": sum(1 for row in rows if row["cost_status"] == "pending_provider_sync"),
            "model_mapping_anomaly_rate": _rate(booleans("mapping_anomaly")),
            "fallback_rate": _rate(booleans("fallback")),
            "schedulability_rate": _rate(schedulable),
            "missing_field_count": sum(
                len(json.loads(row["missing_fields"])) for row in rows
            ),
            "average_source_reliability": round(
                sum(float(row["source_reliability"]) for row in rows) / len(rows), 8
            ),
            "source_type_counts": {
                source: sum(1 for row in rows if row["source_type"] == source)
                for source in sorted({str(row["source_type"]) for row in rows})
            },
            "contains_mock": any(
                row["source_type"] in {"demo_mock", "integration_test_fixture"}
                for row in rows
            ),
        }
        confidence = calculate_weighted_wilson(
            [
                {
                    "evidence_id": row["evidence_id"],
                    "observed_at": row["observed_at"],
                    "success": (
                        None if row["success"] is None else bool(row["success"])
                    ),
                    "source_type": row["source_type"],
                    "source_reliability": row["source_reliability"],
                    "missing_fields": json.loads(row["missing_fields"]),
                }
                for row in rows
            ],
            window_name=window_name,
            window_end=now,
            metric_snapshot_id=metric_snapshot_id,
            policy=self.confidence.policy,
        )
        db.execute(
            """INSERT INTO metric_snapshots(
                 snapshot_id,aggregation_version,window_name,window_seconds,
                 environment_id,model,channel,stream,request_profile_id,traffic_class,currency,
                 window_start,window_end,generated_at,last_evidence_at,
                 evidence_age_seconds,freshness_status,data_state,sample_count,
                 source_watermark,metrics_json,tenant_id,workspace_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                metric_snapshot_id,
                AGGREGATION_VERSION,
                window_name,
                seconds,
                *key,
                start.isoformat(),
                now.isoformat(),
                now.isoformat(),
                last.isoformat(),
                round(evidence_age, 6),
                freshness,
                data_state,
                len(rows),
                watermark,
                json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                *self.scope.sql_parameters(),
            ),
        )
        confidence_snapshot_id = (
            f"CS-{_canonical_hash({'snapshot': metric_snapshot_id, 'version': CONFIDENCE_VERSION, 'input': confidence['input_fingerprint']})[:24]}"
        )
        db.execute(
            """INSERT INTO statistical_confidence_snapshots(
                 confidence_snapshot_id,metric_snapshot_id,confidence_version,
                 policy_version,calculated_at,confidence_state,confidence_label,
                 input_fingerprint,result_json,tenant_id,workspace_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                confidence_snapshot_id,
                metric_snapshot_id,
                CONFIDENCE_VERSION,
                confidence["policy_version"],
                now.isoformat(),
                confidence["confidence_state"],
                confidence["confidence_label"],
                confidence["input_fingerprint"],
                json.dumps(confidence, ensure_ascii=False, sort_keys=True),
                *self.scope.sql_parameters(),
            ),
        )

    def _audit(
        self,
        db: sqlite3.Connection,
        event_type: str,
        created_at: datetime,
        details: dict[str, Any],
    ) -> str:
        audit_id = f"MA-{uuid.uuid4()}"
        db.execute(
            """INSERT INTO metric_aggregation_audit(
                 audit_id,event_type,created_at,aggregation_version,details_json,
                 tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)""",
            (
                audit_id,
                event_type,
                created_at.isoformat(),
                AGGREGATION_VERSION,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                *self.scope.sql_parameters(),
            ),
        )
        return audit_id

    def list_snapshots(
        self,
        *,
        window: str | None = None,
        environment_id: str | None = None,
        model: str | None = None,
        channel: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.security.authorize("snapshot.read")
        if window is not None and window not in WINDOWS:
            raise MetricsError("invalid_metric_window")
        limit = min(max(int(limit), 1), 500)
        offset = max(int(offset), 0)
        clauses = ["aggregation_version=?", "tenant_id=?", "workspace_id=?"]
        values: list[Any] = [AGGREGATION_VERSION, *self.scope.sql_parameters()]
        for column, value in (
            ("window_name", window),
            ("environment_id", environment_id),
            ("model", model),
            ("channel", channel),
        ):
            if value not in (None, ""):
                clauses.append(f"{column}=?")
                values.append(value)
        where = " AND ".join(clauses)
        with self.connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM metric_snapshots WHERE {where}", values
            ).fetchone()[0]
            rows = db.execute(
                f"""SELECT * FROM metric_snapshots WHERE {where}
                    ORDER BY window_name,environment_id,model,channel,
                             request_profile_id,currency
                    LIMIT ? OFFSET ?""",
                [*values, limit, offset],
            ).fetchall()
            state = db.execute(
                """SELECT * FROM metric_aggregation_state
                   WHERE aggregation_version=? AND tenant_id=? AND workspace_id=?""",
                (AGGREGATION_VERSION, *self.scope.sql_parameters()),
            ).fetchone()
        items = []
        for row in rows:
            item = dict(row)
            item["stream"] = None if item["stream"] is None else bool(item["stream"])
            item["metrics"] = json.loads(item.pop("metrics_json"))
            items.append(item)
        return {
            "status": "ready" if items else "unknown",
            "aggregation_version": AGGREGATION_VERSION,
            "source_watermark": state["source_watermark"] if state else None,
            "last_refresh_at": state["last_refresh_at"] if state else None,
            "event_count": state["event_count"] if state else 0,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "items": items,
        }

    def audit_events(self) -> list[dict[str, Any]]:
        try:
            self.security.authorize("audit.read")
        except AuthorizationError:
            # The metrics service may inspect only its own scoped aggregation
            # audit as part of deterministic rebuild verification.
            self.security.authorize(
                "snapshot.read", required_role="metrics_service"
            )
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM metric_aggregation_audit
                   WHERE tenant_id=? AND workspace_id=?
                   ORDER BY created_at,audit_id"""
                , self.scope.sql_parameters()
            ).fetchall()
        return [
            {**dict(row), "details": json.loads(row["details_json"])}
            for row in rows
        ]
