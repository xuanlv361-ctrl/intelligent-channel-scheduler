"""Idempotent production import path for reconciled historical UAT evidence."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.incremental_metrics_service import bind_enterprise_service_security
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext

class HistoricalEvidenceImportError(ValueError):
    """Stable import failure without including source-row content."""


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class HistoricalEvidenceImportService:
    """Persist an immutable reconciliation batch for MetricsEvidenceAdapter.

    Raw CSV rows never reach this boundary.  Callers may provide only the safe
    metric projection produced by the reviewed reconciliation adapter.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        principal: PrincipalContext | None = None,
        authorization: AuthorizationService | None = None,
        development_mode: bool = False,
    ):
        self.database_path = Path(database_path)
        self.security = bind_enterprise_service_security(
            principal=principal,
            authorization=authorization,
            development_mode=development_mode,
            development_role="collector_service",
        )
        self.scope = self.security.scope
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS import_batches(
                  batch_id TEXT NOT NULL,
                  sha256 TEXT,
                  source_type TEXT,
                  created_at TEXT,
                  payload TEXT,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,batch_id)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                  event_id TEXT NOT NULL,
                  event_type TEXT,
                  created_at TEXT,
                  details TEXT,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,event_id)
                );
                CREATE INDEX IF NOT EXISTS ix_import_batches_scope_created
                  ON import_batches(tenant_id,workspace_id,created_at,batch_id);
                CREATE INDEX IF NOT EXISTS ix_audit_log_scope_created
                  ON audit_log(tenant_id,workspace_id,created_at,event_id);
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def import_reconciliation(
        self,
        events: list[dict[str, Any]],
        *,
        evidence_manifest_sha256: str,
        reconciliation_result_sha256: str,
        imported_at: datetime,
    ) -> dict[str, Any]:
        self.security.authorize("evidence.import", required_role="collector_service")
        if len(events) != 60:
            raise HistoricalEvidenceImportError(
                "historical_reconciliation_not_complete")
        if len({item.get("evidence_id") for item in events}) != len(events):
            raise HistoricalEvidenceImportError(
                "historical_evidence_identity_not_unique")
        normalized_rows = []
        for index, event in enumerate(events, 1):
            serialized = _canonical(event).casefold()
            if any(token in serialized for token in (
                    "api key", "api_key", "authorization", "cookie", '"ip"')):
                raise HistoricalEvidenceImportError(
                    "historical_projection_contains_sensitive_field")
            normalized_rows.append({
                **event,
                "_row_number": index,
                "_classification": "valid",
                "_issues": [],
            })
        source_material = {
            "evidence_manifest_sha256": evidence_manifest_sha256,
            "reconciliation_result_sha256": reconciliation_result_sha256,
            "normalized_rows": normalized_rows,
        }
        source_sha = hashlib.sha256(
            _canonical(source_material).encode("utf-8")).hexdigest()
        batch_id = f"IMP-HIST-{source_sha[:20].upper()}"
        created_at = imported_at.astimezone(timezone.utc).isoformat()
        payload = {
            "batch_id": batch_id,
            "source_sha256": source_sha,
            "source_type": "measured_uat_historical_reconciled",
            "environment_id": "china_uat",
            "created_at": created_at,
            "imported_at": created_at,
            "imported_row_count": len(normalized_rows),
            "audit_status": "immutable_audited",
            "provenance": {
                "schema_id": "domestic_uat_billing_csv_v1",
                "evidence_manifest_sha256": evidence_manifest_sha256,
                "reconciliation_result_sha256": reconciliation_result_sha256,
                "raw_sensitive_fields_persisted": False,
            },
            "normalized_rows": normalized_rows,
            "normalized_preview": normalized_rows[:100],
        }
        encoded = _canonical(payload)
        event_id = f"AUDIT-{hashlib.sha256(batch_id.encode()).hexdigest()[:24]}"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT sha256,payload FROM import_batches
                   WHERE batch_id=? AND tenant_id=? AND workspace_id=?""",
                (batch_id, *self.scope.sql_parameters()),
            ).fetchone()
            if current:
                if current["sha256"] != source_sha or current["payload"] != encoded:
                    raise HistoricalEvidenceImportError(
                        "historical_import_identity_conflict")
                return {
                    "batch_id": batch_id,
                    "inserted_count": 0,
                    "duplicate_count": len(normalized_rows),
                    "idempotent": True,
                    "source_sha256": source_sha,
                }
            db.execute(
                """INSERT INTO import_batches(
                     batch_id,sha256,source_type,created_at,payload,
                     tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)""",
                (
                    batch_id,
                    source_sha,
                    payload["source_type"],
                    created_at,
                    encoded,
                    *self.scope.sql_parameters(),
                ),
            )
            db.execute(
                """INSERT INTO audit_log(
                     event_id,event_type,created_at,details,tenant_id,workspace_id)
                   VALUES(?,?,?,?,?,?)""",
                (
                    event_id,
                    "historical_reconciliation_import_confirmed",
                    created_at,
                    _canonical({
                        "batch_id": batch_id,
                        "source_sha256": source_sha,
                        "evidence_manifest_sha256":
                            evidence_manifest_sha256,
                        "record_count": len(normalized_rows),
                    }),
                    *self.scope.sql_parameters(),
                ),
            )
        return {
            "batch_id": batch_id,
            "inserted_count": len(normalized_rows),
            "duplicate_count": 0,
            "idempotent": False,
            "source_sha256": source_sha,
        }
