"""Versioned, fail-closed price catalog with immutable local history.

The service deliberately has no built-in HTTP client.  A source adapter must be
explicitly approved by policy and supplied as an injected transport.  The
default therefore cannot perform a network request accidentally.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.incremental_metrics_service import bind_enterprise_service_security
from backend.security.authorization import AuthorizationError, AuthorizationService
from backend.security.principal import PrincipalContext

Transport = Callable[[Mapping[str, Any], float], Mapping[str, Any]]
_IDENTIFIER = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_PRICE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
_SENSITIVE = ("api_key", "apikey", "authorization", "cookie", "password", "secret", "token")


class PriceCatalogError(ValueError):
    """Stable error code; never contains transport data or credentials."""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise PriceCatalogError("timezone_required")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PriceCatalogError("invalid_price_timestamp") from exc
    if parsed.tzinfo is None:
        raise PriceCatalogError("timezone_required")
    return parsed.astimezone(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest().upper()


def _load_policy(path: str | Path) -> dict[str, Any]:
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PriceCatalogError("invalid_price_sync_policy") from exc
    required = {
        "policy_version", "catalog_schema_version", "network_enabled_default",
        "timeout_seconds", "maximum_attempts", "stale_after_seconds",
        "expire_after_seconds", "allowed_billing_units", "allowed_sources",
    }
    if not isinstance(policy, dict) or not required.issubset(policy):
        raise PriceCatalogError("invalid_price_sync_policy")
    if policy["network_enabled_default"] is not False:
        raise PriceCatalogError("unsafe_price_sync_default")
    if not 0 < float(policy["timeout_seconds"]) <= 10:
        raise PriceCatalogError("invalid_price_sync_timeout")
    if not 1 <= int(policy["maximum_attempts"]) <= 2:
        raise PriceCatalogError("invalid_price_sync_attempts")
    if not 0 < int(policy["stale_after_seconds"]) < int(policy["expire_after_seconds"]):
        raise PriceCatalogError("invalid_price_freshness_policy")
    sources = policy["allowed_sources"]
    if not isinstance(sources, dict) or not sources or any(
        not isinstance(item, dict) or item.get("approved") is not True
        or item.get("adapter") != "injected_transport_v1"
        or not isinstance(item.get("network_required"), bool)
        for item in sources.values()
    ):
        raise PriceCatalogError("invalid_price_source_allowlist")
    policy["policy_sha256"] = _digest(policy)
    return policy


class PriceCatalogService:
    """Owns immutable price versions and a replaceable latest-state pointer."""

    def __init__(self, database_path: str | Path, policy_path: str | Path, *,
                 transport: Transport | None = None,
                 clock: Callable[[], datetime] | None = None,
                 principal: PrincipalContext | None = None,
                 authorization: AuthorizationService | None = None,
                 development_mode: bool = False):
        self.path = Path(database_path)
        self.policy = _load_policy(policy_path)
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.security = bind_enterprise_service_security(
            principal=principal,
            authorization=authorization,
            development_mode=development_mode,
            development_role="price_sync_service",
        )
        self.scope = self.security.scope
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _connection(self):
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _migrate(self) -> None:
        with self._connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS price_catalog_versions(
              catalog_version TEXT NOT NULL, schema_version TEXT NOT NULL,
              policy_version TEXT NOT NULL, source_id TEXT NOT NULL,
              fetched_at TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
              record_count INTEGER NOT NULL, created_at TEXT NOT NULL,
              payload_json TEXT NOT NULL, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,catalog_version),
              UNIQUE(tenant_id,workspace_id,payload_sha256));
            CREATE TABLE IF NOT EXISTS price_catalog_records(
              catalog_version TEXT NOT NULL, environment_id TEXT NOT NULL,
              model_id TEXT NOT NULL, channel_id TEXT NOT NULL,
              currency TEXT NOT NULL, input_unit_price TEXT NOT NULL,
              cached_input_unit_price TEXT,
              output_unit_price TEXT NOT NULL, billing_unit TEXT NOT NULL,
              effective_from TEXT NOT NULL, effective_until TEXT,
              source_id TEXT NOT NULL, fetched_at TEXT NOT NULL,
              record_sha256 TEXT NOT NULL, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,catalog_version,environment_id,
                model_id,channel_id,currency),
              FOREIGN KEY(tenant_id,workspace_id,catalog_version)
                REFERENCES price_catalog_versions(tenant_id,workspace_id,catalog_version));
            CREATE INDEX IF NOT EXISTS ix_price_lookup ON price_catalog_records(
              tenant_id,workspace_id,environment_id,model_id,channel_id,currency,
              effective_from);
            CREATE TABLE IF NOT EXISTS price_sync_state(
              source_id TEXT NOT NULL, last_catalog_version TEXT,
              last_attempted_at TEXT, last_succeeded_at TEXT,
              last_status TEXT NOT NULL, last_error_code TEXT,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,source_id),
              FOREIGN KEY(tenant_id,workspace_id,last_catalog_version)
                REFERENCES price_catalog_versions(tenant_id,workspace_id,catalog_version));
            CREATE TABLE IF NOT EXISTS price_sync_audit(
              audit_id TEXT NOT NULL, event_type TEXT NOT NULL,
              source_id TEXT NOT NULL, created_at TEXT NOT NULL,
              details_json TEXT NOT NULL, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,audit_id));
            CREATE TABLE IF NOT EXISTS model_price_catalog_versions(
              price_version TEXT NOT NULL, environment_id TEXT NOT NULL,
              effective_from TEXT NOT NULL, effective_to TEXT,
              source_type TEXT NOT NULL, source_reference TEXT NOT NULL,
              captured_at TEXT NOT NULL, checksum TEXT NOT NULL,
              model_count INTEGER NOT NULL, confirmed_count INTEGER NOT NULL,
              pending_count INTEGER NOT NULL, created_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,price_version),
              UNIQUE(tenant_id,workspace_id,checksum));
            CREATE TABLE IF NOT EXISTS model_price_catalog_records(
              price_version TEXT NOT NULL, environment_id TEXT NOT NULL,
              model_id TEXT NOT NULL, requested_model TEXT, actual_model TEXT,
              billed_model TEXT, provider TEXT, currency TEXT NOT NULL,
              input_price_per_million_tokens TEXT,
              cached_input_price_per_million_tokens TEXT,
              output_price_per_million_tokens TEXT,
              per_request_price TEXT, pricing_unit TEXT NOT NULL,
              effective_from TEXT NOT NULL, effective_to TEXT,
              source_type TEXT NOT NULL, source_reference TEXT NOT NULL,
              captured_at TEXT NOT NULL, checksum TEXT NOT NULL,
              raw_record_sha256 TEXT, provider_price_version TEXT,
              supported_endpoints_json TEXT,
              confidence TEXT NOT NULL, verification_status TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,price_version,model_id),
              FOREIGN KEY(tenant_id,workspace_id,price_version)
                REFERENCES model_price_catalog_versions(tenant_id,workspace_id,price_version));
            CREATE INDEX IF NOT EXISTS ix_model_price_lookup ON
              model_price_catalog_records(tenant_id,workspace_id,environment_id,
                model_id,effective_from);
            """)
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(price_catalog_records)")}
            if "cached_input_unit_price" not in columns:
                db.execute("ALTER TABLE price_catalog_records "
                           "ADD COLUMN cached_input_unit_price TEXT")
            model_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(model_price_catalog_records)")}
            for name, declaration in {
                "actual_model": "TEXT",
                "raw_record_sha256": "TEXT",
                "provider_price_version": "TEXT",
                "supported_endpoints_json": "TEXT",
            }.items():
                if name not in model_columns:
                    db.execute(
                        f"ALTER TABLE model_price_catalog_records ADD COLUMN {name} {declaration}")

    @staticmethod
    def _optional_decimal(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value)
        if not _PRICE.fullmatch(text):
            raise PriceCatalogError("invalid_price_decimal")
        try:
            decimal = Decimal(text)
        except InvalidOperation as exc:
            raise PriceCatalogError("invalid_price_decimal") from exc
        if not decimal.is_finite() or decimal < 0:
            raise PriceCatalogError("invalid_price_decimal")
        return format(decimal.normalize(), "f") if decimal else "0"

    def import_model_price_version(self, *, price_version: str,
            environment_id: str, effective_from: str, effective_to: str | None,
            source_type: str, source_reference: str,
            records: list[Mapping[str, Any]]) -> dict[str, Any]:
        """Persist an immutable model-level catalog without inventing channels.

        This boundary is intentionally local-only.  Callers must supply evidence
        references and cannot overwrite a previously signed version.
        """
        self.security.authorize("price.sync", required_role="price_sync_service")
        for value in (price_version, environment_id, source_type, source_reference):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise PriceCatalogError("invalid_price_record_identifier")
        start = _iso(_parse(effective_from))
        end = None if effective_to is None else _iso(_parse(effective_to))
        if end is not None and end <= start:
            raise PriceCatalogError("invalid_price_effective_range")
        if not records:
            raise PriceCatalogError("empty_price_catalog")
        captured = _iso(self.clock())
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in records:
            model_id = raw.get("model_id")
            if not isinstance(model_id, str) or not _IDENTIFIER.fullmatch(model_id):
                raise PriceCatalogError("invalid_price_record_identifier")
            if model_id in seen:
                raise PriceCatalogError("duplicate_price_record")
            seen.add(model_id)
            currency = raw.get("currency")
            if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
                raise PriceCatalogError("invalid_price_currency")
            status = str(raw.get("verification_status") or "pending_confirmation")
            if status not in {"confirmed", "pending_confirmation", "rejected"}:
                raise PriceCatalogError("invalid_price_verification_status")
            item = {
                "price_version": price_version, "environment_id": environment_id,
                "model_id": model_id, "requested_model": raw.get("requested_model"),
                "actual_model": raw.get("actual_model"),
                "billed_model": raw.get("billed_model"), "provider": raw.get("provider"),
                "currency": currency,
                "input_price_per_million_tokens": self._optional_decimal(
                    raw.get("input_price_per_million_tokens")),
                "cached_input_price_per_million_tokens": self._optional_decimal(
                    raw.get("cached_input_price_per_million_tokens")),
                "output_price_per_million_tokens": self._optional_decimal(
                    raw.get("output_price_per_million_tokens")),
                "per_request_price": self._optional_decimal(raw.get("per_request_price")),
                "pricing_unit": str(raw.get("pricing_unit") or "per_1m_tokens"),
                "effective_from": start, "effective_to": end,
                "source_type": source_type, "source_reference": source_reference,
                "captured_at": captured, "confidence": str(raw.get("confidence") or "unknown"),
                "verification_status": status,
                "raw_record_sha256": raw.get("raw_record_sha256"),
                "provider_price_version": raw.get("provider_price_version"),
                "supported_endpoints_json": json.dumps(
                    raw.get("supported_endpoints") or [], ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")),
            }
            if item["pricing_unit"] not in {"per_1m_tokens", "per_request", "tiered"}:
                raise PriceCatalogError("invalid_price_billing_unit")
            if status == "confirmed" and not any(item[key] is not None for key in (
                    "input_price_per_million_tokens", "output_price_per_million_tokens",
                    "per_request_price")):
                raise PriceCatalogError("confirmed_price_missing")
            item["checksum"] = _digest({k: v for k, v in item.items()
                                        if k not in {"captured_at", "checksum"}})
            normalized.append(item)
        normalized.sort(key=lambda row: row["model_id"])
        semantic = {"price_version": price_version, "environment_id": environment_id,
            "effective_from": start, "effective_to": end, "source_type": source_type,
            "source_reference": source_reference,
            "records": [{k: v for k, v in row.items() if k != "captured_at"}
                        for row in normalized]}
        checksum = _digest(semantic)
        confirmed = sum(row["verification_status"] == "confirmed" for row in normalized)
        with self._connection() as db:
            prior = db.execute("""SELECT checksum FROM model_price_catalog_versions
              WHERE price_version=? AND tenant_id=? AND workspace_id=?""",
              (price_version, *self.scope.sql_parameters())).fetchone()
            if prior:
                if prior["checksum"] != checksum:
                    raise PriceCatalogError("immutable_price_version_conflict")
                return {**self.model_catalog_status(price_version), "idempotent": True}
            db.execute("""INSERT INTO model_price_catalog_versions VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (price_version, environment_id,
              start, end, source_type, source_reference, captured, checksum,
              len(normalized), confirmed, len(normalized)-confirmed, captured,
              *self.scope.sql_parameters()))
            db.executemany("""INSERT INTO model_price_catalog_records(
              price_version,environment_id,model_id,requested_model,actual_model,
              billed_model,provider,currency,input_price_per_million_tokens,
              cached_input_price_per_million_tokens,output_price_per_million_tokens,
              per_request_price,pricing_unit,effective_from,effective_to,
              source_type,source_reference,captured_at,checksum,raw_record_sha256,
              provider_price_version,supported_endpoints_json,confidence,
              verification_status,tenant_id,workspace_id) VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [(
                row["price_version"], row["environment_id"], row["model_id"],
                row["requested_model"], row["actual_model"], row["billed_model"], row["provider"],
                row["currency"], row["input_price_per_million_tokens"],
                row["cached_input_price_per_million_tokens"],
                row["output_price_per_million_tokens"], row["per_request_price"],
                row["pricing_unit"], row["effective_from"], row["effective_to"],
                row["source_type"], row["source_reference"], row["captured_at"],
                row["checksum"], row["raw_record_sha256"],
                row["provider_price_version"], row["supported_endpoints_json"],
                row["confidence"], row["verification_status"],
                *self.scope.sql_parameters()) for row in normalized])
            self._audit(db, "model_price_catalog_persisted", source_type,
                        {"price_version": price_version, "record_count": len(normalized),
                         "checksum": checksum})
        return {**self.model_catalog_status(price_version), "idempotent": False}

    def model_catalog_status(self, price_version: str | None = None) -> dict[str, Any]:
        self.security.authorize("price.read")
        with self._connection() as db:
            if price_version:
                row = db.execute("""SELECT * FROM model_price_catalog_versions
                  WHERE price_version=? AND tenant_id=? AND workspace_id=?""",
                  (price_version, *self.scope.sql_parameters())).fetchone()
            else:
                row = db.execute("""SELECT * FROM model_price_catalog_versions
                  WHERE tenant_id=? AND workspace_id=? ORDER BY effective_from DESC,
                  created_at DESC LIMIT 1""", self.scope.sql_parameters()).fetchone()
        if not row:
            return {"status": "unavailable", "price_version": None,
                    "model_count": 0, "confirmed_count": 0, "pending_count": 0}
        return {"status": "ready", **dict(row)}

    def list_model_prices(self, price_version: str | None = None) -> list[dict[str, Any]]:
        status = self.model_catalog_status(price_version)
        if not status.get("price_version"):
            return []
        with self._connection() as db:
            rows = db.execute("""SELECT * FROM model_price_catalog_records
              WHERE price_version=? AND tenant_id=? AND workspace_id=?
              ORDER BY model_id""", (status["price_version"],
              *self.scope.sql_parameters())).fetchall()
        return [dict(row) for row in rows]

    def _audit(self, db: sqlite3.Connection, event: str, source_id: str,
               details: Mapping[str, Any]) -> None:
        safe = {key: value for key, value in details.items()
                if not any(term in str(key).casefold() for term in _SENSITIVE)}
        db.execute("""INSERT INTO price_sync_audit(
          audit_id,event_type,source_id,created_at,details_json,
          tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)""", (
            "PA-" + uuid.uuid4().hex.upper(), event, source_id,
            _iso(self.clock()), _canonical(safe), *self.scope.sql_parameters()))

    def plan(self, source_id: str, *, allow_network: bool = False) -> dict[str, Any]:
        self.security.authorize("price.sync", required_role="price_sync_service")
        source = self.policy["allowed_sources"].get(source_id)
        if source is None:
            return {"status": "blocked", "reason": "price_source_not_allowed",
                    "source_id": source_id, "network_called": False}
        if source["network_required"] and not allow_network:
            return {"status": "blocked", "reason": "price_network_disabled",
                    "source_id": source_id, "network_called": False}
        if self.transport is None:
            return {"status": "blocked", "reason": "price_transport_not_configured",
                    "source_id": source_id, "network_called": False}
        return {"status": "ready", "reason": None, "source_id": source_id,
                "adapter": source["adapter"],
                "timeout_seconds": float(self.policy["timeout_seconds"]),
                "maximum_attempts": int(self.policy["maximum_attempts"]),
                "network_called": False}

    def _normalize_record(self, raw: Any, source_id: str, fetched_at: str) -> dict[str, Any]:
        required = {
            "environment_id", "model_id", "channel_id", "currency",
            "input_unit_price", "output_unit_price", "billing_unit",
            "effective_from", "effective_until",
        }
        optional = {"cached_input_unit_price"}
        if (not isinstance(raw, dict) or not required.issubset(raw)
                or set(raw) - required - optional):
            raise PriceCatalogError("invalid_price_record_schema")
        for key in ("environment_id", "model_id", "channel_id"):
            if not isinstance(raw[key], str) or not _IDENTIFIER.fullmatch(raw[key]):
                raise PriceCatalogError("invalid_price_record_identifier")
        currency = raw["currency"]
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise PriceCatalogError("invalid_price_currency")
        if raw["billing_unit"] not in self.policy["allowed_billing_units"]:
            raise PriceCatalogError("invalid_price_billing_unit")
        prices: dict[str, str] = {}
        for key in ("input_unit_price", "output_unit_price"):
            value = raw[key]
            if not isinstance(value, str) or not _PRICE.fullmatch(value):
                raise PriceCatalogError("invalid_price_decimal")
            try:
                decimal = Decimal(value)
            except InvalidOperation as exc:
                raise PriceCatalogError("invalid_price_decimal") from exc
            if not decimal.is_finite() or decimal < 0:
                raise PriceCatalogError("invalid_price_decimal")
            prices[key] = format(decimal.normalize(), "f") if decimal else "0"
        cached_price = raw.get("cached_input_unit_price")
        if cached_price is not None:
            if not isinstance(cached_price, str) or not _PRICE.fullmatch(cached_price):
                raise PriceCatalogError("invalid_price_decimal")
            try:
                cached_decimal = Decimal(cached_price)
            except InvalidOperation as exc:
                raise PriceCatalogError("invalid_price_decimal") from exc
            if not cached_decimal.is_finite() or cached_decimal < 0:
                raise PriceCatalogError("invalid_price_decimal")
            prices["cached_input_unit_price"] = (
                format(cached_decimal.normalize(), "f") if cached_decimal else "0")
        else:
            prices["cached_input_unit_price"] = None
        effective_from = _iso(_parse(raw["effective_from"]))
        effective_until = None if raw["effective_until"] is None else _iso(_parse(raw["effective_until"]))
        if effective_until is not None and effective_until <= effective_from:
            raise PriceCatalogError("invalid_price_effective_range")
        normalized = {
            "environment_id": raw["environment_id"], "model_id": raw["model_id"],
            "channel_id": raw["channel_id"], "currency": currency,
            **prices, "billing_unit": raw["billing_unit"],
            "effective_from": effective_from, "effective_until": effective_until,
            "source_id": source_id, "fetched_at": fetched_at,
        }
        normalized["record_sha256"] = _digest({
            key: value for key, value in normalized.items() if key != "fetched_at"
        })
        return normalized

    def _normalize_payload(self, payload: Any, source_id: str, fetched_at: str) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "records"}:
            raise PriceCatalogError("invalid_price_catalog_schema")
        if payload["schema_version"] != self.policy["catalog_schema_version"]:
            raise PriceCatalogError("unknown_price_catalog_schema")
        if not isinstance(payload["records"], list) or not payload["records"]:
            raise PriceCatalogError("empty_price_catalog")
        if len(payload["records"]) > 10000:
            raise PriceCatalogError("price_catalog_too_large")
        records = [self._normalize_record(row, source_id, fetched_at) for row in payload["records"]]
        keys = [(row["environment_id"], row["model_id"], row["channel_id"], row["currency"])
                for row in records]
        if len(keys) != len(set(keys)):
            raise PriceCatalogError("duplicate_price_record")
        return sorted(records, key=lambda row: tuple(str(row[key]) for key in
            ("environment_id", "model_id", "channel_id", "currency")))

    def synchronize(self, source_id: str, *, allow_network: bool = False) -> dict[str, Any]:
        self.security.authorize("price.sync", required_role="price_sync_service")
        planned = self.plan(source_id, allow_network=allow_network)
        attempted_at = _iso(self.clock())
        if planned["status"] != "ready":
            with self._connection() as db:
                db.execute("""INSERT INTO price_sync_state(
                  source_id,last_catalog_version,last_attempted_at,last_succeeded_at,
                  last_status,last_error_code,tenant_id,workspace_id)
                  VALUES(?,NULL,?,NULL,'blocked',?,?,?)
                  ON CONFLICT(tenant_id,workspace_id,source_id) DO UPDATE SET
                  last_attempted_at=excluded.last_attempted_at,
                  last_status='blocked',last_error_code=excluded.last_error_code""",
                  (source_id, attempted_at, planned["reason"],
                   *self.scope.sql_parameters()))
                self._audit(db, "price_sync_blocked", source_id, {"code": planned["reason"]})
            return {**planned, "catalog": self.catalog_status(source_id)}
        source = self.policy["allowed_sources"][source_id]
        network_called = False
        error_code: str | None = None
        transport_result: Mapping[str, Any] | None = None
        for _attempt in range(int(self.policy["maximum_attempts"])):
            try:
                transport_result = self.transport(source, float(self.policy["timeout_seconds"]))  # type: ignore[misc]
                if not isinstance(transport_result, Mapping):
                    raise PriceCatalogError("invalid_price_transport_result")
                network_called = network_called or bool(transport_result.get("network_called", False))
                records = self._normalize_payload(transport_result.get("payload"), source_id, attempted_at)
                break
            except PriceCatalogError as exc:
                error_code = str(exc)
                records = None
                break
            except Exception:
                error_code = "price_transport_failed"
                records = None
        if records is None:
            code = error_code or "price_transport_failed"
            with self._connection() as db:
                db.execute("""INSERT INTO price_sync_state(
                  source_id,last_catalog_version,last_attempted_at,last_succeeded_at,
                  last_status,last_error_code,tenant_id,workspace_id)
                  VALUES(?,NULL,?,NULL,'failed',?,?,?)
                  ON CONFLICT(tenant_id,workspace_id,source_id) DO UPDATE SET
                  last_attempted_at=excluded.last_attempted_at,
                  last_status='failed',last_error_code=excluded.last_error_code""",
                  (source_id, attempted_at, code, *self.scope.sql_parameters()))
                self._audit(db, "price_sync_failed", source_id, {"code": code,
                    "network_called": network_called})
            return {"status": "failed", "reason": code, "source_id": source_id,
                    "network_called": network_called, "catalog": self.catalog_status(source_id)}
        payload = {"schema_version": self.policy["catalog_schema_version"], "records": records}
        semantic_payload = {"schema_version": self.policy["catalog_schema_version"],
            "source_id": source_id, "records": [{
                key: value for key, value in row.items()
                if key not in {"fetched_at", "record_sha256"}
            } for row in records]}
        payload_sha256 = _digest(semantic_payload)
        catalog_version = "PC-" + payload_sha256[:24]
        with self._connection() as db:
            prior = db.execute("""SELECT 1 FROM price_catalog_versions
              WHERE catalog_version=? AND tenant_id=? AND workspace_id=?""",
              (catalog_version, *self.scope.sql_parameters())).fetchone()
            if not prior:
                db.execute("""INSERT INTO price_catalog_versions(
                  catalog_version,schema_version,policy_version,source_id,fetched_at,
                  payload_sha256,record_count,created_at,payload_json,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    catalog_version, self.policy["catalog_schema_version"],
                    self.policy["policy_version"], source_id, attempted_at,
                    payload_sha256, len(records), attempted_at, _canonical(payload),
                    *self.scope.sql_parameters()))
                db.executemany("""INSERT INTO price_catalog_records(
                  catalog_version,environment_id,model_id,channel_id,currency,
                  input_unit_price,cached_input_unit_price,output_unit_price,billing_unit,effective_from,
                  effective_until,source_id,fetched_at,record_sha256,
                  tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [(
                    catalog_version, row["environment_id"], row["model_id"],
                    row["channel_id"], row["currency"], row["input_unit_price"],
                    row["cached_input_unit_price"], row["output_unit_price"], row["billing_unit"],
                    row["effective_from"], row["effective_until"], source_id,
                    attempted_at, row["record_sha256"],
                    *self.scope.sql_parameters()) for row in records])
            db.execute("""INSERT INTO price_sync_state(
              source_id,last_catalog_version,last_attempted_at,last_succeeded_at,
              last_status,last_error_code,tenant_id,workspace_id)
              VALUES(?,?,?,?,'ready',NULL,?,?)
              ON CONFLICT(tenant_id,workspace_id,source_id) DO UPDATE SET
              last_catalog_version=excluded.last_catalog_version,
              last_attempted_at=excluded.last_attempted_at,last_succeeded_at=excluded.last_succeeded_at,
              last_status='ready',last_error_code=NULL""",
              (source_id, catalog_version, attempted_at, attempted_at,
               *self.scope.sql_parameters()))
            self._audit(db, "price_catalog_unchanged" if prior else "price_catalog_persisted",
                        source_id, {"catalog_version": catalog_version,
                        "record_count": len(records), "network_called": network_called})
        return {"status": "ready", "source_id": source_id,
                "catalog_version": catalog_version, "record_count": len(records),
                "idempotent": bool(prior), "network_called": network_called}

    def _freshness(self, fetched_at: str, effective_until: str | None,
                   effective_from: str | None = None) -> str:
        now = self.clock().astimezone(timezone.utc)
        if effective_from is not None and now < _parse(effective_from):
            return "not_yet_effective"
        age = max(0.0, (now - _parse(fetched_at)).total_seconds())
        if effective_until is not None and now >= _parse(effective_until):
            return "expired"
        if age > int(self.policy["expire_after_seconds"]):
            return "expired"
        if age > int(self.policy["stale_after_seconds"]):
            return "stale"
        return "fresh"

    def resolve(self, *, environment_id: str, model_id: str, channel_id: str,
                currency: str) -> dict[str, Any]:
        self.security.authorize("price.read")
        with self._connection() as db:
            row = db.execute("""SELECT r.* FROM price_catalog_records r
              JOIN price_sync_state s ON s.last_catalog_version=r.catalog_version
               AND s.tenant_id=r.tenant_id AND s.workspace_id=r.workspace_id
              WHERE r.tenant_id=? AND r.workspace_id=? AND
                r.environment_id=? AND r.model_id=? AND r.channel_id=? AND r.currency=?
              ORDER BY r.effective_from DESC LIMIT 1""",
              (*self.scope.sql_parameters(), environment_id, model_id,
               channel_id, currency)).fetchone()
        if row is None:
            return {"status": "unavailable", "eligible": False,
                    "reason": "price_unavailable", "environment_id": environment_id,
                    "model_id": model_id, "channel_id": channel_id,
                    "currency": currency, "network_called": False}
        item = dict(row)
        freshness = self._freshness(
            item["fetched_at"], item["effective_until"], item["effective_from"])
        return {**item, "status": freshness, "eligible": freshness == "fresh",
                "reason": None if freshness == "fresh" else f"price_{freshness}",
                "network_called": False}

    def catalog_status(self, source_id: str) -> dict[str, Any]:
        self.security.authorize("price.read")
        with self._connection() as db:
            state = db.execute("""SELECT * FROM price_sync_state WHERE source_id=?
              AND tenant_id=? AND workspace_id=?""",
              (source_id, *self.scope.sql_parameters())).fetchone()
            if state and state["last_catalog_version"]:
                version = db.execute("""SELECT * FROM price_catalog_versions
                  WHERE catalog_version=? AND tenant_id=? AND workspace_id=?""",
                  (state["last_catalog_version"],
                   *self.scope.sql_parameters())).fetchone()
            else:
                version = None
        if version is None:
            return {"status": "unavailable", "source_id": source_id,
                    "catalog_version": None, "record_count": 0,
                    "last_status": state["last_status"] if state else "never_run",
                    "last_error_code": state["last_error_code"] if state else None,
                    "network_called": False}
        freshness = self._freshness(version["fetched_at"], None)
        return {"status": freshness, "source_id": source_id,
                "catalog_version": version["catalog_version"],
                "record_count": version["record_count"],
                "fetched_at": version["fetched_at"],
                "last_status": state["last_status"],
                "last_error_code": state["last_error_code"],
                "network_called": False}

    def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            self.security.authorize("audit.read")
        except AuthorizationError:
            # Price Sync can inspect its own scoped synchronization audit for
            # safe retry diagnostics; it cannot query any other audit domain.
            self.security.authorize("price.read", required_role="price_sync_service")
        if not 1 <= int(limit) <= 500:
            raise PriceCatalogError("invalid_price_audit_limit")
        with self._connection() as db:
            rows = db.execute("""SELECT audit_id,event_type,source_id,created_at,details_json
              FROM price_sync_audit WHERE tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC,audit_id DESC LIMIT ?""",
              (*self.scope.sql_parameters(), int(limit))).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]
