"""Snapshot-only Scheduler adapter for durable incremental metrics."""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.incremental_metrics_service import bind_enterprise_service_security
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext

class IncrementalMetricProvider:
    """Overlay candidates from a materialized metric snapshot.

    The provider never reads raw evidence.  Missing/stale/blocked snapshots do
    not silently fall back to fixed health values when this provider is enabled.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        window: str = "1h",
        confidence_version: str | None = None,
        principal: PrincipalContext | None = None,
        authorization: AuthorizationService | None = None,
        development_mode: bool = False,
    ):
        if window not in {"5m", "1h", "24h"}:
            raise ValueError("invalid_metric_window")
        self.database_path = Path(database_path)
        self.window = window
        self.confidence_version = confidence_version
        self.security = bind_enterprise_service_security(
            principal=principal,
            authorization=authorization,
            development_mode=development_mode,
            development_role="scheduler_service",
        )
        self.scope = self.security.scope

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.database_path}?mode=ro", uri=True, timeout=10
        )
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _timestamp(value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S")

    def apply(
        self,
        candidates: list[dict[str, str]],
        *,
        environment_id: str,
        requested_model: str,
        stream: bool,
        request_profile_id: str,
    ) -> tuple[list[dict[str, str]], dict[str, Any]]:
        self.security.authorize("snapshot.read", required_role="scheduler_service")
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM metric_snapshots
                   WHERE tenant_id=? AND workspace_id=? AND
                     window_name=? AND environment_id=? AND model=?
                     AND stream=? AND request_profile_id=?
                   ORDER BY generated_at DESC,sample_count DESC,snapshot_id""",
                (
                    *self.scope.sql_parameters(),
                    self.window,
                    environment_id,
                    requested_model,
                    int(stream),
                    request_profile_id,
                ),
            ).fetchall()
            confidence_rows = (
                db.execute(
                    """SELECT metric_snapshot_id,confidence_snapshot_id,
                              confidence_version,policy_version,confidence_state,
                              confidence_label,result_json
                       FROM statistical_confidence_snapshots
                       WHERE confidence_version=? AND tenant_id=? AND workspace_id=?""",
                    (self.confidence_version, *self.scope.sql_parameters()),
                ).fetchall()
                if self.confidence_version
                else []
            )
        by_channel: dict[str, sqlite3.Row] = {}
        for row in rows:
            channel = str(row["channel"])
            if channel and channel not in by_channel:
                by_channel[channel] = row
        confidence_by_metric: dict[str, dict[str, Any]] = {}
        if self.confidence_version:
            confidence_by_metric = {
                str(item["metric_snapshot_id"]): {
                    **dict(item),
                    "result": json.loads(item["result_json"]),
                }
                for item in confidence_rows
            }
        output = deepcopy(candidates)
        applied = blocked = unavailable = 0
        snapshot_ids: list[str] = []
        confidence_snapshot_ids: list[str] = []
        for candidate in output:
            row = by_channel.get(str(candidate["channel_id"]))
            candidate["dynamic_metrics_window"] = self.window
            if row is None:
                candidate["availability_status"] = "unavailable"
                candidate["dynamic_metrics_state"] = "unknown"
                candidate["dynamic_metrics_block_reason"] = (
                    "dynamic_metric_snapshot_unavailable"
                )
                unavailable += 1
                continue
            metrics = json.loads(row["metrics_json"])
            candidate["dynamic_metrics_state"] = str(row["data_state"])
            candidate["dynamic_metric_snapshot_id"] = str(row["snapshot_id"])
            snapshot_ids.append(str(row["snapshot_id"]))
            if self.confidence_version:
                confidence = confidence_by_metric.get(str(row["snapshot_id"]))
                if confidence is None:
                    candidate["availability_status"] = "unavailable"
                    candidate["statistical_confidence_state"] = "unknown"
                    candidate["confidence_block_reason"] = (
                        "statistical_confidence_snapshot_unavailable"
                    )
                    blocked += 1
                    continue
                result = confidence["result"]
                candidate.update(
                    {
                        "statistical_confidence_state": confidence[
                            "confidence_state"
                        ],
                        "statistical_confidence_label": confidence[
                            "confidence_label"
                        ],
                        "statistical_confidence_version": confidence[
                            "confidence_version"
                        ],
                        "confidence_policy_version": confidence["policy_version"],
                        "statistical_confidence_snapshot_id": confidence[
                            "confidence_snapshot_id"
                        ],
                        "confidence_effective_sample_size": str(
                            result["effective_sample_size"]
                        ),
                        "confidence_interval_lower": str(result["interval_lower"]),
                        "confidence_interval_upper": str(result["interval_upper"]),
                        "confidence_adjusted_success_score": str(
                            result["adjusted_success_score"]
                        ),
                        "confidence_adjustment_reason_codes": [
                            "weighted_source_reliability",
                            "freshness_decay",
                            "missing_field_completeness",
                            "wilson_lower_bound",
                        ],
                    }
                )
                confidence_snapshot_ids.append(
                    str(confidence["confidence_snapshot_id"])
                )
                if confidence["confidence_state"] != "ready":
                    candidate["availability_status"] = "unavailable"
                    candidate["confidence_block_reason"] = (
                        f"statistical_confidence_{confidence['confidence_state']}"
                    )
                    blocked += 1
                    continue
            if row["data_state"] in {"blocked", "stale", "unknown"}:
                candidate["availability_status"] = "unavailable"
                candidate["dynamic_metrics_block_reason"] = (
                    f"dynamic_metrics_{row['data_state']}"
                )
                blocked += 1
                continue
            success = metrics.get("success_rate")
            latency = metrics.get("latency_p50_ms")
            if success is None or latency is None:
                candidate["availability_status"] = "unavailable"
                candidate["dynamic_metrics_block_reason"] = (
                    "dynamic_metrics_required_field_missing"
                )
                blocked += 1
                continue
            candidate["success_rate"] = str(success)
            candidate["observed_success_rate"] = str(success)
            candidate["sample_size"] = str(row["sample_count"])
            candidate["latency_ms"] = str(latency)
            candidate["metrics_updated_at"] = self._timestamp(
                str(row["last_evidence_at"])
            )
            candidate["data_source"] = "incremental_metric_snapshot"
            candidate["dynamic_metrics_block_reason"] = ""
            applied += 1
        return output, {
            "provider": "incremental_metric_snapshot",
            "window": self.window,
            "environment_id": environment_id,
            "request_profile_id": request_profile_id,
            "applied_count": applied,
            "blocked_count": blocked,
            "unavailable_count": unavailable,
            "snapshot_ids": sorted(set(snapshot_ids)),
            "confidence_version": self.confidence_version,
            "confidence_snapshot_ids": sorted(set(confidence_snapshot_ids)),
            "raw_evidence_scanned": False,
        }
