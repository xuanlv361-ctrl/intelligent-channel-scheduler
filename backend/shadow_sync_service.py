from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from backend.persistent_session_service import PersistentSessionError, PersistentSessionService
from backend.realtime_log_sync_service import ACTIVE_STATES, RealtimeLogSyncService
from backend.security.principal import PrincipalContext
from backend.tenant_security import TenantScope


EXACT_CORRELATIONS = {
    "exact_provider_request_id", "exact_provider_response_id",
    "exact_provider_trace_id", "exact_client_correlation_id",
    "manually_confirmed",
}


class ShadowSyncService:
    """Idempotent orchestration and read model over the existing log-sync tables."""

    def __init__(
        self,
        realtime: RealtimeLogSyncService,
        persistent: PersistentSessionService,
        launch_persistent: Callable[[str], int],
        *,
        freshness_seconds: int = 60,
        initial_range_hours: int = 24,
        overlap_seconds: int = 120,
        now: Callable[[], datetime] | None = None,
    ):
        self.realtime = realtime
        self.persistent = persistent
        self.launch_persistent = launch_persistent
        self.freshness_seconds = max(15, int(freshness_seconds))
        self.initial_range_hours = max(1, min(168, int(initial_range_hours)))
        self.overlap_seconds = max(0, min(600, int(overlap_seconds)))
        self.now = now or (lambda: datetime.now(timezone.utc))
        if hasattr(self.realtime, "connect"):
            with self.realtime.connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS shadow_sync_settings(
                  tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
                  environment_id TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
                  interval_seconds INTEGER NOT NULL,updated_at TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,environment_id))""")
                scope = TenantScope.local_development()
                db.execute("""INSERT OR IGNORE INTO shadow_sync_settings(
                  tenant_id,workspace_id,environment_id,enabled,interval_seconds,updated_at)
                  VALUES(?,?,'china_uat',1,?,?)""", (
                    *scope.sql_parameters(), self.freshness_seconds,
                    self.now().astimezone(timezone.utc).isoformat()))

    @staticmethod
    def _scope(principal: PrincipalContext | None) -> TenantScope:
        if isinstance(principal, PrincipalContext) and principal.is_verified:
            return TenantScope(principal.tenant_id, principal.workspace_id)
        return TenantScope.local_development()

    @staticmethod
    def _parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def ensure_sync(
        self, *, force: bool = False, trigger: str = "frontend_init",
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        settings = self.source_config(principal=principal)
        if not settings["enabled"]:
            return {"status": "paused", "created": False, "trigger": trigger}
        jobs = self.realtime.list_jobs("china_uat", principal=principal)
        active = next((job for job in jobs if job["state"] in ACTIVE_STATES or
                       job["state"] == "stopping"), None)
        if active:
            return {"status": "already_running", "created": False,
                    "trigger": trigger, "job": active}

        now = self.now().astimezone(timezone.utc)
        latest = jobs[0] if jobs else None
        last_success = self._parse(
            (latest or {}).get("last_successful_read_at") or
            (latest or {}).get("last_successful_poll_at"))
        if not force and last_success and (
                now - last_success).total_seconds() < self.freshness_seconds:
            return {"status": "fresh", "created": False, "trigger": trigger,
                    "fresh_until": (last_success + timedelta(
                        seconds=self.freshness_seconds)).isoformat(), "job": latest}

        session_status = self.persistent.status(principal=principal)
        session = session_status.get("session")
        if not session:
            return {"status": "not_configured", "created": False,
                    "trigger": trigger, "reason": "persistent_session_missing"}
        if session.get("state") not in {"active", "in_use"} or \
                session.get("authentication_status") not in {"authenticated", "verified"}:
            return {"status": "authentication_required", "created": False,
                    "trigger": trigger,
                    "reason": session.get("failure_code") or "persistent_session_unavailable"}
        if session.get("usage_state") == "in_use":
            return {"status": "already_running", "created": False,
                    "trigger": trigger, "job": session_status.get("active_job")}

        cursor = self.realtime.cursor("china_uat", principal=principal)
        watermark = self._parse((cursor or {}).get("cursor_value"))
        if watermark is None:
            watermark = next(
                (parsed for job in jobs
                 if (parsed := self._parse(job.get("watermark_utc"))) is not None),
                None,
            )
        date_from = (
            watermark - timedelta(seconds=self.overlap_seconds)
            if watermark else now - timedelta(hours=self.initial_range_hours))
        try:
            job = self.persistent.create_job({
                "environment_id": "china_uat",
                "date_from": date_from.isoformat(),
                "date_to": now.isoformat(),
                "timezone": "Asia/Shanghai",
                "periodic_polling": False,
                "sync_interval_seconds": 7,
                "maximum_records": 500,
                "maximum_http_reads": 50,
                "maximum_records_observed": 500,
                "maximum_records_accepted": 500,
                "maximum_elapsed_seconds": 120,
                "page_size": 100,
                "maximum_pages": 50,
                "persistent_session_id": session["persistent_session_id"],
                "explicit_confirmation": True,
            }, principal=principal)
            pid = self.launch_persistent(job["sync_job_id"])
            job = self.realtime.update_job(
                job["sync_job_id"], worker_pid=pid, principal=principal,
                permission="collector.oneshot.execute")
            self.realtime.event(
                job["sync_job_id"], "china_uat", "shadow_sync_ensured",
                {"trigger": trigger, "force": bool(force),
                 "range_from": date_from.isoformat(), "range_to": now.isoformat()},
                principal=principal, permission="collector.oneshot.execute")
            return {"status": "started", "created": True,
                    "trigger": trigger, "job": job}
        except PersistentSessionError as exc:
            return {"status": "blocked", "created": False,
                    "trigger": trigger, "reason": str(exc)}

    def source_config(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        if not hasattr(self.realtime, "connect"):
            return {"environment_id": "china_uat", "log_source_id": "domestic_uat_billing_logs",
                    "enabled": True, "interval_seconds": self.freshness_seconds,
                    "api_url": None, "authentication_reference": "persistent_session_vault"}
        scope = self._scope(principal)
        with self.realtime.connect() as db:
            row = db.execute("""SELECT enabled,interval_seconds,updated_at
              FROM shadow_sync_settings WHERE tenant_id=? AND workspace_id=?
              AND environment_id='china_uat'""", scope.sql_parameters()).fetchone()
        try:
            contract = self.realtime.resolve_environment("china_uat")
            safe_url = contract["log_page_url"]
        except Exception:
            safe_url = None
        return {
            "environment_id": "china_uat", "log_source_id": "domestic_uat_billing_logs",
            "enabled": bool(row["enabled"]) if row else True,
            "interval_seconds": int(row["interval_seconds"]) if row else self.freshness_seconds,
            "updated_at": row["updated_at"] if row else None,
            "api_url": safe_url,
            "authentication_reference": "persistent_session_vault",
        }

    def set_enabled(self, enabled: bool, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal)
        now = self.now().astimezone(timezone.utc).isoformat()
        with self.realtime.connect() as db:
            db.execute("""INSERT INTO shadow_sync_settings(
              tenant_id,workspace_id,environment_id,enabled,interval_seconds,updated_at)
              VALUES(?,?,'china_uat',?,?,?)
              ON CONFLICT(tenant_id,workspace_id,environment_id) DO UPDATE SET
              enabled=excluded.enabled,updated_at=excluded.updated_at""", (
                *scope.sql_parameters(), int(enabled), self.freshness_seconds, now))
        return self.source_config(principal=principal)

    def confirm_correlation(
        self, record_id: str, execution_id: str, *,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        """Confirm one previously unlinked record without weakening exact matching."""
        scope = self.realtime._scope(principal, "evidence.import")
        record_id = str(record_id or "").strip()
        execution_id = str(execution_id or "").strip()
        if not record_id or not execution_id:
            raise ValueError("shadow_manual_association_invalid")
        with self.realtime.connect() as db:
            record = db.execute("""SELECT record_id,environment_id FROM realtime_log_records
              WHERE record_id=? AND tenant_id=? AND workspace_id=?""",
              (record_id, *scope.sql_parameters())).fetchone()
            if not record:
                raise LookupError("shadow_record_not_found")
            execution = db.execute("""SELECT execution_id FROM uat_executions
              WHERE execution_id=? AND environment_id=?
                AND tenant_id=? AND workspace_id=?""",
              (execution_id, record["environment_id"], *scope.sql_parameters())).fetchone()
            if not execution:
                raise LookupError("shadow_execution_not_found")
            previous = db.execute("""SELECT state,execution_id,method,details_json
              FROM realtime_log_correlations WHERE record_id=? AND environment_id=?
                AND tenant_id=? AND workspace_id=?""",
              (record_id, record["environment_id"], *scope.sql_parameters())).fetchone()
            if previous and previous["state"] in EXACT_CORRELATIONS:
                if previous["execution_id"] == execution_id:
                    return {"status": "already_confirmed", "record_id": record_id,
                            "execution_id": execution_id, "correlation_status": previous["state"]}
                raise ValueError("shadow_correlation_already_exact")
            now = self.now().astimezone(timezone.utc).isoformat()
            actor = getattr(principal, "principal_id", "local_operator")
            details = {
                "confirmed_by": actor,
                "confirmed_at": now,
                "previous_state": previous["state"] if previous else "unmatched",
                "previous_execution_id": previous["execution_id"] if previous else None,
            }
            db.execute("""INSERT INTO realtime_log_correlations(
              correlation_id,execution_id,record_id,environment_id,state,method,
              created_at,details_json,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(environment_id,record_id) DO UPDATE SET
                execution_id=excluded.execution_id,state=excluded.state,
                method=excluded.method,created_at=excluded.created_at,
                details_json=excluded.details_json
              WHERE tenant_id=excluded.tenant_id AND workspace_id=excluded.workspace_id""", (
                str(uuid.uuid4()), execution_id, record_id, record["environment_id"],
                "manually_confirmed", "manual_operator_confirmation", now,
                json.dumps(details, ensure_ascii=False, sort_keys=True),
                *scope.sql_parameters(),
            ))
        return {"status": "confirmed", "record_id": record_id,
                "execution_id": execution_id,
                "correlation_status": "manually_confirmed"}

    def _rows(self, principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._scope(principal)
        with self.realtime.connect() as db:
            rows = db.execute("""SELECT r.*,c.correlation_id,c.execution_id,
              c.state AS correlation_state,c.method AS correlation_method,
              c.created_at AS correlated_at,c.details_json AS correlation_details
              FROM realtime_log_records r
              LEFT JOIN realtime_log_correlations c
                ON c.record_id=r.record_id AND c.tenant_id=r.tenant_id
                AND c.workspace_id=r.workspace_id
              WHERE r.environment_id='china_uat'
                AND r.tenant_id=? AND r.workspace_id=?
              ORDER BY COALESCE(r.platform_created_at,r.observed_at) DESC""",
              scope.sql_parameters()).fetchall()
        executions = {row.get("execution_id"): row for row in self.realtime._executions()}
        result = []
        for row in rows:
            normalized = json.loads(row["normalized_json"])
            execution = executions.get(row["execution_id"] or "", {})
            recommendation = (
                (execution.get("shadow_decision") or {}).get("recommended_candidate")
                or execution.get("scheduler_recommendation")
                or execution.get("selected_channel")
                or execution.get("selected_model"))
            actual_channel = normalized.get("actual_channel_id") or normalized.get("actual_channel_name")
            actual_model = normalized.get("actual_model") or normalized.get("requested_model")
            actual_selection = actual_channel or actual_model
            comparable = bool(row["correlation_state"] in EXACT_CORRELATIONS and
                              recommendation and actual_selection)
            actual_cost = normalized.get("actual_cost")
            estimated_cost = execution.get("estimated_cost")
            if estimated_cost is None:
                estimated_cost = execution.get("estimated_cost_cny")
            result.append({
                "record_id": row["record_id"],
                "platform_log_id": row["platform_log_id"],
                "environment_id": row["environment_id"],
                "occurred_at": normalized.get("normalized_utc_timestamp") or
                    row["platform_created_at"] or row["observed_at"],
                "request_id": execution.get("local_request_id") or
                    execution.get("request_id"),
                "provider_request_id": row["provider_request_id"] or
                    normalized.get("provider_request_id") or row["request_id"],
                "provider_response_id": row["provider_response_id"] or
                    normalized.get("provider_response_id") or row["response_id"],
                "provider_trace_id": row["provider_trace_id"] or
                    normalized.get("provider_trace_id"),
                "response_id": execution.get("response_id"),
                "decision_id": normalized.get("decision_id") or execution.get("decision_id"),
                "model": actual_model,
                "actual_channel": actual_channel,
                "shadow_recommendation": recommendation,
                "correlation_status": row["correlation_state"] or "unmatched",
                "correlation_method": row["correlation_method"],
                "execution_id": row["execution_id"],
                "comparable": comparable,
                "comparison_dimension": "channel" if actual_channel else "model",
                "matches": (recommendation == actual_selection) if comparable else None,
                "unlinked_reason": (None if row["correlation_state"] in EXACT_CORRELATIONS
                    else "historical_provider_identifier_not_captured"),
                "latency_ms": normalized.get("latency_ms"),
                "ttft_ms": normalized.get("ttft_ms"),
                "actual_cost": actual_cost, "currency": normalized.get("currency"),
                "estimated_cost": estimated_cost,
                "cost_difference": (actual_cost - estimated_cost)
                    if isinstance(actual_cost, (int, float)) and
                    isinstance(estimated_cost, (int, float)) else None,
                "http_status": normalized.get("http_status"),
                "result": normalized.get("result"),
                "error_category": normalized.get("error_category"),
                "source_type": normalized.get("source_type"),
                "sync_job_id": row["sync_job_id"],
                "is_mock": False,
            })
        return result

    def dashboard(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        jobs = self.realtime.list_jobs("china_uat", principal=principal)
        latest = jobs[0] if jobs else None
        cursor = self.realtime.cursor("china_uat", principal=principal)
        rows = self._rows(principal)
        comparable = [row for row in rows if row["comparable"]]
        matched = [row for row in comparable if row["matches"]]
        exact = [row for row in rows if row["correlation_status"] in EXACT_CORRELATIONS]
        status = "not_configured" if not latest else (
            "syncing" if latest["state"] in ACTIVE_STATES else
            "sync_failed" if latest["state"] in {"failed", "blocked"} else
            "sync_succeeded" if latest.get("last_successful_read_at") or
            latest.get("last_successful_poll_at") else latest["state"])
        session = self.persistent.status(principal=principal)
        session_row = session.get("session")
        connection = (
            "not_configured" if not session_row else
            "connected" if session_row.get("state") in {"active", "in_use"} and
            session_row.get("authentication_status") in {"authenticated", "verified"}
            else "authentication_required")
        return {
            "environment_id": "china_uat", "source_type": "measured_uat_realtime_browser_sync",
            "is_mock": False, "connection_status": connection,
            "auto_sync_enabled": self.source_config(principal=principal)["enabled"],
            "sync_status": status, "sync_interval_seconds": self.freshness_seconds,
            "last_successful_at": (latest or {}).get("last_successful_read_at") or
                (latest or {}).get("last_successful_poll_at"),
            "next_sync_at": ((self._parse((latest or {}).get("last_successful_read_at") or
                (latest or {}).get("last_successful_poll_at")) +
                timedelta(seconds=self.freshness_seconds)).isoformat()
                if self._parse((latest or {}).get("last_successful_read_at") or
                    (latest or {}).get("last_successful_poll_at")) else None),
            "last_error": (latest or {}).get("error_code"),
            "last_safe_error_message": (latest or {}).get("safe_error_message"),
            "cursor": cursor,
            "latest_job": latest,
            "metrics": {
                "synced_logs": len(rows), "linked_executions": len(exact),
                "unlinked_logs": sum(row["correlation_status"] not in EXACT_CORRELATIONS
                                     for row in rows),
                "comparable_logs": len(comparable),
                "agreement_rate": len(matched) / len(comparable) if comparable else None,
                "average_latency_difference_ms": None,
                "estimated_cost_difference": (
                    sum(row["cost_difference"] for row in comparable
                        if row["cost_difference"] is not None) /
                    sum(row["cost_difference"] is not None for row in comparable)
                    if any(row["cost_difference"] is not None for row in comparable) else None),
            },
            "items": rows[:500],
            "association_status": "linked" if exact else "insufficient_identifiers",
            "shadow_status": "calculated" if comparable else "insufficient_decision_evidence",
            "unlinked": [row for row in rows if row["correlation_status"] not in EXACT_CORRELATIONS][:200],
            "sync_history": jobs[:100],
            "technical": {
                "cursor_storage": "realtime_log_sync_cursors",
                "record_storage": "realtime_log_records",
                "correlation_storage": "realtime_log_correlations",
                "completion_calls": 0,
            },
        }
