"""Business-facing rolling metrics over the canonical unified call ledger."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.incremental_metrics_service import WINDOWS
from backend.tenant_security import TenantScope


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("missing_timestamp")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    result = values[low] if low == high else values[low] + (values[high] - values[low]) * (position - low)
    return round(result, 3)


def _success(row: sqlite3.Row) -> bool:
    status = str(row["request_status"] or "").lower()
    if status:
        return status in {"success", "succeeded", "completed"}
    code = row["http_status"]
    return code is not None and 200 <= int(code) < 400


class DynamicMetricsService:
    _lock = threading.Lock()

    def __init__(self, path: Path, scope: TenantScope):
        self.path = Path(path)
        self.scope = scope
        self._init()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS dynamic_metric_jobs(
                  job_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  finished_at TEXT,
                  source_watermark TEXT,
                  event_count INTEGER NOT NULL DEFAULT 0,
                  snapshot_count INTEGER NOT NULL DEFAULT 0,
                  error_code TEXT,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,job_id)
                );
                CREATE TABLE IF NOT EXISTS dynamic_metric_state(
                  singleton_id INTEGER NOT NULL CHECK(singleton_id=1),
                  last_job_id TEXT,
                  last_started_at TEXT,
                  last_completed_at TEXT,
                  source_watermark TEXT,
                  event_count INTEGER NOT NULL DEFAULT 0,
                  snapshot_count INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'never_run',
                  error_code TEXT,
                  tenant_id TEXT NOT NULL,
                  workspace_id TEXT NOT NULL,
                  PRIMARY KEY(tenant_id,workspace_id,singleton_id)
                );
                """
            )
            db.execute(
                """INSERT OR IGNORE INTO dynamic_metric_state(
                     singleton_id,tenant_id,workspace_id) VALUES(1,?,?)""",
                self.scope.sql_parameters(),
            )

    def ensure(self, adapter: Any | None = None) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            with self.connect() as db:
                row = db.execute(
                    """SELECT * FROM dynamic_metric_state WHERE singleton_id=1
                       AND tenant_id=? AND workspace_id=?""",
                    self.scope.sql_parameters(),
                ).fetchone()
            return {"job_id": row["last_job_id"] if row else None, "status": "running", "already_running": True,
                    "watermark": row["source_watermark"] if row else None, "started_at": row["last_started_at"] if row else None}
        job_id = f"DMJ-{uuid.uuid4()}"
        started = datetime.now(timezone.utc).isoformat()
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """INSERT INTO dynamic_metric_jobs(job_id,status,started_at,tenant_id,workspace_id)
                       VALUES(?,'running',?,?,?)""", (job_id, started, *self.scope.sql_parameters()))
                db.execute(
                    """UPDATE dynamic_metric_state SET last_job_id=?,last_started_at=?,status='running',error_code=NULL
                       WHERE singleton_id=1 AND tenant_id=? AND workspace_id=?""",
                    (job_id, started, *self.scope.sql_parameters()))
            if adapter is not None:
                adapter.synchronize_safely()
            with self.connect() as db:
                source = db.execute(
                    """SELECT COUNT(*) count,MAX(occurred_at) watermark FROM standardized_call_logs
                       WHERE tenant_id=? AND workspace_id=? AND duplicate_of IS NULL""",
                    self.scope.sql_parameters()).fetchone()
                snapshots = db.execute(
                    """SELECT COUNT(*) FROM metric_snapshots WHERE tenant_id=? AND workspace_id=?""",
                    self.scope.sql_parameters()).fetchone()[0]
                finished = datetime.now(timezone.utc).isoformat()
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """UPDATE dynamic_metric_jobs SET status='ready',finished_at=?,source_watermark=?,
                       event_count=?,snapshot_count=? WHERE job_id=? AND tenant_id=? AND workspace_id=?""",
                    (finished, source["watermark"], source["count"], snapshots, job_id, *self.scope.sql_parameters()))
                db.execute(
                    """UPDATE dynamic_metric_state SET last_completed_at=?,source_watermark=?,event_count=?,
                       snapshot_count=?,status='ready',error_code=NULL WHERE singleton_id=1
                       AND tenant_id=? AND workspace_id=?""",
                    (finished, source["watermark"], source["count"], snapshots, *self.scope.sql_parameters()))
            return {"job_id": job_id, "status": "ready", "already_running": False,
                    "watermark": source["watermark"], "started_at": started}
        except Exception:
            finished = datetime.now(timezone.utc).isoformat()
            with self.connect() as db:
                db.execute("UPDATE dynamic_metric_jobs SET status='failed',finished_at=?,error_code='dynamic_metrics_refresh_failed' WHERE job_id=? AND tenant_id=? AND workspace_id=?",
                           (finished, job_id, *self.scope.sql_parameters()))
                db.execute("UPDATE dynamic_metric_state SET last_completed_at=?,status='failed',error_code='dynamic_metrics_refresh_failed' WHERE singleton_id=1 AND tenant_id=? AND workspace_id=?",
                           (finished, *self.scope.sql_parameters()))
            raise
        finally:
            self._lock.release()

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM dynamic_metric_jobs WHERE job_id=? AND tenant_id=? AND workspace_id=?",
                             (job_id, *self.scope.sql_parameters())).fetchone()
        return dict(row) if row else None

    def overview(self, *, environment_id: str = "china_uat", window: str = "24h",
                 model_id: str | None = None, traffic_class: str = "business",
                 source_type: str | None = None) -> dict[str, Any]:
        if window not in WINDOWS:
            raise ValueError("invalid_metric_window")
        if traffic_class not in {"business", "probe", "all"}:
            raise ValueError("invalid_traffic_class")
        now = datetime.now(timezone.utc)
        start = now - timedelta(seconds=WINDOWS[window])
        # Do not compare heterogeneous legacy ISO strings lexically. Normalize
        # UTC, +08:00 and documented naive-UTC rows before applying windows.
        clauses = ["tenant_id=?", "workspace_id=?", "duplicate_of IS NULL"]
        values: list[Any] = [*self.scope.sql_parameters()]
        if environment_id and environment_id != "all": clauses.append("environment_id=?"); values.append(environment_id)
        if model_id: clauses.append("COALESCE(actual_model,requested_model)=?"); values.append(model_id)
        if traffic_class != "all": clauses.append("traffic_class=?"); values.append(traffic_class)
        if source_type: clauses.append("source_type=?"); values.append(source_type)
        with self.connect() as db:
            candidate_rows = db.execute(f"SELECT * FROM standardized_call_logs WHERE {' AND '.join(clauses)} ORDER BY occurred_at", values).fetchall()
            rows = [row for row in candidate_rows if start < _utc(row["occurred_at"]) <= now]
            state = db.execute("SELECT * FROM dynamic_metric_state WHERE singleton_id=1 AND tenant_id=? AND workspace_id=?",
                               self.scope.sql_parameters()).fetchone()
            snapshot_rows = db.execute(
                """SELECT snapshot_id,model,channel,data_state,sample_count,last_evidence_at,
                          evidence_age_seconds,metrics_json FROM metric_snapshots
                   WHERE tenant_id=? AND workspace_id=? AND window_name=? AND environment_id=?""",
                (*self.scope.sql_parameters(), window, environment_id)).fetchall()
        bucket_seconds = {"5m": 60, "1h": 300, "24h": 3600}[window]
        buckets: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            dt = _utc(row["occurred_at"])
            epoch = int(dt.timestamp()) // bucket_seconds * bucket_seconds
            key = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
            buckets.setdefault(key, []).append(row)
        def nums(items: list[sqlite3.Row], field: str) -> list[float]:
            return [float(r[field]) for r in items if r[field] is not None]
        def tokens(items: list[sqlite3.Row], field: str) -> int:
            return sum(int(r[field] or 0) for r in items)
        request_series=[]; latency_series=[]; token_series=[]; cost_series=[]
        for at, items in sorted(buckets.items()):
            actual = [float(r["provider_cost_amount_exact"] or r["cost_amount"]) for r in items
                      if r["cost_type"] in {"actual_provider_cost","provider_actual"} and (r["provider_cost_amount_exact"] is not None or r["cost_amount"] is not None)]
            estimated = [float(r["cost_amount"]) for r in items if r["cost_type"] == "estimated_versioned_price" and r["cost_amount"] is not None]
            lat = nums(items,"total_latency_ms"); ttft=nums(items,"first_token_latency_ms")
            request_series.append({"at":at,"requests":len(items),"success_rate":sum(_success(r) for r in items)/len(items)})
            latency_series.append({"at":at,"p50":_percentile(lat,.5),"p95":_percentile(lat,.95),"p99":_percentile(lat,.99),"ttft_p95":_percentile(ttft,.95)})
            token_series.append({"at":at,"input":tokens(items,"input_tokens"),"cached":tokens(items,"cached_input_tokens"),"output":tokens(items,"output_tokens")})
            cost_series.append({"at":at,"actual":round(sum(actual),8) if actual else None,"estimated":round(sum(estimated),8) if estimated else None,"pending":sum(r["cost_status"]=="pending_provider_sync" for r in items)})
        models: dict[str,list[sqlite3.Row]]={}
        for row in rows: models.setdefault(str(row["actual_model"] or row["requested_model"] or "未记录模型"),[]).append(row)
        model_metrics=[]
        for model, items in models.items():
            lat=nums(items,"total_latency_ms"); successes=sum(_success(r) for r in items); n=len(items)
            latest=max(_utc(r["occurred_at"]) for r in items); age=max(0,(now-latest).total_seconds())
            p=successes/n; margin=1.96*math.sqrt(max(p*(1-p)/n,0))
            actual=[float(r["provider_cost_amount_exact"] or r["cost_amount"]) for r in items if r["cost_type"] in {"actual_provider_cost","provider_actual"} and (r["provider_cost_amount_exact"] is not None or r["cost_amount"] is not None)]
            # Scheduler freshness is intentionally stricter than the selected
            # visualization window: a 24h trend can be visible yet too old for
            # a live routing decision.
            if age>3600: status="stale"
            elif n<5: status="insufficient_sample"
            else: status="healthy"
            model_metrics.append({"model":model,"request_count":n,"success_rate":p,"p95_ms":_percentile(lat,.95),
                "total_tokens":tokens(items,"input_tokens")+tokens(items,"cached_input_tokens")+tokens(items,"output_tokens"),
                "actual_cost":round(sum(actual),8) if actual else None,"pending_cost_count":sum(r["cost_status"]=="pending_provider_sync" for r in items),
                "last_sample_at":latest.isoformat(),"age_seconds":round(age,1),"confidence":{"lower":max(0,p-margin),"upper":min(1,p+margin),"sample_size":n},
                "status":status,"eligible":status=="healthy"})
        model_metrics.sort(key=lambda x:(not x["eligible"],-x["request_count"],x["model"]))
        all_latency=nums(rows,"total_latency_ms"); actual_all=[float(r["provider_cost_amount_exact"] or r["cost_amount"]) for r in rows if r["cost_type"] in {"actual_provider_cost","provider_actual"} and (r["provider_cost_amount_exact"] is not None or r["cost_amount"] is not None)]
        estimated_all=[float(r["cost_amount"]) for r in rows if r["cost_type"]=="estimated_versioned_price" and r["cost_amount"] is not None]
        state_counts={key:sum(item["status"]==key for item in model_metrics) for key in ("healthy","insufficient_sample","stale","blocked","unknown")}
        return {
            "update_status": {"status": state["status"] if state else "never_run", "last_aggregated_at":state["last_completed_at"] if state else None,
                              "source_watermark":state["source_watermark"] if state else None,"next_refresh_at":(now+timedelta(seconds=60)).isoformat()},
            "scope":{"environment_id":environment_id,"window":window,"traffic_class":traffic_class,"source_type":source_type,"timezone":"UTC","display_timezone":"UTC+8"},
            "kpis":{"request_count":len(rows),"success_rate":sum(_success(r) for r in rows)/len(rows) if rows else None,
                    "p50_ms":_percentile(all_latency,.5),"p95_ms":_percentile(all_latency,.95),"p99_ms":_percentile(all_latency,.99),
                    "total_tokens":tokens(rows,"input_tokens")+tokens(rows,"cached_input_tokens")+tokens(rows,"output_tokens"),
                    "actual_cost":round(sum(actual_all),8) if actual_all else None,"estimated_cost":round(sum(estimated_all),8) if estimated_all else None,
                    "pending_cost_count":sum(r["cost_status"]=="pending_provider_sync" for r in rows),"eligible_model_count":state_counts["healthy"]},
            "request_series":request_series,"latency_series":latency_series,"token_series":token_series,"cost_series":cost_series,
            "model_metrics":model_metrics,
            "freshness":[{"model":m["model"],"last_sample_at":m["last_sample_at"],"age_seconds":m["age_seconds"],"status":m["status"]} for m in model_metrics],
            "confidence":[{"model":m["model"],"sample_size":m["request_count"],"success_rate":m["success_rate"],**m["confidence"],"eligible":m["eligible"]} for m in model_metrics],
            "snapshot_status":state_counts,
            "coverage":{"event_count":len(rows),"model_count":len(model_metrics),"latency_count":len(all_latency),"actual_cost_count":len(actual_all),"estimated_cost_count":len(estimated_all)},
            "technical_metadata":{"aggregation_version":"dynamic_metrics_v2","source_contract":"standardized_call_logs/canonical_only",
                "source_watermark":state["source_watermark"] if state else None,"legacy_snapshot_count":len(snapshot_rows),
                "snapshot_ids":[r["snapshot_id"] for r in snapshot_rows],"configuration_version":next((r["configuration_version"] for r in reversed(rows) if r["configuration_version"]),None)},
        }
