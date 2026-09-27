"""Isolated, tenant-scoped fault injection primitives for China UAT acceptance runs.

This module deliberately has no provider or application imports.  An integration
point asks for an outcome at a named layer and either continues to the live
provider or translates the returned fault metadata into its own response/error.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.tenant_security import TenantScope


ALLOWED_ENVIRONMENT = "china_uat"
ALLOWED_ERROR_TYPES = frozenset({
    "400", "401", "429", "500", "502", "503", "524",
    "connection_timeout", "connection_reset", "delayed_response",
    "empty_response", "sse_malformed", "stream_interrupted",
    "usage_missing", "usage_mismatch",
})
ALLOWED_ERROR_LAYERS = frozenset({"pre_request", "post_response", "stream"})
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@*+-]{0,255}$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:authorization|api_?key|access_?token|refresh_?token|password|"
    r"secret|credential|cookie|private_?key|bearer)(?:$|_)", re.IGNORECASE)
_SENSITIVE_VALUE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|\bsk-[A-Za-z0-9_-]{8,}|"
    r"\bAKIA[0-9A-Z]{16}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
    re.IGNORECASE,
)
_CREATE_FIELDS = frozenset({
    "acceptance_run_id", "environment_id", "model", "error_type", "count",
    "delay_ms", "expires_at", "ttl_seconds", "error_layer", "affects_circuit",
    "probe_run_id", "traffic_class",
})


class UatFaultInjectionError(ValueError):
    """Raised for a rejected or invalid fault-injection operation."""


class UatFaultInjectionService:
    """Store and atomically consume UAT-only fault rules.

    Rules are always created disabled.  This ensures constructing the service or
    creating a rule cannot modify provider traffic until an explicit ``enable``.
    """

    def __init__(self, path: str | Path, scope: TenantScope | None = None,
                 clock: Callable[[], datetime] | None = None,
                 policy_path: str | Path | None = None):
        self.path = str(path)
        self.scope = scope or TenantScope.local_development()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        default_policy = Path(__file__).resolve().parents[1] / "config" / "uat_fault_injection_policy_v1.json"
        self.policy_path = Path(policy_path) if policy_path else default_policy
        self.policy = self._load_policy()
        self._init_store()

    def _load_policy(self) -> dict[str, Any]:
        try:
            policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UatFaultInjectionError("fault_injection_policy_unavailable") from exc
        if (policy.get("allowed_environment") != ALLOWED_ENVIRONMENT or
                set(policy.get("allowed_error_types", ())) != ALLOWED_ERROR_TYPES or
                set(policy.get("allowed_error_layers", ())) != ALLOWED_ERROR_LAYERS):
            raise UatFaultInjectionError("fault_injection_policy_invalid")
        return policy

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise UatFaultInjectionError("fault_injection_clock_timezone_required")
        return value.astimezone(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _init_store(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS uat_fault_injection_rules(
              fault_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              acceptance_run_id TEXT NOT NULL,
              probe_run_id TEXT,
              traffic_class TEXT NOT NULL DEFAULT 'probe',
              environment_id TEXT NOT NULL CHECK(environment_id='china_uat'),
              model TEXT NOT NULL,
              error_type TEXT NOT NULL,
              error_layer TEXT NOT NULL,
              configured_count INTEGER NOT NULL,
              remaining_count INTEGER NOT NULL,
              delay_ms INTEGER NOT NULL,
              expires_at TEXT NOT NULL,
              affects_circuit INTEGER NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_uat_fault_rules_scope_match
              ON uat_fault_injection_rules(
                tenant_id,workspace_id,environment_id,enabled,error_layer,model);
            CREATE INDEX IF NOT EXISTS idx_uat_fault_rules_acceptance_run
              ON uat_fault_injection_rules(
                tenant_id,workspace_id,acceptance_run_id);
            CREATE TABLE IF NOT EXISTS uat_fault_injection_audit(
              audit_id TEXT PRIMARY KEY,
              fault_id TEXT,
              tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              acceptance_run_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              created_at TEXT NOT NULL,
              details_json TEXT NOT NULL
            );
            """)
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(uat_fault_injection_rules)")}
            if "probe_run_id" not in columns:
                db.execute("ALTER TABLE uat_fault_injection_rules ADD COLUMN probe_run_id TEXT")
            if "traffic_class" not in columns:
                db.execute("ALTER TABLE uat_fault_injection_rules ADD COLUMN traffic_class TEXT NOT NULL DEFAULT 'probe'")
            db.execute("UPDATE uat_fault_injection_rules SET probe_run_id=acceptance_run_id WHERE probe_run_id IS NULL")

    @staticmethod
    def _assert_no_sensitive_fields(value: Any, path: str = "request") -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                name = str(key)
                if _SENSITIVE_KEY.search(name):
                    # Never include the submitted key or value in the exception.
                    raise UatFaultInjectionError("sensitive_field_not_allowed")
                UatFaultInjectionService._assert_no_sensitive_fields(nested, path)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                UatFaultInjectionService._assert_no_sensitive_fields(nested, path)
        elif isinstance(value, str) and _SENSITIVE_VALUE.search(value):
            raise UatFaultInjectionError("sensitive_value_not_allowed")

    @staticmethod
    def _identifier(value: Any, field: str, *, wildcard: bool = False) -> str:
        if wildcard and value == "*":
            return value
        if not isinstance(value, str) or value != value.strip() or not _SAFE_VALUE.fullmatch(value):
            raise UatFaultInjectionError(f"{field}_invalid")
        return value

    @staticmethod
    def _require_uat(environment_id: str) -> None:
        if environment_id != ALLOWED_ENVIRONMENT:
            raise UatFaultInjectionError("fault_injection_environment_not_allowed")

    def _audit(self, db: sqlite3.Connection, *, fault_id: str | None,
               acceptance_run_id: str, event_type: str,
               details: Mapping[str, Any] | None = None) -> str:
        audit_id = f"UATFIA-{uuid.uuid4().hex[:24].upper()}"
        safe_details = dict(details or {})
        self._assert_no_sensitive_fields(safe_details)
        db.execute("""INSERT INTO uat_fault_injection_audit(
          audit_id,fault_id,tenant_id,workspace_id,acceptance_run_id,event_type,
          created_at,details_json) VALUES(?,?,?,?,?,?,?,?)""", (
          audit_id, fault_id, *self.scope.sql_parameters(), acceptance_run_id,
          event_type, self._now().isoformat(),
          json.dumps(safe_details, ensure_ascii=False, sort_keys=True)))
        return audit_id

    def create(self, body: Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        values = dict(body or {})
        values.update(kwargs)
        self._assert_no_sensitive_fields(values)
        unknown = set(values) - _CREATE_FIELDS
        if unknown:
            raise UatFaultInjectionError("fault_injection_fields_invalid")
        environment = str(values.get("environment_id") or "")
        self._require_uat(environment)
        run_id = self._identifier(values.get("acceptance_run_id"), "acceptance_run_id")
        probe_run_id = self._identifier(
            values.get("probe_run_id") or run_id, "probe_run_id")
        traffic_class = self._identifier(
            values.get("traffic_class") or "probe", "traffic_class")
        if traffic_class != "probe":
            raise UatFaultInjectionError("fault_injection_traffic_class_not_allowed")
        model = self._identifier(values.get("model"), "model", wildcard=True)
        error_type = str(values.get("error_type") or "")
        layer = str(values.get("error_layer") or "")
        if error_type not in ALLOWED_ERROR_TYPES:
            raise UatFaultInjectionError("fault_injection_error_type_invalid")
        if layer not in ALLOWED_ERROR_LAYERS:
            raise UatFaultInjectionError("fault_injection_error_layer_invalid")
        if not isinstance(values.get("affects_circuit"), bool):
            raise UatFaultInjectionError("fault_injection_affects_circuit_invalid")
        try:
            count = int(values.get("count"))
            delay_ms = int(values.get("delay_ms", 0))
        except (TypeError, ValueError) as exc:
            raise UatFaultInjectionError("fault_injection_limits_invalid") from exc
        if isinstance(values.get("count"), bool) or isinstance(values.get("delay_ms"), bool):
            raise UatFaultInjectionError("fault_injection_limits_invalid")
        max_count = int(self.policy["maximum_trigger_count"])
        max_delay = int(self.policy["maximum_delay_ms"])
        if not (1 <= count <= max_count and 0 <= delay_ms <= max_delay):
            raise UatFaultInjectionError("fault_injection_limits_invalid")
        now = self._now()
        raw_expiry = values.get("expires_at")
        raw_ttl = values.get("ttl_seconds")
        if (raw_expiry is None) == (raw_ttl is None):
            raise UatFaultInjectionError("fault_injection_expiry_invalid")
        try:
            if raw_expiry is not None:
                expiry = datetime.fromisoformat(str(raw_expiry))
                if expiry.tzinfo is None:
                    raise ValueError
                expiry = expiry.astimezone(timezone.utc)
            else:
                ttl = int(raw_ttl)
                expiry = now + timedelta(seconds=ttl)
        except (TypeError, ValueError, OverflowError) as exc:
            raise UatFaultInjectionError("fault_injection_expiry_invalid") from exc
        max_ttl = int(self.policy["maximum_ttl_seconds"])
        if not (now < expiry <= now + timedelta(seconds=max_ttl)):
            raise UatFaultInjectionError("fault_injection_expiry_invalid")
        fault_id = f"UATFI-{uuid.uuid4().hex[:24].upper()}"
        timestamp = now.isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO uat_fault_injection_rules(
              fault_id,tenant_id,workspace_id,acceptance_run_id,probe_run_id,
              traffic_class,environment_id,
              model,error_type,error_layer,configured_count,remaining_count,
              delay_ms,expires_at,affects_circuit,enabled,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""", (
              fault_id, *self.scope.sql_parameters(), run_id, probe_run_id,
              traffic_class, environment, model,
              error_type, layer, count, count, delay_ms, expiry.isoformat(),
              int(values["affects_circuit"]), timestamp, timestamp))
            audit_id = self._audit(db, fault_id=fault_id, acceptance_run_id=run_id,
                                   event_type="fault_rule_created",
                                   details={"enabled": False})
            db.commit()
        result = self.get(fault_id)
        result["audit_id"] = audit_id
        return result

    create_rule = create

    def _row(self, db: sqlite3.Connection, fault_id: str) -> sqlite3.Row | None:
        return db.execute("""SELECT * FROM uat_fault_injection_rules
          WHERE fault_id=? AND tenant_id=? AND workspace_id=?""",
          (fault_id, *self.scope.sql_parameters())).fetchone()

    @staticmethod
    def _present(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["affects_circuit"] = bool(result["affects_circuit"])
        return result

    def get(self, fault_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = self._row(db, fault_id)
        if row is None:
            raise UatFaultInjectionError("fault_rule_not_found")
        return self._present(row)

    get_rule = get

    def list(self, *, acceptance_run_id: str | None = None,
             include_expired: bool = True) -> list[dict[str, Any]]:
        clauses = ["tenant_id=?", "workspace_id=?"]
        params: list[Any] = list(self.scope.sql_parameters())
        if acceptance_run_id is not None:
            clauses.append("acceptance_run_id=?")
            params.append(self._identifier(acceptance_run_id, "acceptance_run_id"))
        if not include_expired:
            clauses.append("expires_at>?")
            params.append(self._now().isoformat())
        with self._connect() as db:
            rows = db.execute("SELECT * FROM uat_fault_injection_rules WHERE " +
                              " AND ".join(clauses) + " ORDER BY created_at,fault_id",
                              params).fetchall()
        return [self._present(row) for row in rows]

    list_rules = list

    def _set_enabled(self, fault_id: str, enabled: bool,
                     *, environment_id: str = ALLOWED_ENVIRONMENT) -> dict[str, Any]:
        self._require_uat(environment_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, fault_id)
            if row is None:
                raise UatFaultInjectionError("fault_rule_not_found")
            if enabled and datetime.fromisoformat(row["expires_at"]) <= self._now():
                raise UatFaultInjectionError("fault_rule_expired")
            db.execute("""UPDATE uat_fault_injection_rules SET enabled=?,updated_at=?
              WHERE fault_id=? AND tenant_id=? AND workspace_id=?""", (
              int(enabled), self._now().isoformat(), fault_id,
              *self.scope.sql_parameters()))
            audit_id = self._audit(
                db, fault_id=fault_id, acceptance_run_id=row["acceptance_run_id"],
                event_type="fault_rule_enabled" if enabled else "fault_rule_disabled")
            db.commit()
        result = self.get(fault_id)
        result["audit_id"] = audit_id
        return result

    def enable(self, fault_id: str, *, environment_id: str = ALLOWED_ENVIRONMENT) -> dict[str, Any]:
        return self._set_enabled(fault_id, True, environment_id=environment_id)

    enable_rule = enable

    def disable(self, fault_id: str, *, environment_id: str = ALLOWED_ENVIRONMENT) -> dict[str, Any]:
        return self._set_enabled(fault_id, False, environment_id=environment_id)

    disable_rule = disable

    @staticmethod
    def _passthrough(*, model: str, layer: str) -> dict[str, Any]:
        return {
            "action": "provider_live_pass_through",
            "model": model,
            "error_layer": layer,
            "error_source": "provider_live",
            "is_fault_injected": False,
            "provider_live": True,
            "provider_live_passthrough": True,
        }

    def evaluate(self, *, environment_id: str, model: str,
                 error_layer: str, acceptance_run_id: str | None = None) -> dict[str, Any]:
        """Atomically consume one matching trigger and return integration metadata."""
        self._require_uat(environment_id)
        model = self._identifier(model, "model")
        if error_layer not in ALLOWED_ERROR_LAYERS:
            raise UatFaultInjectionError("fault_injection_error_layer_invalid")
        if acceptance_run_id is not None:
            acceptance_run_id = self._identifier(acceptance_run_id, "acceptance_run_id")
        now_text = self._now().isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            clauses = [
                "tenant_id=?", "workspace_id=?", "environment_id=?", "enabled=1",
                "remaining_count>0", "expires_at>?", "error_layer=?",
                "(model=? OR model='*')",
            ]
            params: list[Any] = [*self.scope.sql_parameters(), environment_id,
                                 now_text, error_layer, model]
            if acceptance_run_id is not None:
                clauses.append("acceptance_run_id=?")
                params.append(acceptance_run_id)
            row = db.execute("SELECT * FROM uat_fault_injection_rules WHERE " +
                " AND ".join(clauses) +
                " ORDER BY CASE WHEN model=? THEN 0 ELSE 1 END,created_at,fault_id LIMIT 1",
                (*params, model)).fetchone()
            if row is None:
                db.commit()
                return self._passthrough(model=model, layer=error_layer)
            changed = db.execute("""UPDATE uat_fault_injection_rules
              SET remaining_count=remaining_count-1,
                  enabled=CASE WHEN remaining_count=1 THEN 0 ELSE enabled END,
                  updated_at=?
              WHERE fault_id=? AND tenant_id=? AND workspace_id=?
                AND enabled=1 AND remaining_count>0""", (
              now_text, row["fault_id"], *self.scope.sql_parameters()))
            if changed.rowcount != 1:
                db.rollback()
                return self._passthrough(model=model, layer=error_layer)
            audit_id = self._audit(db, fault_id=row["fault_id"],
                acceptance_run_id=row["acceptance_run_id"],
                event_type="fault_trigger_consumed",
                details={"model": model, "error_layer": error_layer,
                         "error_type": row["error_type"]})
            db.commit()
        return {
            "action": "inject_fault",
            "fault_id": row["fault_id"],
            "audit_id": audit_id,
            "acceptance_run_id": row["acceptance_run_id"],
            "model": model,
            "error_type": row["error_type"],
            "error_layer": row["error_layer"],
            "delay_ms": row["delay_ms"],
            "affects_circuit": bool(row["affects_circuit"]),
            "error_source": "uat_fault_injection",
            "is_fault_injected": True,
            "provider_live": False,
            "provider_live_passthrough": False,
        }

    match_and_consume = evaluate
    consume = evaluate

    def pre_request_outcome(self, *, environment_id: str, model: str,
                            acceptance_run_id: str | None = None) -> dict[str, Any]:
        return self.evaluate(environment_id=environment_id, model=model,
                             error_layer="pre_request",
                             acceptance_run_id=acceptance_run_id)

    def post_response_outcome(self, *, environment_id: str, model: str,
                              acceptance_run_id: str | None = None) -> dict[str, Any]:
        return self.evaluate(environment_id=environment_id, model=model,
                             error_layer="post_response",
                             acceptance_run_id=acceptance_run_id)

    def stream_outcome(self, *, environment_id: str, model: str,
                       acceptance_run_id: str | None = None) -> dict[str, Any]:
        return self.evaluate(environment_id=environment_id, model=model,
                             error_layer="stream",
                             acceptance_run_id=acceptance_run_id)

    def cleanup_by_acceptance_run_id(self, acceptance_run_id: str, *,
                                     environment_id: str = ALLOWED_ENVIRONMENT) -> dict[str, Any]:
        self._require_uat(environment_id)
        run_id = self._identifier(acceptance_run_id, "acceptance_run_id")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("""SELECT COUNT(*) FROM uat_fault_injection_rules
              WHERE tenant_id=? AND workspace_id=? AND acceptance_run_id=?""",
              (*self.scope.sql_parameters(), run_id)).fetchone()[0]
            db.execute("""DELETE FROM uat_fault_injection_rules
              WHERE tenant_id=? AND workspace_id=? AND acceptance_run_id=?""",
              (*self.scope.sql_parameters(), run_id))
            audit_id = self._audit(db, fault_id=None, acceptance_run_id=run_id,
                                   event_type="acceptance_run_faults_cleaned",
                                   details={"deleted_rule_count": count})
            db.commit()
        return {"acceptance_run_id": run_id, "deleted_rule_count": count,
                "audit_id": audit_id}

    cleanup = cleanup_by_acceptance_run_id

    def list_audit(self, *, acceptance_run_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["tenant_id=?", "workspace_id=?"]
        params: list[Any] = list(self.scope.sql_parameters())
        if acceptance_run_id is not None:
            clauses.append("acceptance_run_id=?")
            params.append(self._identifier(acceptance_run_id, "acceptance_run_id"))
        with self._connect() as db:
            rows = db.execute("SELECT * FROM uat_fault_injection_audit WHERE " +
                " AND ".join(clauses) + " ORDER BY created_at,audit_id", params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result


__all__ = [
    "ALLOWED_ENVIRONMENT", "ALLOWED_ERROR_LAYERS", "ALLOWED_ERROR_TYPES",
    "UatFaultInjectionError", "UatFaultInjectionService",
]
