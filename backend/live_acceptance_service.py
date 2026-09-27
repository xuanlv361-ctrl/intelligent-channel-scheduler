"""Persistent evidence for live strategy-effect and continuous-probe runs."""
from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from backend.tenant_security import TenantScope


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(float(value) for value in values if value is not None)
    if not ordered:
        return None
    index = (len(ordered) - 1) * percentile_value
    low, high = math.floor(index), math.ceil(index)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


class LiveAcceptanceService:
    def __init__(self, database_path: str | Path,
                 scope: TenantScope | None = None):
        self.path = Path(database_path)
        self.scope = scope or TenantScope.local_development()
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _migrate(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_effect_runs(
              strategy_effect_run_id TEXT NOT NULL,status TEXT NOT NULL,
              environment_id TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,
              git_commit TEXT NOT NULL,database_watermark INTEGER NOT NULL,
              configuration_version TEXT,price_version TEXT,model_catalog_version TEXT,
              log_sync_time TEXT,uat_environment_enabled INTEGER NOT NULL,
              planned_requests INTEGER NOT NULL,completed_requests INTEGER NOT NULL DEFAULT 0,
              evidence_path TEXT,summary_json TEXT,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,strategy_effect_run_id));
            CREATE TABLE IF NOT EXISTS strategy_effect_results(
              result_id INTEGER PRIMARY KEY AUTOINCREMENT,strategy_effect_run_id TEXT NOT NULL,
              request_case_id TEXT NOT NULL,repetition INTEGER NOT NULL,strategy TEXT NOT NULL,
              strategy_version TEXT,configuration_version TEXT,metric_snapshot_id TEXT,
              selected_model TEXT NOT NULL,candidates_json TEXT NOT NULL,exclusions_json TEXT NOT NULL,
              scores_json TEXT NOT NULL,request_id TEXT,response_id TEXT,decision_id TEXT,
              actual_model TEXT,http_status INTEGER,success INTEGER NOT NULL,
              first_token_latency_ms REAL,total_latency_ms REAL,input_tokens INTEGER,
              cached_input_tokens INTEGER,output_tokens INTEGER,total_tokens INTEGER,
              actual_provider_cost TEXT,estimated_versioned_price TEXT,cost_status TEXT NOT NULL,
              price_version TEXT,retry_count INTEGER NOT NULL,fallback_used INTEGER NOT NULL,
              output_complete INTEGER NOT NULL,assertion_passed INTEGER NOT NULL,
              assertion_json TEXT NOT NULL,error_category TEXT,created_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
              UNIQUE(tenant_id,workspace_id,strategy_effect_run_id,request_case_id,repetition,strategy));
            CREATE TABLE IF NOT EXISTS continuous_probe_runs(
              probe_run_id TEXT NOT NULL,status TEXT NOT NULL,environment_id TEXT NOT NULL,
              started_at TEXT NOT NULL,finished_at TEXT,git_commit TEXT NOT NULL,
              database_watermark INTEGER NOT NULL,configuration_json TEXT NOT NULL,
              current_phase INTEGER NOT NULL DEFAULT 1,current_interval_seconds REAL NOT NULL,
              current_concurrency INTEGER NOT NULL DEFAULT 1,sent_count INTEGER NOT NULL DEFAULT 0,
              completed_count INTEGER NOT NULL DEFAULT 0,success_count INTEGER NOT NULL DEFAULT 0,
              failure_count INTEGER NOT NULL DEFAULT 0,throttle_count INTEGER NOT NULL DEFAULT 0,
              pause_count INTEGER NOT NULL DEFAULT 0,circuit_open_count INTEGER NOT NULL DEFAULT 0,
              recovery_count INTEGER NOT NULL DEFAULT 0,last_error TEXT,next_run_at TEXT,
              stop_requested INTEGER NOT NULL DEFAULT 0,summary_json TEXT,evidence_path TEXT,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,probe_run_id));
            CREATE TABLE IF NOT EXISTS continuous_probe_events(
              event_id INTEGER PRIMARY KEY AUTOINCREMENT,probe_run_id TEXT NOT NULL,
              event_type TEXT NOT NULL,source_type TEXT NOT NULL,created_at TEXT NOT NULL,
              details_json TEXT NOT NULL,tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_strategy_effect_results_run
              ON strategy_effect_results(tenant_id,workspace_id,strategy_effect_run_id,strategy);
            CREATE INDEX IF NOT EXISTS ix_continuous_probe_events_run
              ON continuous_probe_events(tenant_id,workspace_id,probe_run_id,event_id);
            """)
            probe_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(continuous_probe_runs)"
            )}
            probe_migrations = {
                "task_name": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
                "desired_status": "TEXT",
                "revision": "INTEGER NOT NULL DEFAULT 1",
                "configuration_checksum": "TEXT",
                "audit_id": "TEXT",
                "created_by": "TEXT",
                "cloned_from_probe_run_id": "TEXT",
                "planned_duration_seconds": "INTEGER",
            }
            for name, sql_type in probe_migrations.items():
                if name not in probe_columns:
                    db.execute(
                        f"ALTER TABLE continuous_probe_runs ADD COLUMN {name} {sql_type}"
                    )
            db.execute("""UPDATE continuous_probe_runs SET
              task_name=COALESCE(NULLIF(task_name,''),'未命名探测任务'),
              created_at=COALESCE(created_at,started_at),
              updated_at=COALESCE(updated_at,finished_at,started_at),
              desired_status=COALESCE(desired_status,status),revision=COALESCE(revision,1)
              WHERE tenant_id=? AND workspace_id=?""", self.scope.sql_parameters())
            db.execute("""CREATE INDEX IF NOT EXISTS ix_continuous_probe_runs_scope_time
              ON continuous_probe_runs(tenant_id,workspace_id,created_at DESC)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_continuous_probe_runs_scope_status
              ON continuous_probe_runs(tenant_id,workspace_id,status,created_at DESC)""")

    @staticmethod
    def _safe_probe_configuration(value: Any) -> Any:
        """Drop credentials recursively before probe configuration is persisted."""
        secret_fragments = {
            "authorization", "api_key", "apikey", "cookie", "password", "secret",
            "token", "credential", "set-cookie",
        }
        if isinstance(value, dict):
            return {
                str(key): LiveAcceptanceService._safe_probe_configuration(item)
                for key, item in value.items()
                if not any(fragment in str(key).lower() for fragment in secret_fragments)
            }
        if isinstance(value, list):
            return [LiveAcceptanceService._safe_probe_configuration(item) for item in value]
        return value

    @staticmethod
    def _new_probe_run_id() -> str:
        return f"PRB-{uuid.uuid4().hex.upper()}"

    def create_strategy_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("""INSERT INTO strategy_effect_runs(
              strategy_effect_run_id,status,environment_id,started_at,git_commit,
              database_watermark,configuration_version,price_version,model_catalog_version,
              log_sync_time,uat_environment_enabled,planned_requests,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                payload["strategy_effect_run_id"], "RUNNING", payload["environment_id"],
                payload.get("started_at") or utcnow(), payload["git_commit"],
                payload["database_watermark"], payload.get("configuration_version"),
                payload.get("price_version"), payload.get("model_catalog_version"),
                payload.get("log_sync_time"), int(payload["uat_environment_enabled"]),
                payload["planned_requests"], *self.scope.sql_parameters()))
        return self.strategy_run(payload["strategy_effect_run_id"])

    def record_strategy_result(self, run_id: str, row: dict[str, Any]) -> None:
        fields = (
            "request_case_id", "repetition", "strategy", "strategy_version",
            "configuration_version", "metric_snapshot_id", "selected_model",
            "candidates_json", "exclusions_json", "scores_json", "request_id",
            "response_id", "decision_id", "actual_model", "http_status", "success",
            "first_token_latency_ms", "total_latency_ms", "input_tokens",
            "cached_input_tokens", "output_tokens", "total_tokens",
            "actual_provider_cost", "estimated_versioned_price", "cost_status",
            "price_version", "retry_count", "fallback_used", "output_complete",
            "assertion_passed", "assertion_json", "error_category", "created_at")
        values = [row.get(field) for field in fields]
        with self.connect() as db:
            db.execute(f"""INSERT INTO strategy_effect_results(
              strategy_effect_run_id,{','.join(fields)},tenant_id,workspace_id)
              VALUES({','.join('?' for _ in range(len(fields)+3))})""",
              (run_id, *values, *self.scope.sql_parameters()))
            db.execute("""UPDATE strategy_effect_runs SET completed_requests=completed_requests+1
              WHERE strategy_effect_run_id=? AND tenant_id=? AND workspace_id=?""",
              (run_id, *self.scope.sql_parameters()))

    def finish_strategy_run(self, run_id: str, summary: dict[str, Any],
                            evidence_path: str) -> None:
        with self.connect() as db:
            db.execute("""UPDATE strategy_effect_runs SET status='COMPLETED',finished_at=?,
              summary_json=?,evidence_path=? WHERE strategy_effect_run_id=?
              AND tenant_id=? AND workspace_id=?""", (
                utcnow(), json.dumps(summary, ensure_ascii=False, sort_keys=True),
                evidence_path, run_id, *self.scope.sql_parameters()))

    def strategy_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            run = db.execute("""SELECT * FROM strategy_effect_runs WHERE
              strategy_effect_run_id=? AND tenant_id=? AND workspace_id=?""",
              (run_id, *self.scope.sql_parameters())).fetchone()
            rows = db.execute("""SELECT * FROM strategy_effect_results WHERE
              strategy_effect_run_id=? AND tenant_id=? AND workspace_id=?
              ORDER BY result_id""", (run_id, *self.scope.sql_parameters())).fetchall()
        if not run:
            raise KeyError("strategy_effect_run_not_found")
        result = dict(run)
        result["items"] = [dict(row) for row in rows]
        result["summary"] = json.loads(result.pop("summary_json") or "{}")
        return result

    def summarize_strategy(self, run_id: str) -> dict[str, Any]:
        run = self.strategy_run(run_id)
        summaries: dict[str, Any] = {}
        baseline_models = {(row["request_case_id"], row["repetition"]): row["selected_model"]
                           for row in run["items"] if row["strategy"] == "current_actual"}
        for strategy in ("current_actual", "latency_first", "cost_first"):
            rows = [row for row in run["items"] if row["strategy"] == strategy]
            successful = [row for row in rows if row["success"]]
            latency = [row["total_latency_ms"] for row in successful
                       if row["total_latency_ms"] is not None]
            first = [row["first_token_latency_ms"] for row in successful
                     if row["first_token_latency_ms"] is not None]
            actual = [Decimal(row["actual_provider_cost"]) for row in rows
                      if row["actual_provider_cost"] is not None]
            estimated = [Decimal(row["estimated_versioned_price"]) for row in rows
                         if row["estimated_versioned_price"] is not None]
            summaries[strategy] = {
                "request_count": len(rows),
                "success_rate": len(successful) / len(rows) if rows else None,
                "p50_ms": percentile(latency, .50), "p95_ms": percentile(latency, .95),
                "p99_ms": percentile(latency, .99),
                "first_token_p50_ms": percentile(first, .50),
                "first_token_p95_ms": percentile(first, .95),
                "input_tokens": sum(row["input_tokens"] or 0 for row in rows),
                "cached_input_tokens": sum(row["cached_input_tokens"] or 0 for row in rows),
                "output_tokens": sum(row["output_tokens"] or 0 for row in rows),
                "total_tokens": sum(row["total_tokens"] or 0 for row in rows),
                "actual_provider_cost": str(sum(actual, Decimal("0"))) if actual else None,
                "actual_cost_coverage": len(actual) / len(rows) if rows else 0,
                "estimated_versioned_price": str(sum(estimated, Decimal("0"))) if estimated else None,
                "estimated_cost_coverage": len(estimated) / len(rows) if rows else 0,
                "fallback_rate": sum(row["fallback_used"] for row in rows) / len(rows) if rows else None,
                "retry_rate": sum(row["retry_count"] > 0 for row in rows) / len(rows) if rows else None,
                "output_complete_rate": sum(row["output_complete"] for row in rows) / len(rows) if rows else None,
                "assertion_pass_rate": sum(row["assertion_passed"] for row in rows) / len(rows) if rows else None,
                "model_distribution": {model: sum(row["selected_model"] == model for row in rows)
                                       for model in sorted({row["selected_model"] for row in rows})},
                "selection_changes_from_baseline": sum(
                    baseline_models.get((row["request_case_id"], row["repetition"])) != row["selected_model"]
                    for row in rows) if strategy != "current_actual" else 0,
                "metric_coverage": len(latency) / len(rows) if rows else 0,
                "confidence": "high" if len(rows) >= 30 and len(latency) == len(rows) else "medium",
            }
        return {"strategy_effect_run_id": run_id, "strategies": summaries,
                "completed_requests": len(run["items"])}

    def reconcile_strategy_actual_costs(self) -> dict[str, Any]:
        """Apply only exact Provider-billing costs to post-run effect evidence."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = int(db.execute("""SELECT COUNT(*) FROM strategy_effect_results
              WHERE actual_provider_cost IS NOT NULL AND tenant_id=? AND workspace_id=?""",
              self.scope.sql_parameters()).fetchone()[0])
            db.execute("""UPDATE strategy_effect_results AS r SET
              actual_provider_cost=(SELECT l.provider_cost_amount_exact
                FROM standardized_call_logs l WHERE l.request_id=r.request_id
                AND l.match_confidence='exact' AND l.cost_type='provider_actual'
                AND l.tenant_id=r.tenant_id AND l.workspace_id=r.workspace_id
                ORDER BY l.cursor_id DESC LIMIT 1),
              cost_status='actual_provider_cost'
              WHERE r.tenant_id=? AND r.workspace_id=? AND EXISTS(
                SELECT 1 FROM standardized_call_logs l WHERE l.request_id=r.request_id
                AND l.match_confidence='exact' AND l.cost_type='provider_actual'
                AND l.provider_cost_amount_exact IS NOT NULL
                AND l.tenant_id=r.tenant_id AND l.workspace_id=r.workspace_id)""",
              self.scope.sql_parameters())
            after = int(db.execute("""SELECT COUNT(*) FROM strategy_effect_results
              WHERE actual_provider_cost IS NOT NULL AND tenant_id=? AND workspace_id=?""",
              self.scope.sql_parameters()).fetchone()[0])
            run_ids = [str(row[0]) for row in db.execute(
                """SELECT DISTINCT strategy_effect_run_id FROM strategy_effect_results
                WHERE tenant_id=? AND workspace_id=?""", self.scope.sql_parameters())]
        for run_id in run_ids:
            summary = self.summarize_strategy(run_id)
            with self.connect() as db:
                db.execute("""UPDATE strategy_effect_runs SET summary_json=?
                  WHERE strategy_effect_run_id=? AND tenant_id=? AND workspace_id=?""",
                  (json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id,
                   *self.scope.sql_parameters()))
        return {"updated_count": max(0, after - before),
                "actual_cost_count": after, "strategy_run_count": len(run_ids)}

    def latest_strategy_run(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("""SELECT strategy_effect_run_id FROM strategy_effect_runs
              WHERE tenant_id=? AND workspace_id=? ORDER BY started_at DESC LIMIT 1""",
              self.scope.sql_parameters()).fetchone()
        return self.strategy_run(row[0]) if row else None

    def create_probe_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a canonical probe task; the backend is the sole ID authority."""
        if payload.get("probe_run_id"):
            raise ValueError("probe_run_id_is_server_generated")
        configuration = self._safe_probe_configuration(payload.get("configuration") or {})
        if not isinstance(configuration, dict):
            raise ValueError("invalid_probe_configuration")
        phases = configuration.get("phases") or []
        interval = configuration.get("request_interval_seconds")
        if interval is None and phases and isinstance(phases[0], dict):
            interval = phases[0].get("interval_seconds")
        interval = float(interval or 10)
        concurrency = int(configuration.get("max_concurrency") or 1)
        planned_duration = configuration.get("duration_seconds")
        if planned_duration is None:
            planned_duration = sum(
                int(phase.get("duration_seconds") or 0)
                for phase in phases if isinstance(phase, dict)
            ) or None
        encoded = json.dumps(configuration, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        run_id = self._new_probe_run_id()
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO continuous_probe_runs(
              probe_run_id,status,environment_id,started_at,git_commit,database_watermark,
              configuration_json,current_interval_seconds,current_concurrency,
              task_name,created_at,updated_at,desired_status,revision,
              configuration_checksum,audit_id,created_by,cloned_from_probe_run_id,
              planned_duration_seconds,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                run_id, "CREATED", payload.get("environment_id") or "china_uat", now,
                payload.get("git_commit") or "unknown", int(payload.get("database_watermark") or 0),
                encoded, interval, concurrency,
                str(payload.get("task_name") or "未命名探测任务").strip() or "未命名探测任务",
                now, now, "CREATED", 1, hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                payload.get("audit_id"), payload.get("created_by"),
                payload.get("cloned_from_probe_run_id"), planned_duration,
                *self.scope.sql_parameters()))
        self.probe_event(run_id, "probe_created", "local_control_event", {
            "task_name": payload.get("task_name") or "未命名探测任务",
            "audit_id": payload.get("audit_id"),
        })
        return self.probe_run(run_id)

    def update_probe(self, run_id: str, **changes: Any) -> None:
        allowed = {"status", "current_phase", "current_interval_seconds",
                   "current_concurrency", "sent_count", "completed_count", "success_count",
                   "failure_count", "throttle_count", "pause_count", "circuit_open_count",
                   "recovery_count", "last_error", "next_run_at", "stop_requested",
                   "finished_at", "summary_json", "evidence_path", "desired_status"}
        if not changes or set(changes) - allowed:
            raise ValueError("invalid_probe_update")
        assignments = ",".join(f"{key}=?" for key in changes)
        values = [json.dumps(value, ensure_ascii=False, sort_keys=True)
                  if key == "summary_json" and isinstance(value, dict) else value
                  for key, value in changes.items()]
        with self.connect() as db:
            cursor = db.execute(f"""UPDATE continuous_probe_runs SET {assignments},
              updated_at=?,revision=revision+1
              WHERE probe_run_id=? AND tenant_id=? AND workspace_id=?""",
              (*values, utcnow(), run_id, *self.scope.sql_parameters()))
            if cursor.rowcount != 1:
                raise KeyError("probe_run_not_found")

    def probe_event(self, run_id: str, event_type: str, source_type: str,
                    details: dict[str, Any]) -> None:
        safe_details = self._safe_probe_configuration(details)
        with self.connect() as db:
            db.execute("""INSERT INTO continuous_probe_events(
              probe_run_id,event_type,source_type,created_at,details_json,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?)""", (run_id, event_type, source_type, utcnow(),
              json.dumps(safe_details, ensure_ascii=False, sort_keys=True), *self.scope.sql_parameters()))

    def probe_run(self, run_id: str, include_events: bool = True) -> dict[str, Any]:
        with self.connect() as db:
            run = db.execute("""SELECT * FROM continuous_probe_runs WHERE probe_run_id=?
              AND tenant_id=? AND workspace_id=?""", (run_id, *self.scope.sql_parameters())).fetchone()
            events = db.execute("""SELECT event_id,event_type,source_type,created_at,details_json
              FROM continuous_probe_events WHERE probe_run_id=? AND tenant_id=? AND workspace_id=?
              ORDER BY event_id""", (run_id, *self.scope.sql_parameters())).fetchall() if include_events else []
        if not run:
            raise KeyError("probe_run_not_found")
        result = dict(run)
        result["configuration"] = json.loads(result.pop("configuration_json"))
        result["summary"] = json.loads(result.pop("summary_json") or "{}")
        result["events"] = [{**dict(row), "details": json.loads(row["details_json"])}
                            for row in events]
        for row in result["events"]:
            row.pop("details_json", None)
        return result

    def list_probe_runs(self, *, query: str | None = None, status: str | None = None,
                        started_from: str | None = None, started_to: str | None = None,
                        limit: int = 50, offset: int = 0) -> dict[str, Any]:
        limit = min(max(int(limit), 1), 200)
        offset = max(int(offset), 0)
        clauses = ["tenant_id=?", "workspace_id=?"]
        parameters: list[Any] = list(self.scope.sql_parameters())
        if query:
            clauses.append("(task_name LIKE ? OR probe_run_id LIKE ?)")
            term = f"%{query.strip()}%"
            parameters.extend((term, term))
        if status:
            clauses.append("status=?")
            parameters.append(status.upper())
        if started_from:
            clauses.append("created_at>=?")
            parameters.append(started_from)
        if started_to:
            clauses.append("created_at<=?")
            parameters.append(started_to)
        where = " AND ".join(clauses)
        with self.connect() as db:
            total = int(db.execute(
                f"SELECT COUNT(*) FROM continuous_probe_runs WHERE {where}", parameters
            ).fetchone()[0])
            rows = db.execute(f"""SELECT * FROM continuous_probe_runs WHERE {where}
              ORDER BY created_at DESC,probe_run_id DESC LIMIT ? OFFSET ?""",
              (*parameters, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["configuration"] = json.loads(item.pop("configuration_json") or "{}")
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def transition_probe_run(self, run_id: str, action: str, *,
                             expected_revision: int | None = None,
                             reason: str | None = None,
                             operator_id: str | None = None) -> dict[str, Any]:
        action = action.lower().strip()
        transitions = {
            "start": ({"CREATED"}, "RUNNING"),
            "pause": ({"RUNNING"}, "PAUSED"),
            "resume": ({"PAUSED"}, "RUNNING"),
            "stop": ({"CREATED", "RUNNING", "PAUSED"}, "STOPPED"),
            "complete": ({"RUNNING"}, "COMPLETED"),
            "fail": ({"CREATED", "RUNNING", "PAUSED"}, "FAILED"),
            "auto_stop": ({"RUNNING", "PAUSED"}, "AUTO_STOPPED"),
        }
        if action not in transitions:
            raise ValueError("invalid_probe_transition")
        allowed_from, target = transitions[action]
        now = utcnow()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT status,revision FROM continuous_probe_runs
              WHERE probe_run_id=? AND tenant_id=? AND workspace_id=?""",
              (run_id, *self.scope.sql_parameters())).fetchone()
            if not row:
                raise KeyError("probe_run_not_found")
            if expected_revision is not None and int(row["revision"]) != int(expected_revision):
                raise RuntimeError("probe_revision_conflict")
            if str(row["status"]).upper() not in allowed_from:
                raise RuntimeError("invalid_probe_state_transition")
            finished = now if target in {"STOPPED", "COMPLETED", "FAILED", "AUTO_STOPPED"} else None
            stop_requested = 1 if target in {"STOPPED", "AUTO_STOPPED"} else 0
            cursor = db.execute("""UPDATE continuous_probe_runs SET status=?,desired_status=?,
              updated_at=?,finished_at=COALESCE(?,finished_at),stop_requested=?,revision=revision+1,
              pause_count=pause_count+?,last_error=CASE WHEN ?='FAILED' THEN ? ELSE last_error END
              WHERE probe_run_id=? AND tenant_id=? AND workspace_id=? AND revision=?""", (
                target, target, now, finished, stop_requested, 1 if target == "PAUSED" else 0,
                target, reason, run_id, *self.scope.sql_parameters(), int(row["revision"])))
            if cursor.rowcount != 1:
                raise RuntimeError("probe_revision_conflict")
        self.probe_event(run_id, f"probe_{action}", "local_control_event", {
            "from_status": row["status"], "to_status": target,
            "reason": reason, "operator_id": operator_id,
        })
        return self.probe_run(run_id)

    def clone_probe_run(self, run_id: str, name: str | None = None) -> dict[str, Any]:
        source = self.probe_run(run_id)
        return self.create_probe_run({
            "task_name": name or f"{source.get('task_name') or '探测任务'}（副本）",
            "environment_id": source["environment_id"],
            "git_commit": source.get("git_commit") or "unknown",
            "database_watermark": source.get("database_watermark") or 0,
            "configuration": source["configuration"],
            "cloned_from_probe_run_id": run_id,
        })

    def probe_events(self, run_id: str, *, after_event_id: int | None = None,
                     limit: int = 100) -> dict[str, Any]:
        self._probe_exists(run_id)
        limit = min(max(int(limit), 1), 500)
        clauses = ["probe_run_id=?", "tenant_id=?", "workspace_id=?"]
        parameters: list[Any] = [run_id, *self.scope.sql_parameters()]
        if after_event_id is not None:
            clauses.append("event_id>?")
            parameters.append(int(after_event_id))
        with self.connect() as db:
            rows = db.execute(f"""SELECT event_id,event_type,source_type,created_at,details_json
              FROM continuous_probe_events WHERE {' AND '.join(clauses)}
              ORDER BY event_id LIMIT ?""", (*parameters, limit)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json") or "{}")
            items.append(item)
        return {"items": items,
                "next_after_event_id": items[-1]["event_id"] if items else after_event_id}

    def _probe_exists(self, run_id: str) -> None:
        with self.connect() as db:
            row = db.execute("""SELECT 1 FROM continuous_probe_runs WHERE probe_run_id=?
              AND tenant_id=? AND workspace_id=?""",
              (run_id, *self.scope.sql_parameters())).fetchone()
        if not row:
            raise KeyError("probe_run_not_found")

    def _probe_log_rows(self, run_id: str) -> list[dict[str, Any]]:
        """Return only persisted probe traffic for the selected run."""
        self._probe_exists(run_id)
        with self.connect() as db:
            table = db.execute("""SELECT 1 FROM sqlite_master
              WHERE type='table' AND name='standardized_call_logs'""").fetchone()
            if not table:
                return []
            rows = db.execute("""SELECT cursor_id,record_id,occurred_at,request_id,
              local_request_id,provider_request_id,provider_response_id,provider_trace_id,
              response_id,decision_id,requested_model,actual_model,provider,stream,
              request_status,http_status,error_category,total_latency_ms,
              first_token_latency_ms,input_tokens,cached_input_tokens,output_tokens,
              cost_amount,currency,cost_status,cost_type,provider_cost_amount_exact,
              fallback_used,is_fault_injected,error_source,fault_id
              FROM standardized_call_logs WHERE tenant_id=? AND workspace_id=?
              AND probe_run_id=? AND traffic_class='probe'
              AND COALESCE(duplicate_status,'canonical')='canonical'
              ORDER BY occurred_at,cursor_id""",
              (*self.scope.sql_parameters(), run_id)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _log_success(row: dict[str, Any]) -> bool:
        status = str(row.get("request_status") or "").lower()
        http = row.get("http_status")
        return status in {"success", "succeeded", "completed", "ok"} or (
            isinstance(http, int) and 200 <= http < 300
        )

    def probe_requests(self, run_id: str, *, limit: int = 100,
                       offset: int = 0) -> dict[str, Any]:
        rows = self._probe_log_rows(run_id)
        limit = min(max(int(limit), 1), 500)
        offset = max(int(offset), 0)
        projected = []
        for row in rows[offset:offset + limit]:
            projected.append({
                "cursor_id": row["cursor_id"], "record_id": row["record_id"],
                "occurred_at": row["occurred_at"], "request_id": row["request_id"],
                "local_request_id": row["local_request_id"],
                "provider_request_id": row["provider_request_id"],
                "provider_response_id": row["provider_response_id"],
                "provider_trace_id": row["provider_trace_id"],
                "response_id": row["response_id"], "decision_id": row["decision_id"],
                "requested_model": row["requested_model"], "actual_model": row["actual_model"],
                "provider": row["provider"], "stream": bool(row["stream"]),
                "http_status": row["http_status"], "success": self._log_success(row),
                "error_category": row["error_category"],
                "latency_ms": row["total_latency_ms"],
                "first_token_latency_ms": row["first_token_latency_ms"],
                "input_tokens": row["input_tokens"],
                "cached_input_tokens": row["cached_input_tokens"],
                "output_tokens": row["output_tokens"],
                "actual_provider_cost": row["provider_cost_amount_exact"]
                    if row["cost_type"] == "provider_actual" else None,
                "estimated_versioned_price": row["cost_amount"]
                    if row["cost_type"] == "estimated_versioned_price" else None,
                "cost_status": row["cost_status"], "currency": row["currency"],
                "fallback_used": bool(row["fallback_used"]),
                "is_fault_injected": bool(row["is_fault_injected"]),
                "error_source": row["error_source"], "fault_id": row["fault_id"],
            })
        return {"items": projected, "total": len(rows), "limit": limit, "offset": offset}

    def probe_metrics(self, run_id: str) -> dict[str, Any]:
        run = self.probe_run(run_id, include_events=False)
        rows = self._probe_log_rows(run_id)
        natural = [row for row in rows if not bool(row.get("is_fault_injected"))]
        successful = [row for row in rows if self._log_success(row)]
        natural_success = [row for row in natural if self._log_success(row)]
        latencies = [row["total_latency_ms"] for row in rows
                     if row.get("total_latency_ms") is not None]
        actual_costs = [Decimal(str(row["provider_cost_amount_exact"])) for row in rows
                        if row.get("cost_type") == "provider_actual"
                        and row.get("provider_cost_amount_exact") is not None]
        estimated_costs = [Decimal(str(row["cost_amount"])) for row in rows
                           if row.get("cost_type") == "estimated_versioned_price"
                           and row.get("cost_amount") is not None]
        pending_cost_count = sum(
            row.get("cost_status") in {None, "pending_provider_sync"} for row in rows
        )
        minute_buckets: dict[str, dict[str, Any]] = {}
        model_buckets: dict[str, list[dict[str, Any]]] = {}
        error_counts: dict[str, int] = {}
        for row in rows:
            minute = str(row["occurred_at"])[:16] + ":00"
            bucket = minute_buckets.setdefault(minute, {
                "time": minute, "requests": 0, "successes": 0,
                "natural_requests": 0, "natural_successes": 0,
                "latencies": [], "input_tokens": 0, "cached_input_tokens": 0,
                "output_tokens": 0, "actual_provider_cost": Decimal("0"),
                "actual_cost_count": 0, "estimated_versioned_price": Decimal("0"),
                "estimated_cost_count": 0,
            })
            bucket["requests"] += 1
            bucket["successes"] += int(self._log_success(row))
            if not bool(row.get("is_fault_injected")):
                bucket["natural_requests"] += 1
                bucket["natural_successes"] += int(self._log_success(row))
            if row.get("total_latency_ms") is not None:
                bucket["latencies"].append(row["total_latency_ms"])
            for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                bucket[field] += int(row.get(field) or 0)
            if row.get("cost_type") == "provider_actual" and row.get("provider_cost_amount_exact"):
                bucket["actual_provider_cost"] += Decimal(str(row["provider_cost_amount_exact"]))
                bucket["actual_cost_count"] += 1
            if row.get("cost_type") == "estimated_versioned_price" and row.get("cost_amount"):
                bucket["estimated_versioned_price"] += Decimal(str(row["cost_amount"]))
                bucket["estimated_cost_count"] += 1
            model = row.get("actual_model") or row.get("requested_model") or "未记录"
            model_buckets.setdefault(str(model), []).append(row)
            if not self._log_success(row):
                if row.get("is_fault_injected"):
                    key = "uat_fault_injection"
                else:
                    key = row.get("error_category") or "provider_observed"
                error_counts[str(key)] = error_counts.get(str(key), 0) + 1
        request_series = []
        for bucket in minute_buckets.values():
            request_series.append({
                "time": bucket["time"], "request_count": bucket["requests"],
                "success_rate": bucket["successes"] / bucket["requests"] if bucket["requests"] else None,
                "natural_success_rate": bucket["natural_successes"] / bucket["natural_requests"]
                    if bucket["natural_requests"] else None,
                "p50_ms": percentile(bucket["latencies"], .5),
                "p95_ms": percentile(bucket["latencies"], .95),
                "p99_ms": percentile(bucket["latencies"], .99),
                "input_tokens": bucket["input_tokens"],
                "cached_input_tokens": bucket["cached_input_tokens"],
                "output_tokens": bucket["output_tokens"],
                "actual_provider_cost": str(bucket["actual_provider_cost"])
                    if bucket["actual_cost_count"] else None,
                "estimated_versioned_price": str(bucket["estimated_versioned_price"])
                    if bucket["estimated_cost_count"] else None,
            })
        models = []
        for model, model_rows in sorted(model_buckets.items()):
            model_latencies = [row["total_latency_ms"] for row in model_rows
                               if row.get("total_latency_ms") is not None]
            models.append({
                "model_id": model, "request_count": len(model_rows),
                "success_rate": sum(self._log_success(row) for row in model_rows) / len(model_rows),
                "p95_ms": percentile(model_latencies, .95),
                "input_tokens": sum(int(row.get("input_tokens") or 0) for row in model_rows),
                "cached_input_tokens": sum(int(row.get("cached_input_tokens") or 0) for row in model_rows),
                "output_tokens": sum(int(row.get("output_tokens") or 0) for row in model_rows),
                "actual_provider_cost": str(sum((Decimal(str(row["provider_cost_amount_exact"]))
                    for row in model_rows if row.get("cost_type") == "provider_actual"
                    and row.get("provider_cost_amount_exact") is not None), Decimal("0")))
                    if any(row.get("cost_type") == "provider_actual" and
                           row.get("provider_cost_amount_exact") is not None for row in model_rows) else None,
            })
        planned = run.get("planned_duration_seconds")
        return {
            "probe_run_id": run_id, "status": run["status"],
            "planned_duration_seconds": planned,
            "request_count": len(rows), "success_count": len(successful),
            "failure_count": len(rows) - len(successful),
            "natural_request_count": len(natural),
            "natural_success_rate": len(natural_success) / len(natural) if natural else None,
            "comprehensive_success_rate": len(successful) / len(rows) if rows else None,
            "p50_ms": percentile(latencies, .5), "p95_ms": percentile(latencies, .95),
            "p99_ms": percentile(latencies, .99),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in rows),
            "cached_input_tokens": sum(int(row.get("cached_input_tokens") or 0) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in rows),
            "actual_provider_cost": str(sum(actual_costs, Decimal("0"))) if actual_costs else None,
            "actual_cost_synced_count": len(actual_costs),
            "estimated_versioned_price": str(sum(estimated_costs, Decimal("0")))
                if estimated_costs else None,
            "estimated_cost_count": len(estimated_costs),
            "pending_provider_sync_count": pending_cost_count,
            "max_observed_concurrency": max((int(run.get("current_concurrency") or 0),), default=0),
            "request_series": request_series, "model_metrics": models,
            "error_composition": [{"category": key, "count": value}
                                  for key, value in sorted(error_counts.items())],
        }

    def latest_probe_run(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("""SELECT probe_run_id FROM continuous_probe_runs
              WHERE tenant_id=? AND workspace_id=? ORDER BY started_at DESC LIMIT 1""",
              self.scope.sql_parameters()).fetchone()
        return self.probe_run(row[0]) if row else None
