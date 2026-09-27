"""Persisted model-capability and model-mapping projections.

The projection is deliberately conservative: model identifiers never imply a
capability.  Official catalog declarations and active explicit evidence can
confirm a capability, while successful canonical business calls can only mark
text/stream execution as observed.  Channel identifiers and request IDs are
not required for model-level statistics.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping

from backend.tenant_security import TenantScope


CAPABILITIES = (
    "text_input", "text_output", "image_understanding", "image_generation",
    "audio_input", "audio_output", "video_input", "video_generation",
    "streaming", "non_streaming", "tool_calling",
)
PUBLIC_CAPABILITIES = tuple(name for name in CAPABILITIES if name != "non_streaming")
ENVIRONMENT_ALIASES = {
    "uat": "china_uat", "china-uat": "china_uat", "china_uat": "china_uat",
    "国内uat": "china_uat", "国内_uat": "china_uat", "国内UAT": "china_uat",
}
SUCCESS_STATES = {"success", "succeeded", "completed", "complete", "ok"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _environment(value: Any) -> str:
    text = str(value or "china_uat").strip()
    return ENVIRONMENT_ALIASES.get(text, ENVIRONMENT_ALIASES.get(text.casefold(), text))


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Legacy CSV imports were normalized from China time before insertion in
    # the canonical ledger.  Remaining naive values are persisted UTC values.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3)


def _json(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _catalog_items(catalog: dict[str, Any] | list[Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    metadata: dict[str, Any] = {}
    if isinstance(catalog, list):
        raw_items = catalog
    elif isinstance(catalog, dict):
        metadata = {key: catalog.get(key) for key in (
            "status", "source_type", "source_reference", "updated_at", "captured_at",
            "catalog_version",
        ) if catalog.get(key) is not None}
        raw_items = catalog.get("data") or catalog.get("models") or catalog.get("items")
        if raw_items is None:
            raw_items = [dict(value, model_id=key) if isinstance(value, dict) else {
                "model_id": key, "value": value,
            } for key, value in catalog.items() if isinstance(value, dict)]
    else:
        raise TypeError("catalog_must_be_dict_or_list")
    if isinstance(raw_items, dict):
        raw_items = [dict(value, model_id=key) if isinstance(value, dict) else {
            "model_id": key, "value": value,
        } for key, value in raw_items.items()]
    items: list[dict[str, Any]] = []
    for raw in raw_items or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        model_id = str(item.get("model_id") or item.get("id") or "").strip()
        if not model_id:
            continue
        item["model_id"] = model_id
        items.append(item)
    items.sort(key=lambda value: value["model_id"])
    return items, metadata


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [str(key) for key, enabled in value.items() if enabled]
    if isinstance(value, Iterable):
        return sorted({str(item) for item in value if item is not None})
    return []


def _capability_name(value: str) -> str | None:
    aliases = {
        "text": "text_input", "text_input": "text_input", "text-output": "text_output",
        "text_output": "text_output", "image": "image_understanding",
        "image_input": "image_understanding", "image_in": "image_understanding",
        "vision": "image_understanding", "image_output": "image_generation",
        "image_out": "image_generation", "image_generation": "image_generation",
        "audio_input": "audio_input", "audio_in": "audio_input",
        "audio_output": "audio_output", "audio_out": "audio_output",
        "video_input": "video_input", "video_in": "video_input",
        "video_output": "video_generation", "video_out": "video_generation",
        "video_generation": "video_generation", "stream": "streaming",
        "streaming": "streaming", "non_streaming": "non_streaming",
        "tool": "tool_calling", "tools": "tool_calling", "tool_calling": "tool_calling",
    }
    return aliases.get(str(value).strip().casefold().replace(" ", "_"))


def _catalog_capabilities(item: Mapping[str, Any]) -> dict[str, str]:
    """Read only explicit declarations; tags/names/endpoints are not evidence."""
    result: dict[str, str] = {}
    declared = item.get("capabilities") or item.get("capability")
    if isinstance(declared, Mapping):
        for key, value in declared.items():
            name = _capability_name(str(key))
            if not name:
                continue
            normalized = str(value).casefold() if not isinstance(value, bool) else (
                "supported" if value else "unsupported")
            if normalized in {"supported", "confirmed", "true", "1"}:
                result[name] = "confirmed"
            elif normalized in {"unsupported", "false", "0"}:
                result[name] = "unsupported"
    elif isinstance(declared, (list, tuple, set)):
        for value in declared:
            name = _capability_name(str(value))
            if name:
                result[name] = "confirmed"
    for direction, target in (("input_modalities", "input"), ("output_modalities", "output")):
        for modality in _string_list(item.get(direction)):
            modality = modality.casefold()
            name = {
                ("input", "text"): "text_input", ("output", "text"): "text_output",
                ("input", "image"): "image_understanding", ("output", "image"): "image_generation",
                ("input", "audio"): "audio_input", ("output", "audio"): "audio_output",
                ("input", "video"): "video_input", ("output", "video"): "video_generation",
            }.get((target, modality))
            if name:
                result[name] = "confirmed"
    for field, capability in (("supports_streaming", "streaming"), ("supports_tools", "tool_calling"),
                              ("tool_calling", "tool_calling")):
        if isinstance(item.get(field), bool):
            result[capability] = "confirmed" if item[field] else "unsupported"
    return result


class ModelCapabilityAggregationService:
    """Build a tenant-scoped, idempotent projection for API and UI consumers."""

    schema_version = "model_capability_aggregation_v3"

    def __init__(self, db_path: str | Path, scope: TenantScope, *,
                 acceptance_evidence_root: str | Path | None = None):
        self.path = Path(db_path)
        self.scope = scope
        self.acceptance_evidence_root = (
            Path(acceptance_evidence_root) if acceptance_evidence_root else None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS model_catalog_snapshots(
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,environment_id TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,checksum TEXT NOT NULL,model_count INTEGER NOT NULL,
              source_type TEXT NOT NULL,source_reference TEXT,captured_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,snapshot_id),
              UNIQUE(tenant_id,workspace_id,environment_id,checksum));
            CREATE TABLE IF NOT EXISTS model_catalog_entries(
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,environment_id TEXT NOT NULL,
              snapshot_id TEXT NOT NULL,model_id TEXT NOT NULL,display_name TEXT,provider TEXT,
              endpoints_json TEXT NOT NULL,tags_json TEXT NOT NULL,context_limit INTEGER,
              pricing_type TEXT,catalog_updated_at TEXT,capabilities_json TEXT NOT NULL,
              details_json TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,snapshot_id,model_id));
            CREATE TABLE IF NOT EXISTS model_capability_aggregation_jobs(
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,environment_id TEXT NOT NULL,
              job_id TEXT NOT NULL,input_checksum TEXT NOT NULL,status TEXT NOT NULL,
              started_at TEXT NOT NULL,finished_at TEXT,source_watermark TEXT,
              catalog_snapshot_id TEXT,model_count INTEGER NOT NULL DEFAULT 0,
              mapping_count INTEGER NOT NULL DEFAULT 0,error_code TEXT,
              PRIMARY KEY(tenant_id,workspace_id,job_id),
              UNIQUE(tenant_id,workspace_id,environment_id,input_checksum));
            CREATE TABLE IF NOT EXISTS model_capability_aggregation_state(
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,environment_id TEXT NOT NULL,
              current_job_id TEXT,current_catalog_snapshot_id TEXT,input_checksum TEXT,
              source_watermark TEXT,last_sync_at TEXT,last_aggregated_at TEXT,
              status TEXT NOT NULL,last_error TEXT,
              PRIMARY KEY(tenant_id,workspace_id,environment_id));
            CREATE TABLE IF NOT EXISTS model_capability_aggregates(
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,environment_id TEXT NOT NULL,
              model_id TEXT NOT NULL,display_name TEXT,provider TEXT,endpoints_json TEXT NOT NULL,
              tags_json TEXT NOT NULL,context_limit INTEGER,pricing_type TEXT,catalog_updated_at TEXT,
              capabilities_json TEXT NOT NULL,evidence_sources_json TEXT NOT NULL,
              call_count INTEGER NOT NULL,success_count INTEGER NOT NULL,failure_count INTEGER NOT NULL,
              stream_count INTEGER NOT NULL,non_stream_count INTEGER NOT NULL,
              p50_latency_ms REAL,p95_latency_ms REAL,input_tokens INTEGER NOT NULL,
              cached_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,cost_amount TEXT,
              currency TEXT,latest_call_at TEXT,latest_request_id TEXT,overall_status TEXT NOT NULL,
              evidence_count INTEGER NOT NULL,aggregation_job_id TEXT NOT NULL,updated_at TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,environment_id,model_id));
            CREATE TABLE IF NOT EXISTS model_mapping_aggregates(
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,environment_id TEXT NOT NULL,
              mapping_id TEXT NOT NULL,requested_model TEXT NOT NULL,billed_model TEXT,
              actual_model TEXT,call_count INTEGER NOT NULL,consistent_count INTEGER NOT NULL,
              mismatch_count INTEGER NOT NULL,missing_actual_count INTEGER NOT NULL,
              evidence_level TEXT NOT NULL,source_type TEXT NOT NULL,latest_observed_at TEXT,
              aggregation_job_id TEXT NOT NULL,updated_at TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,environment_id,mapping_id));
            CREATE INDEX IF NOT EXISTS ix_capability_models_scope
              ON model_capability_aggregates(tenant_id,workspace_id,environment_id,model_id);
            CREATE INDEX IF NOT EXISTS ix_model_mappings_scope
              ON model_mapping_aggregates(tenant_id,workspace_id,environment_id,requested_model);
            """)

    def _table_exists(self, db: sqlite3.Connection, name: str) -> bool:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _store_catalog(self, db: sqlite3.Connection, catalog: dict[str, Any] | list[Any],
                       environment_id: str, timestamp: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        items, metadata = _catalog_items(catalog)
        normalized = []
        for item in items:
            normalized.append({
                "model_id": item["model_id"],
                "display_name": item.get("name") or item.get("display_name") or item["model_id"],
                "provider": item.get("provider") or item.get("owned_by"),
                "endpoints": _string_list(item.get("endpoints") or item.get("supported_endpoints") or item.get("endpoint")),
                "tags": _string_list(item.get("tags")),
                "context_limit": item.get("context_limit") or item.get("context_window") or item.get("max_context_tokens"),
                "pricing_type": item.get("pricing_type") or item.get("billing_type"),
                "catalog_updated_at": item.get("updated_at") or metadata.get("updated_at"),
                "capabilities": _catalog_capabilities(item),
                "details": {key: item.get(key) for key in (
                    "input_modalities", "output_modalities", "supports_streaming", "supports_tools",
                    "mime_types", "max_output_tokens",
                ) if item.get(key) is not None},
            })
        checksum = _digest(normalized)
        snapshot_id = f"MCS-{checksum[:24]}"
        db.execute("""INSERT OR IGNORE INTO model_catalog_snapshots(
          tenant_id,workspace_id,environment_id,snapshot_id,checksum,model_count,source_type,
          source_reference,captured_at,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
          (*self.scope.sql_parameters(), environment_id, snapshot_id, checksum, len(normalized),
           str(metadata.get("source_type") or "uat_model_catalog"), metadata.get("source_reference"),
           str(metadata.get("captured_at") or timestamp), _canonical(metadata)))
        for item in normalized:
            db.execute("""INSERT OR IGNORE INTO model_catalog_entries(
              tenant_id,workspace_id,environment_id,snapshot_id,model_id,display_name,provider,
              endpoints_json,tags_json,context_limit,pricing_type,catalog_updated_at,
              capabilities_json,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (*self.scope.sql_parameters(), environment_id, snapshot_id, item["model_id"],
               item["display_name"], item["provider"], _canonical(item["endpoints"]),
               _canonical(item["tags"]), item["context_limit"], item["pricing_type"],
               item["catalog_updated_at"], _canonical(item["capabilities"]), _canonical(item["details"])))
        return snapshot_id, normalized, metadata

    def _latest_catalog(self, db: sqlite3.Connection, environment_id: str) -> dict[str, Any]:
        snapshot = db.execute("""SELECT * FROM model_catalog_snapshots WHERE tenant_id=?
          AND workspace_id=? AND environment_id=? ORDER BY captured_at DESC LIMIT 1""",
          (*self.scope.sql_parameters(), environment_id)).fetchone()
        if not snapshot:
            return {"models": [], "source_type": "persisted_catalog_unavailable"}
        rows = db.execute("""SELECT * FROM model_catalog_entries WHERE tenant_id=?
          AND workspace_id=? AND environment_id=? AND snapshot_id=? ORDER BY model_id""",
          (*self.scope.sql_parameters(), environment_id, snapshot["snapshot_id"])).fetchall()
        return {"models": [{"id": row["model_id"], "name": row["display_name"],
            "provider": row["provider"], "endpoints": _json(row["endpoints_json"], []),
            "tags": _json(row["tags_json"], []), "context_limit": row["context_limit"],
            "pricing_type": row["pricing_type"], "updated_at": row["catalog_updated_at"],
            **_json(row["details_json"], {})} for row in rows],
            "source_type": "persisted_uat_model_catalog",
            "captured_at": snapshot["captured_at"],
            "source_reference": snapshot["snapshot_id"]}

    def _call_rows(self, db: sqlite3.Connection, environment_id: str) -> list[dict[str, Any]]:
        if not self._table_exists(db, "standardized_call_logs"):
            return []
        columns = {row[1] for row in db.execute("PRAGMA table_info(standardized_call_logs)")}
        select = [name for name in (
            "record_id", "occurred_at", "request_id", "response_id", "provider_log_id",
            "environment_id", "requested_model",
            "billed_model", "actual_model", "provider", "stream", "request_status", "http_status",
            "endpoint_type",
            "error_category", "total_latency_ms", "first_token_latency_ms", "input_tokens",
            "cached_input_tokens", "output_tokens", "cost_amount", "currency", "source_type",
            "is_historical", "evidence_level", "duplicate_of", "duplicate_status", "traffic_class",
            "is_fault_injected", "error_source",
        ) if name in columns]
        if not select:
            return []
        rows = db.execute(f"SELECT {','.join(select)} FROM standardized_call_logs WHERE tenant_id=? AND workspace_id=?",
                          self.scope.sql_parameters()).fetchall()
        result = []
        for raw in rows:
            row = dict(raw)
            if _environment(row.get("environment_id")) != environment_id:
                continue
            if row.get("duplicate_of") is not None or row.get("duplicate_status") in {"exact_duplicate", "duplicate"}:
                continue
            if bool(row.get("is_fault_injected")) or row.get("error_source") == "uat_fault_injection":
                continue
            if str(row.get("traffic_class") or "business").casefold() == "probe":
                continue
            if str(row.get("requested_model") or "").casefold() == "non_model_uat_http":
                continue
            result.append(row)
        return result

    def _evidence_rows(self, db: sqlite3.Connection, environment_id: str) -> list[dict[str, Any]]:
        if not self._table_exists(db, "capability_evidence"):
            return self._acceptance_evidence_rows(environment_id)
        columns = {row[1] for row in db.execute("PRAGMA table_info(capability_evidence)")}
        wanted = [name for name in (
            "evidence_id", "evidence_type", "observed_at", "environment_id", "subject_id",
            "source", "requirement", "state", "details_json", "valid_until", "revoked_at",
            "maximum_context_tokens",
        ) if name in columns]
        rows = db.execute(f"SELECT {','.join(wanted)} FROM capability_evidence WHERE tenant_id=? AND workspace_id=?",
                          self.scope.sql_parameters()).fetchall()
        result = [dict(row) for row in rows if _environment(row["environment_id"]) == environment_id
                  and not ("revoked_at" in row.keys() and row["revoked_at"])]
        result.extend(self._acceptance_evidence_rows(environment_id))
        return result

    def _acceptance_evidence_rows(self, environment_id: str) -> list[dict[str, Any]]:
        """Read immutable live acceptance artifacts as evidence, never as demo data."""
        root = self.acceptance_evidence_root
        if root is None or not root.is_dir() or environment_id != "china_uat":
            return []
        evidence: list[dict[str, Any]] = []
        for path in sorted(root.glob("**/multimodal_live.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            image = payload.get("image") or {}
            if image.get("execution_status") == "success" and image.get("actual_model"):
                evidence.append({"evidence_id": f"FILE-{_digest([str(path), 'image'])[:24]}",
                    "evidence_type": "live_measured", "observed_at": image.get("created_at") or payload.get("started_at"),
                    "environment_id": environment_id, "subject_id": image["actual_model"],
                    "source": "real_execution", "requirement": "image_in", "state": "supported",
                    "details_json": _canonical({"source_reference": str(path),
                        "request_id": image.get("request_id")}), "valid_until": None, "revoked_at": None})
        for path in sorted(root.glob("**/multimodal_quota_retry.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            audio = payload.get("audio") or {}
            if audio.get("requested_model") and int(audio.get("http_status") or 0) == 503:
                file_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                evidence.append({"evidence_id": f"FILE-{_digest([str(path), 'audio'])[:24]}",
                    "evidence_type": "live_measured", "observed_at": audio.get("created_at") or file_time,
                    "environment_id": environment_id, "subject_id": audio["requested_model"],
                    "source": "real_execution", "requirement": "audio", "state": "unknown",
                    "details_json": _canonical({"directions": ["input"],
                        "reason": "channel_unavailable", "source_reference": str(path)}),
                    "valid_until": None, "revoked_at": None})
        for path in sorted(root.glob("**/video_task_poll.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            completed = next((row for row in reversed(payload.get("polls") or [])
                              if row.get("task_status") == "completed" and int(row.get("http_status") or 0) == 200), None)
            if completed:
                file_time = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                evidence.append({"evidence_id": f"FILE-{_digest([str(path), 'video'])[:24]}",
                    "evidence_type": "live_measured", "observed_at": completed.get("created_at") or file_time,
                    "environment_id": environment_id,
                    "subject_id": "dreamina-seedance-2-0-fast-260128", "source": "real_execution",
                    "requirement": "video_generation", "state": "supported",
                    "details_json": _canonical({"source_reference": str(path),
                        "request_id": completed.get("request_id"), "task_id": payload.get("task_id")}),
                    "valid_until": None, "revoked_at": None})
        return evidence

    @staticmethod
    def _successful(row: Mapping[str, Any]) -> bool:
        status = str(row.get("request_status") or "").casefold()
        http_status = row.get("http_status")
        return status in SUCCESS_STATES or (isinstance(http_status, int) and 200 <= http_status < 300)

    @staticmethod
    def _evidence_capabilities(row: Mapping[str, Any]) -> list[str]:
        requirement = str(row.get("requirement") or "").casefold()
        details = _json(row.get("details_json"), {})
        if requirement == "text":
            return ["text_input", "text_output"]
        if requirement == "stream":
            return ["streaming"]
        if requirement == "image_in":
            return ["image_understanding"]
        if requirement == "image_out":
            return ["image_generation"]
        if requirement == "video_in":
            return ["video_input"]
        if requirement == "tool":
            return ["tool_calling"]
        if requirement == "audio":
            directions = set(_string_list(details.get("directions") or details.get("direction")))
            values = []
            if directions & {"input", "in", "audio_input"}:
                values.append("audio_input")
            if directions & {"output", "out", "audio_output"}:
                values.append("audio_output")
            return values
        if requirement in {"video_out", "video_generation"}:
            return ["video_generation"]
        output_modalities = {str(value).casefold() for value in _string_list(details.get("output_modalities"))}
        input_modalities = {str(value).casefold() for value in _string_list(details.get("input_modalities"))}
        result = []
        if "audio" in input_modalities: result.append("audio_input")
        if "audio" in output_modalities: result.append("audio_output")
        if "video" in input_modalities: result.append("video_input")
        if "video" in output_modalities: result.append("video_generation")
        return result

    def ensure(self, catalog: dict[str, Any] | list[Any] | None,
               environment_id: str = "china_uat") -> dict[str, Any]:
        environment_id = _environment(environment_id)
        timestamp = _now()
        with self._lock, self.connect() as db:
            if catalog is None:
                catalog = self._latest_catalog(db, environment_id)
            snapshot_id, catalog_items, _ = self._store_catalog(db, catalog, environment_id, timestamp)
            calls = self._call_rows(db, environment_id)
            evidence = self._evidence_rows(db, environment_id)
            input_checksum = _digest({
                "schema": self.schema_version, "catalog": snapshot_id,
                "calls": sorted((_canonical(row) for row in calls)),
                "evidence": sorted((_canonical(row) for row in evidence)),
            })
            existing = db.execute("""SELECT * FROM model_capability_aggregation_jobs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=? AND input_checksum=?""",
              (*self.scope.sql_parameters(), environment_id, input_checksum)).fetchone()
            if existing and existing["status"] == "ready":
                return {**dict(existing), "already_current": True, "update_status": "ready"}
            job_id = f"MCAJ-{input_checksum[:24]}"
            db.execute("""INSERT OR REPLACE INTO model_capability_aggregation_jobs(
              tenant_id,workspace_id,environment_id,job_id,input_checksum,status,started_at,
              catalog_snapshot_id,model_count,mapping_count) VALUES(?,?,?,?,?,'running',?,?,0,0)""",
              (*self.scope.sql_parameters(), environment_id, job_id, input_checksum, timestamp, snapshot_id))

            catalog_by_model = {item["model_id"]: item for item in catalog_items}
            model_ids = set(catalog_by_model)
            for row in calls:
                model_ids.update(str(row[field]).strip() for field in (
                    "requested_model", "billed_model", "actual_model") if row.get(field))
            model_ids.update(str(row["subject_id"]).strip() for row in evidence if row.get("subject_id"))
            calls_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in calls:
                # Model statistics use actual when explicitly captured, otherwise requested.
                model_id = str(row.get("actual_model") or row.get("requested_model") or "").strip()
                if model_id:
                    calls_by_model[model_id].append(row)
            evidence_by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in evidence:
                evidence_by_model[str(row["subject_id"])].append(row)

            aggregates: list[dict[str, Any]] = []
            for model_id in sorted(model_ids):
                item = catalog_by_model.get(model_id, {"model_id": model_id, "display_name": model_id,
                    "provider": None, "endpoints": [], "tags": [], "context_limit": None,
                    "pricing_type": None, "catalog_updated_at": None, "capabilities": {}})
                statuses = {name: "pending" for name in CAPABILITIES}
                sources: dict[str, set[str]] = {name: set() for name in CAPABILITIES}
                reasons: dict[str, set[str]] = {name: set() for name in CAPABILITIES}
                conflicts: set[str] = set()
                for name, state in item.get("capabilities", {}).items():
                    if name in statuses:
                        statuses[name] = state
                        sources[name].add("official_catalog")
                for row in evidence_by_model.get(model_id, []):
                    state = ("confirmed" if row.get("state") == "supported"
                             and row.get("source") in {"manual", "official_catalog", "live"} else
                             "observed" if row.get("state") == "supported" else (
                        "unsupported" if row.get("state") == "unsupported" else "pending"))
                    for name in self._evidence_capabilities(row):
                        if name not in statuses:
                            continue
                        if statuses[name] not in {"pending", state}:
                            conflicts.add(name)
                        if state == "confirmed" or statuses[name] == "pending":
                            statuses[name] = state
                        sources[name].add("manual_review" if row.get("source") == "manual" else
                                          "realtime_execution" if row.get("source") in {"live", "real_execution"} else
                                          "official_catalog" if row.get("source") == "official_catalog" else
                                          "explicit_evidence")
                        reason = row.get("reason")
                        if not reason and row.get("details_json"):
                            try:
                                reason = json.loads(str(row["details_json"])).get("reason")
                            except (json.JSONDecodeError, TypeError, AttributeError):
                                reason = None
                        if reason:
                            reasons[name].add(str(reason))
                model_calls = calls_by_model.get(model_id, [])
                successful = [row for row in model_calls if self._successful(row)]
                for row in successful:
                    observed = ["text_input", "text_output", "streaming" if bool(row.get("stream")) else "non_streaming"]
                    for name in observed:
                        if statuses[name] == "pending":
                            statuses[name] = "observed"
                        elif statuses[name] == "unsupported":
                            conflicts.add(name)
                            statuses[name] = "observed"
                        sources[name].add("historical_uat_log" if row.get("source_type") == "historical_uat_csv" else "realtime_execution")
                latencies = [float(row["total_latency_ms"]) for row in model_calls if row.get("total_latency_ms") is not None]
                costs = Decimal("0"); cost_seen = False; currency = None
                for row in model_calls:
                    if row.get("cost_amount") is not None:
                        try:
                            costs += Decimal(str(row["cost_amount"])); cost_seen = True
                        except InvalidOperation:
                            pass
                    currency = currency or row.get("currency")
                capability_payload = {name: {"status": statuses[name], "sources": sorted(sources[name]),
                                             "conflict": name in conflicts,
                                             "reasons": sorted(reasons[name])}
                                      for name in CAPABILITIES}
                overall = "confirmed" if "confirmed" in statuses.values() else (
                    "observed" if "observed" in statuses.values() else (
                    "unsupported" if set(statuses.values()) <= {"pending", "unsupported"}
                    and "unsupported" in statuses.values() else "pending"))
                parsed_times = [parsed for row in model_calls
                                if (parsed := _parse_time(row.get("occurred_at"))) is not None]
                latest = max(parsed_times, default=None)
                latest_row = max(model_calls, key=lambda row: _parse_time(row.get("occurred_at")) or datetime.min.replace(tzinfo=timezone.utc)) if model_calls else None
                source_names = sorted(({source for values in sources.values() for source in values}
                                       | ({"official_catalog"} if model_id in catalog_by_model else set())))
                aggregates.append({
                    "model_id": model_id, "item": item, "capabilities": capability_payload,
                    "sources": source_names, "call_count": len(model_calls), "success_count": len(successful),
                    "failure_count": len(model_calls) - len(successful),
                    "stream_count": sum(1 for row in model_calls if bool(row.get("stream"))),
                    "non_stream_count": sum(1 for row in model_calls if not bool(row.get("stream"))),
                    "p50": _percentile(latencies, .5), "p95": _percentile(latencies, .95),
                    "input": sum(int(row.get("input_tokens") or 0) for row in model_calls),
                    "cached": sum(int(row.get("cached_input_tokens") or 0) for row in model_calls),
                    "output": sum(int(row.get("output_tokens") or 0) for row in model_calls),
                    "cost": str(costs) if cost_seen else None, "currency": currency,
                    "latest": latest.isoformat() if latest else None,
                    "latest_request_id": latest_row.get("request_id") if latest_row else None,
                    "overall": overall, "evidence_count": len(evidence_by_model.get(model_id, [])),
                })

            mapping_groups: dict[tuple[str, str | None, str | None], list[dict[str, Any]]] = defaultdict(list)
            for row in calls:
                requested = str(row.get("requested_model") or "").strip()
                if not requested:
                    continue
                billed = str(row["billed_model"]).strip() if row.get("billed_model") else None
                actual = str(row["actual_model"]).strip() if row.get("actual_model") else None
                mapping_groups[(requested, billed, actual)].append(row)
            mappings = []
            for key, rows in sorted(mapping_groups.items(), key=lambda value: tuple(x or "" for x in value[0])):
                requested, billed, actual = key
                mapping_id = "MM-" + _digest({"environment": environment_id, "key": key})[:24]
                source_types = {str(row.get("source_type") or "unknown") for row in rows}
                has_realtime = any(source != "historical_uat_csv" for source in source_types)
                explicit_complete = billed is not None and actual is not None
                has_execution_link = any(row.get("request_id") and actual is not None for row in rows)
                evidence_level = "exact_mapping" if explicit_complete and has_realtime and has_execution_link else (
                    "realtime_linked" if has_execution_link else (
                    "historical_observation" if explicit_complete and not has_realtime else "pending"))
                explicit_triplet = billed is not None and actual is not None
                consistent = int(explicit_triplet and requested == billed == actual)
                parsed_times = [parsed for row in rows
                                if (parsed := _parse_time(row.get("occurred_at"))) is not None]
                latest = max(parsed_times, default=None)
                mappings.append({
                    "mapping_id": mapping_id, "requested_model": requested, "billed_model": billed,
                    "actual_model": actual, "call_count": len(rows),
                    "consistent_count": len(rows) if consistent else 0,
                    "mismatch_count": len(rows) if explicit_triplet and not consistent else 0,
                    "missing_actual_count": len(rows) if actual is None else 0,
                    "evidence_level": evidence_level,
                    "source_type": "realtime_execution" if has_realtime else "historical_uat_csv",
                    "latest": latest.isoformat() if latest else None,
                })

            db.execute("DELETE FROM model_capability_aggregates WHERE tenant_id=? AND workspace_id=? AND environment_id=?",
                       (*self.scope.sql_parameters(), environment_id))
            db.execute("DELETE FROM model_mapping_aggregates WHERE tenant_id=? AND workspace_id=? AND environment_id=?",
                       (*self.scope.sql_parameters(), environment_id))
            for row in aggregates:
                item = row["item"]
                db.execute("""INSERT INTO model_capability_aggregates VALUES(
                  ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (*self.scope.sql_parameters(), environment_id, row["model_id"], item.get("display_name"),
                   item.get("provider"), _canonical(item.get("endpoints", [])), _canonical(item.get("tags", [])),
                   item.get("context_limit"), item.get("pricing_type"), item.get("catalog_updated_at"),
                   _canonical(row["capabilities"]), _canonical(row["sources"]), row["call_count"],
                   row["success_count"], row["failure_count"], row["stream_count"], row["non_stream_count"],
                   row["p50"], row["p95"], row["input"], row["cached"], row["output"], row["cost"],
                   row["currency"], row["latest"], row["latest_request_id"], row["overall"],
                   row["evidence_count"], job_id, timestamp))
            for row in mappings:
                db.execute("""INSERT INTO model_mapping_aggregates VALUES(
                  ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (*self.scope.sql_parameters(), environment_id, row["mapping_id"], row["requested_model"],
                   row["billed_model"], row["actual_model"], row["call_count"], row["consistent_count"],
                   row["mismatch_count"], row["missing_actual_count"], row["evidence_level"],
                   row["source_type"], row["latest"], job_id, timestamp))
            watermark = max((_parse_time(row.get("occurred_at")) for row in calls), default=None)
            watermark_text = watermark.isoformat() if watermark else None
            db.execute("""UPDATE model_capability_aggregation_jobs SET status='ready',finished_at=?,
              source_watermark=?,model_count=?,mapping_count=? WHERE tenant_id=? AND workspace_id=? AND job_id=?""",
              (timestamp, watermark_text, len(aggregates), len(mappings), *self.scope.sql_parameters(), job_id))
            db.execute("""INSERT INTO model_capability_aggregation_state(
              tenant_id,workspace_id,environment_id,current_job_id,current_catalog_snapshot_id,
              input_checksum,source_watermark,last_sync_at,last_aggregated_at,status,last_error)
              VALUES(?,?,?,?,?,?,?,?,?,'ready',NULL)
              ON CONFLICT(tenant_id,workspace_id,environment_id) DO UPDATE SET
              current_job_id=excluded.current_job_id,current_catalog_snapshot_id=excluded.current_catalog_snapshot_id,
              input_checksum=excluded.input_checksum,source_watermark=excluded.source_watermark,
              last_sync_at=excluded.last_sync_at,last_aggregated_at=excluded.last_aggregated_at,
              status='ready',last_error=NULL""",
              (*self.scope.sql_parameters(), environment_id, job_id, snapshot_id, input_checksum,
               watermark_text, timestamp, timestamp))
            return {"job_id": job_id, "status": "ready", "update_status": "ready",
                    "already_current": False, "environment_id": environment_id,
                    "catalog_snapshot_id": snapshot_id, "model_count": len(aggregates),
                    "mapping_count": len(mappings), "source_watermark": watermark_text,
                    "started_at": timestamp, "finished_at": timestamp}

    def _model_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["endpoints"] = _json(item.pop("endpoints_json"), [])
        item["tags"] = _json(item.pop("tags_json"), [])
        item["capabilities"] = _json(item.pop("capabilities_json"), {})
        item["evidence_sources"] = _json(item.pop("evidence_sources_json"), [])
        item["success_rate"] = round(item["success_count"] / item["call_count"] * 100, 3) if item["call_count"] else None
        item["total_tokens"] = item["input_tokens"] + item["cached_tokens"] + item["output_tokens"]
        return item

    def models(self, environment_id: str = "china_uat", *, search: str | None = None,
               provider: str | None = None, capability: str | None = None,
               status: str | None = None, has_calls: bool | None = None,
               page: int = 1, page_size: int = 100, sort_by: str = "model_id",
               sort_order: str = "asc") -> dict[str, Any]:
        environment_id = _environment(environment_id)
        allowed_sort = {"model_id", "provider", "call_count", "success_count", "p95_latency_ms", "latest_call_at", "overall_status"}
        sort_by = sort_by if sort_by in allowed_sort else "model_id"
        direction = "DESC" if sort_order.casefold() == "desc" else "ASC"
        clauses = ["tenant_id=?", "workspace_id=?", "environment_id=?"]
        params: list[Any] = [*self.scope.sql_parameters(), environment_id]
        if search:
            clauses.append("(model_id LIKE ? OR display_name LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if provider:
            clauses.append("provider=?"); params.append(provider)
        if status:
            clauses.append("overall_status=?"); params.append(status)
        if has_calls is True:
            clauses.append("call_count>0")
        elif has_calls is False:
            clauses.append("call_count=0")
        with self.connect() as db:
            rows = [self._model_dict(row) for row in db.execute(
                f"SELECT * FROM model_capability_aggregates WHERE {' AND '.join(clauses)} ORDER BY {sort_by} {direction},model_id LIMIT ? OFFSET ?",
                (*params, max(1, min(page_size, 500)), max(0, page - 1) * max(1, min(page_size, 500))))]
            if capability:
                rows = [row for row in rows if row["capabilities"].get(capability, {}).get("status") != "pending"]
            total = db.execute(f"SELECT COUNT(*) FROM model_capability_aggregates WHERE {' AND '.join(clauses)}", params).fetchone()[0]
        return {"environment_id": environment_id, "items": rows, "total": int(total),
                "page": page, "page_size": page_size}

    def model_detail(self, model_id: str, environment_id: str = "china_uat") -> dict[str, Any]:
        environment_id = _environment(environment_id)
        with self.connect() as db:
            row = db.execute("""SELECT * FROM model_capability_aggregates WHERE tenant_id=?
              AND workspace_id=? AND environment_id=? AND model_id=?""",
              (*self.scope.sql_parameters(), environment_id, model_id)).fetchone()
        if not row:
            return {"status": "not_found", "environment_id": environment_id, "model_id": model_id}
        return {"status": "ready", **self._model_dict(row),
                "technical_metadata": {"aggregation_job_id": row["aggregation_job_id"],
                                       "schema_version": self.schema_version}}

    def mappings(self, environment_id: str = "china_uat") -> dict[str, Any]:
        environment_id = _environment(environment_id)
        with self.connect() as db:
            items = [dict(row) for row in db.execute("""SELECT * FROM model_mapping_aggregates
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              ORDER BY call_count DESC,requested_model,billed_model,actual_model""",
              (*self.scope.sql_parameters(), environment_id))]
        return {"environment_id": environment_id, "items": items, "total": len(items),
                "exact_count": sum(1 for row in items if row["evidence_level"] == "exact_mapping"),
                "realtime_linked_count": sum(1 for row in items if row["evidence_level"] == "realtime_linked"),
                "historical_count": sum(1 for row in items if row["evidence_level"] == "historical_observation"),
                "pending_count": sum(1 for row in items if row["evidence_level"] == "pending")}

    def status(self, environment_id: str = "china_uat") -> dict[str, Any]:
        environment_id = _environment(environment_id)
        with self.connect() as db:
            row = db.execute("""SELECT * FROM model_capability_aggregation_state WHERE tenant_id=?
              AND workspace_id=? AND environment_id=?""",
              (*self.scope.sql_parameters(), environment_id)).fetchone()
        return dict(row) if row else {"environment_id": environment_id, "status": "not_started",
                                      "update_status": "not_started"}

    def overview(self, environment_id: str = "china_uat") -> dict[str, Any]:
        environment_id = _environment(environment_id)
        model_result = self.models(environment_id, page_size=500)
        models = model_result["items"]
        mapping_result = self.mappings(environment_id)
        state = self.status(environment_id)
        now = datetime.now(timezone.utc)
        with self.connect() as db:
            historical = realtime = recent = evidence_count = 0
            if self._table_exists(db, "standardized_call_logs"):
                rows = self._call_rows(db, environment_id)
                historical = sum(1 for row in rows if row.get("source_type") == "historical_uat_csv")
                realtime = sum(1 for row in rows if row.get("source_type") != "historical_uat_csv")
                recent = sum(1 for row in rows if (parsed := _parse_time(row.get("occurred_at")))
                             and (now - parsed).total_seconds() <= 86400)
            if self._table_exists(db, "capability_evidence"):
                evidence_count = len(self._evidence_rows(db, environment_id))
            snapshot = db.execute("""SELECT * FROM model_catalog_snapshots WHERE tenant_id=?
              AND workspace_id=? AND environment_id=? ORDER BY captured_at DESC LIMIT 1""",
              (*self.scope.sql_parameters(), environment_id)).fetchone()
            snapshot_metadata = _json(snapshot["metadata_json"], {}) if snapshot else {}
        coverage = {name: {state_name: 0 for state_name in ("confirmed", "observed", "pending", "unsupported")}
                    for name in PUBLIC_CAPABILITIES}
        source_distribution: dict[str, int] = defaultdict(int)
        for model in models:
            for name in PUBLIC_CAPABILITIES:
                coverage[name][model["capabilities"][name]["status"]] += 1
            if not model["evidence_sources"]:
                source_distribution["no_evidence"] += 1
            else:
                for source in model["evidence_sources"]:
                    source_distribution[source] += 1
        counts = {status: sum(1 for model in models if model["overall_status"] == status)
                  for status in ("confirmed", "observed", "pending", "unsupported")}
        return {
            "status": state.get("status", "not_started"),
            "update_status": state.get("status", "not_started"),
            "environment_id": environment_id,
            "catalog_status": str(snapshot_metadata.get("status") or "ready") if snapshot else "not_loaded",
            "catalog_model_count": int(snapshot["model_count"]) if snapshot else 0,
            "call_evidence_model_count": sum(1 for model in models if model["call_count"] > 0),
            "confirmed_model_count": counts["confirmed"], "observed_model_count": counts["observed"],
            "pending_model_count": counts["pending"], "unsupported_model_count": counts["unsupported"],
            "mapping_count": mapping_result["total"], "recent_24h_calls": recent,
            "historical_log_count": historical, "realtime_log_count": realtime,
            "evidence_count": evidence_count, "last_sync_at": state.get("last_sync_at"),
            "last_aggregated_at": state.get("last_aggregated_at"), "models": models,
            "capability_coverage": coverage, "evidence_sources": dict(sorted(source_distribution.items())),
            "technical_metadata": {"schema_version": self.schema_version,
                "catalog_snapshot_id": state.get("current_catalog_snapshot_id"),
                "aggregation_job_id": state.get("current_job_id"),
                "source_watermark": state.get("source_watermark"),
                "canonical_only": True, "probe_included": False, "fault_injection_included": False},
        }


__all__ = ["CAPABILITIES", "PUBLIC_CAPABILITIES", "ModelCapabilityAggregationService"]
