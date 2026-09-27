"""Reviewed runtime output-capability evidence for the six domestic UAT models."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.tenant_security import TenantScope


MODEL_IDS = (
    "deepseek-v4-flash", "glm-5.2", "claude-sonnet-5", "gpt-5.6-terra",
    "kimi-k2.7-code", "doubao-seed-2-0-mini-260215",
)
EVIDENCE_TYPES = {
    "uat_authoritative_capability", "uat_models_explicit_field",
    "versioned_channel_metadata", "reviewed_configuration",
    "historical_observed_lower_bound",
}
CONFIDENCE_STATES = {"confirmed", "pending_confirmation"}


class UatOutputCapabilityError(ValueError):
    pass


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise UatOutputCapabilityError("capability_time_timezone_required")
    return value.astimezone(timezone.utc).isoformat()


class UatOutputCapabilityService:
    """Append-only reviewed claims; resolution rejects stale or conflicting claims."""

    def __init__(self, path: Path, scope: TenantScope | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.path = path
        self.scope = scope or TenantScope.local_development()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._init()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS uat_output_capability_evidence(
              evidence_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL, model_id TEXT NOT NULL,
              provider TEXT NOT NULL, channel_id TEXT NOT NULL,
              max_context_tokens INTEGER, max_input_tokens INTEGER,
              max_output_tokens INTEGER, streaming_supported INTEGER,
              multimodal_capabilities_json TEXT NOT NULL,
              observed_output_lower_bound INTEGER,
              evidence_source TEXT NOT NULL, evidence_type TEXT NOT NULL,
              evidence_version TEXT NOT NULL, observed_at TEXT NOT NULL,
              expires_at TEXT NOT NULL, confidence_status TEXT NOT NULL,
              reviewed_by TEXT NOT NULL, review_timestamp TEXT NOT NULL,
              superseded_at TEXT, superseded_by TEXT,
              PRIMARY KEY(tenant_id,workspace_id,evidence_id)
            );
            CREATE INDEX IF NOT EXISTS ix_uat_output_capability_resolution
              ON uat_output_capability_evidence(
                tenant_id,workspace_id,model_id,channel_id,expires_at,
                superseded_at,review_timestamp DESC);
            CREATE TABLE IF NOT EXISTS uat_output_capability_audit(
              audit_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL, evidence_id TEXT,
              event_type TEXT NOT NULL, created_at TEXT NOT NULL,
              details_json TEXT NOT NULL
            );
            """)

    def _audit(self, db, evidence_id: str | None, event: str,
               details: dict[str, Any]) -> None:
        db.execute("INSERT INTO uat_output_capability_audit VALUES(?,?,?,?,?,?,?)", (
            f"UOC-A-{uuid.uuid4().hex.upper()}", *self.scope.sql_parameters(),
            evidence_id, event, _iso(self.clock()),
            json.dumps(details, sort_keys=True, ensure_ascii=False)))

    @staticmethod
    def _positive_optional(body: dict[str, Any], name: str) -> int | None:
        value = body.get(name)
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise UatOutputCapabilityError(f"invalid_{name}")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise UatOutputCapabilityError(f"invalid_{name}") from exc
        if parsed <= 0:
            raise UatOutputCapabilityError(f"invalid_{name}")
        return parsed

    def review(self, body: dict[str, Any], *, reviewed_by: str) -> dict[str, Any]:
        model = str(body.get("model_id") or "").strip()
        channel = str(body.get("channel_id") or "").strip()
        provider = str(body.get("provider") or "").strip()
        source = str(body.get("evidence_source") or "").strip()
        evidence_type = str(body.get("evidence_type") or "").strip()
        version = str(body.get("evidence_version") or "").strip()
        confidence = str(body.get("confidence_status") or "pending_confirmation").strip()
        if model not in MODEL_IDS:
            raise UatOutputCapabilityError("model_not_in_six_model_scope")
        if not channel or len(channel) > 128:
            raise UatOutputCapabilityError("channel_id_required")
        if not provider or not source or not version or any(
                len(value) > 256 for value in (provider, source, version)):
            raise UatOutputCapabilityError("capability_provenance_required")
        if evidence_type not in EVIDENCE_TYPES:
            raise UatOutputCapabilityError("capability_evidence_type_not_allowed")
        if confidence not in CONFIDENCE_STATES:
            raise UatOutputCapabilityError("invalid_confidence_status")
        if not reviewed_by:
            raise UatOutputCapabilityError("reviewer_identity_required")
        try:
            observed = datetime.fromisoformat(str(body.get("observed_at")))
            expires = datetime.fromisoformat(str(body.get("expires_at")))
        except ValueError as exc:
            raise UatOutputCapabilityError("invalid_capability_time") from exc
        observed_text, expires_text = _iso(observed), _iso(expires)
        now = self.clock().astimezone(timezone.utc)
        if observed.astimezone(timezone.utc) > now or expires.astimezone(timezone.utc) <= now:
            raise UatOutputCapabilityError("capability_evidence_not_current")
        limits = {name: self._positive_optional(body, name) for name in (
            "max_context_tokens", "max_input_tokens", "max_output_tokens")}
        lower_bound = self._positive_optional(body, "observed_output_lower_bound")
        if evidence_type == "historical_observed_lower_bound" and (
                lower_bound is None or any(value is not None for value in limits.values())):
            raise UatOutputCapabilityError("historical_evidence_only_proves_lower_bound")
        if confidence == "confirmed" and evidence_type != "historical_observed_lower_bound" and any(
                limits[name] is None for name in ("max_context_tokens", "max_output_tokens")):
            raise UatOutputCapabilityError("confirmed_capability_limits_required")
        streaming = body.get("streaming_supported")
        if streaming is not None and not isinstance(streaming, bool):
            raise UatOutputCapabilityError("invalid_streaming_supported")
        multimodal = body.get("multimodal_capabilities") or []
        if not isinstance(multimodal, list) or any(
                not isinstance(item, str) or not item.strip() for item in multimodal):
            raise UatOutputCapabilityError("invalid_multimodal_capabilities")
        evidence_id = f"UOC-{uuid.uuid4().hex[:24].upper()}"
        reviewed_at = _iso(now)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if body.get("supersede_existing") is True:
                rows = db.execute("""SELECT evidence_id FROM uat_output_capability_evidence
                  WHERE tenant_id=? AND workspace_id=? AND model_id=? AND channel_id=?
                    AND superseded_at IS NULL""", (*self.scope.sql_parameters(), model, channel)).fetchall()
                for row in rows:
                    db.execute("""UPDATE uat_output_capability_evidence
                      SET superseded_at=?,superseded_by=? WHERE tenant_id=? AND workspace_id=?
                      AND evidence_id=?""", (reviewed_at, evidence_id,
                      *self.scope.sql_parameters(), row["evidence_id"]))
                    self._audit(db, row["evidence_id"], "capability_superseded",
                                {"superseded_by": evidence_id})
            db.execute("""INSERT INTO uat_output_capability_evidence VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)""", (
              evidence_id, *self.scope.sql_parameters(), model, provider, channel,
              limits["max_context_tokens"], limits["max_input_tokens"],
              limits["max_output_tokens"], None if streaming is None else int(streaming),
              json.dumps(sorted(set(multimodal)), ensure_ascii=False), lower_bound,
              source, evidence_type, version, observed_text, expires_text, confidence,
              reviewed_by, reviewed_at))
            self._audit(db, evidence_id, "capability_reviewed", {
                "model_id": model, "channel_id": channel,
                "evidence_type": evidence_type, "evidence_version": version,
                "confidence_status": confidence})
            db.commit()
        return self.get(evidence_id)

    def get(self, evidence_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("""SELECT * FROM uat_output_capability_evidence
              WHERE tenant_id=? AND workspace_id=? AND evidence_id=?""",
              (*self.scope.sql_parameters(), evidence_id)).fetchone()
        if not row:
            raise UatOutputCapabilityError("capability_evidence_not_found")
        return self._public(row)

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["streaming_supported"] = (None if result["streaming_supported"] is None
                                          else bool(result["streaming_supported"]))
        result["multimodal_capabilities"] = json.loads(
            result.pop("multimodal_capabilities_json"))
        result.pop("tenant_id", None)
        result.pop("workspace_id", None)
        return result

    def resolve(self, model_id: str, channel_id: str) -> dict[str, Any]:
        if model_id not in MODEL_IDS:
            return self._pending(model_id, channel_id, "model_not_in_six_model_scope")
        now = _iso(self.clock())
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM uat_output_capability_evidence
              WHERE tenant_id=? AND workspace_id=? AND model_id=? AND channel_id=?
                AND superseded_at IS NULL AND expires_at>? ORDER BY review_timestamp DESC""",
              (*self.scope.sql_parameters(), model_id, channel_id, now)).fetchall()
        confirmed = [row for row in rows if row["confidence_status"] == "confirmed"
                     and row["evidence_type"] != "historical_observed_lower_bound"]
        if not confirmed:
            return self._pending(model_id, channel_id, "capability_pending_confirmation", rows)
        signatures = {(row["max_context_tokens"], row["max_input_tokens"],
                       row["max_output_tokens"], row["streaming_supported"])
                      for row in confirmed}
        if len(signatures) != 1:
            return self._pending(model_id, channel_id, "capability_evidence_conflict", confirmed)
        item = self._public(confirmed[0])
        item.update({
            "status": "confirmed", "confirmed_max_output_tokens": item["max_output_tokens"],
            "confirmed_channel_max_output_tokens": item["max_output_tokens"],
            "fresh_until": item["expires_at"],
        })
        return item

    def _pending(self, model_id: str, channel_id: str, reason: str,
                 rows: list[sqlite3.Row] | None = None) -> dict[str, Any]:
        return {
            "model_id": model_id, "provider": None, "channel_id": channel_id,
            "max_context_tokens": None, "max_input_tokens": None,
            "max_output_tokens": None, "confirmed_max_output_tokens": None,
            "confirmed_channel_max_output_tokens": None,
            "streaming_supported": None, "multimodal_capabilities": [],
            "evidence_source": "pending_confirmation", "evidence_type": None,
            "evidence_version": None, "observed_at": None, "expires_at": None,
            "fresh_until": None, "confidence_status": "pending_confirmation",
            "reviewed_by": None, "review_timestamp": None, "status": reason,
            "evidence_count": len(rows or []),
        }

    def catalog(self, channel_id: str) -> dict[str, Any]:
        return {model: self.resolve(model, channel_id) for model in MODEL_IDS}

    def audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("""SELECT audit_id,evidence_id,event_type,created_at,details_json
              FROM uat_output_capability_audit WHERE tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC LIMIT ?""", (*self.scope.sql_parameters(), limit)).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]
