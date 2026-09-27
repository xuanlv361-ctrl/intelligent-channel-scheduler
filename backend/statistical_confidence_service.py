"""Persistence and read API for versioned statistical-confidence snapshots."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.statistical_confidence import CONFIDENCE_VERSION, load_confidence_policy


class StatisticalConfidenceService:
    def __init__(self, path: Path, *, security: Any):
        self.path = Path(path)
        if security is None:
            raise ValueError("enterprise_service_identity_required")
        self.security = security
        self.scope = security.scope
        self.policy = load_confidence_policy()
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS statistical_confidence_snapshots(
                  confidence_snapshot_id TEXT NOT NULL,
                  metric_snapshot_id TEXT NOT NULL,
                  confidence_version TEXT NOT NULL,
                  policy_version TEXT NOT NULL,
                  calculated_at TEXT NOT NULL,
                  confidence_state TEXT NOT NULL,
                  confidence_label TEXT NOT NULL,
                  input_fingerprint TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,confidence_snapshot_id),
                  UNIQUE(tenant_id,workspace_id,metric_snapshot_id,confidence_version)
                );
                CREATE INDEX IF NOT EXISTS ix_statistical_confidence_metric
                  ON statistical_confidence_snapshots(
                    tenant_id,workspace_id,metric_snapshot_id,
                    confidence_version,calculated_at);
                CREATE TABLE IF NOT EXISTS statistical_confidence_state(
                  confidence_version TEXT NOT NULL,
                  policy_version TEXT NOT NULL,
                  last_calculated_at TEXT,
                  source_snapshot_count INTEGER NOT NULL DEFAULT 0,
                  last_status TEXT NOT NULL,
                  last_error_code TEXT,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,confidence_version)
                );
                CREATE TABLE IF NOT EXISTS statistical_confidence_audit(
                  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  confidence_version TEXT NOT NULL,
                  policy_version TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  details_json TEXT NOT NULL,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL
                );
                """
            )
            db.execute(
                """INSERT OR IGNORE INTO statistical_confidence_state(
                     confidence_version,policy_version,last_status,
                     tenant_id,workspace_id)
                   VALUES(?,?,'never_run',?,?)""",
                (
                    CONFIDENCE_VERSION,
                    self.policy["policy_version"],
                    *self.scope.sql_parameters(),
                ),
            )

    def list_snapshots(
        self,
        *,
        metric_snapshot_id: str | None = None,
        confidence_version: str = CONFIDENCE_VERSION,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.security.authorize("snapshot.read")
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("invalid_confidence_pagination")
        clauses = ["confidence_version=?", "tenant_id=?", "workspace_id=?"]
        params: list[Any] = [confidence_version, *self.scope.sql_parameters()]
        if metric_snapshot_id:
            clauses.append("metric_snapshot_id=?")
            params.append(metric_snapshot_id)
        where = " AND ".join(clauses)
        with self.connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM statistical_confidence_snapshots WHERE {where}",
                params,
            ).fetchone()[0]
            rows = db.execute(
                f"""SELECT * FROM statistical_confidence_snapshots
                    WHERE {where}
                    ORDER BY calculated_at DESC,metric_snapshot_id
                    LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
            state = db.execute(
                """SELECT * FROM statistical_confidence_state
                   WHERE confidence_version=? AND tenant_id=? AND workspace_id=?""",
                (confidence_version, *self.scope.sql_parameters()),
            ).fetchone()
        items = []
        for row in rows:
            result = json.loads(row["result_json"])
            result["confidence_snapshot_id"] = row["confidence_snapshot_id"]
            result["calculated_at"] = row["calculated_at"]
            items.append(result)
        return {
            "status": "ready" if items else "unknown",
            "confidence_version": confidence_version,
            "policy_version": self.policy["policy_version"],
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "items": items,
            "runtime": dict(state) if state else None,
            "network_called": False,
            "source_contract": "normalized_local_metric_evidence_projection",
        }

    def mark_refresh(
        self,
        db: sqlite3.Connection,
        *,
        calculated_at: datetime,
        snapshot_count: int,
    ) -> None:
        self.security.authorize("snapshot.generate", required_role="metrics_service")
        timestamp = calculated_at.astimezone(timezone.utc).isoformat()
        db.execute(
            """UPDATE statistical_confidence_state
               SET policy_version=?,last_calculated_at=?,
                   source_snapshot_count=?,last_status='ready',
                   last_error_code=NULL
               WHERE confidence_version=? AND tenant_id=? AND workspace_id=?""",
            (
                self.policy["policy_version"],
                timestamp,
                snapshot_count,
                CONFIDENCE_VERSION,
                *self.scope.sql_parameters(),
            ),
        )
        db.execute(
            """INSERT INTO statistical_confidence_audit(
                 confidence_version,policy_version,event_type,created_at,details_json,
                 tenant_id,workspace_id)
               VALUES(?,?,?,?,?,?,?)""",
            (
                CONFIDENCE_VERSION,
                self.policy["policy_version"],
                "confidence_snapshots_refreshed",
                timestamp,
                json.dumps({"snapshot_count": snapshot_count}, sort_keys=True),
                *self.scope.sql_parameters(),
            ),
        )
