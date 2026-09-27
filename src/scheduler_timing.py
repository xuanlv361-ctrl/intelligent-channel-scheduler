"""Scheduler-only timing telemetry with provider latency explicitly excluded."""

from __future__ import annotations

import hashlib
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator


SCHEDULER_STAGES = (
    "candidate_loading",
    "metric_lookup",
    "eligibility_and_scoring",
    "decision_persistence",
    "audit_emission",
    "scheduler_total",
)

PERFORMANCE_STAGES = (
    "request_validation",
    "candidate_discovery",
    "capability_filter",
    "metrics_read",
    "constraint_filter",
    "scoring",
    "deterministic_sort",
    "fallback_plan",
    "decision_persistence",
    "audit_write",
    "scheduler_total",
)


class SchedulerTimingError(ValueError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


class SchedulerTimingRecorder:
    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = _utc_now):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _migrate(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS scheduler_timing_migrations(
              version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scheduler_timing_samples(
              sample_id TEXT PRIMARY KEY,
              decision_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              stage TEXT NOT NULL,
              duration_ms REAL NOT NULL,
              cold_start INTEGER NOT NULL,
              recorded_at TEXT NOT NULL,
              UNIQUE(decision_id, stage));
            CREATE INDEX IF NOT EXISTS idx_scheduler_timing_recorded
              ON scheduler_timing_samples(recorded_at,stage);
            """)
            db.execute(
                "INSERT OR IGNORE INTO scheduler_timing_migrations VALUES(1,?)",
                (self.clock().isoformat(),),
            )
            db.executescript("""
            CREATE TABLE IF NOT EXISTS scheduler_performance_spans(
              span_id TEXT NOT NULL,local_request_id TEXT NOT NULL,
              provider_request_id TEXT,decision_id TEXT,environment_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              model_id TEXT,traffic_class TEXT NOT NULL,
              stream INTEGER NOT NULL,strategy_id TEXT NOT NULL,stage TEXT NOT NULL,
              duration_ms REAL NOT NULL,started_at TEXT NOT NULL,finished_at TEXT NOT NULL,
              success INTEGER NOT NULL,error_category TEXT,configuration_version TEXT,
              metrics_snapshot_id TEXT,source_type TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,span_id),
              UNIQUE(tenant_id,workspace_id,local_request_id,stage));
            CREATE INDEX IF NOT EXISTS idx_scheduler_performance_scope
              ON scheduler_performance_spans(environment_id,traffic_class,created_at);
            CREATE TABLE IF NOT EXISTS scheduler_performance_requests(
              local_request_id TEXT NOT NULL,provider_request_id TEXT,decision_id TEXT,
              environment_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',strategy_id TEXT NOT NULL,
              model_id TEXT,traffic_class TEXT NOT NULL,stream INTEGER NOT NULL,
              candidate_count INTEGER,filtered_count INTEGER,scheduler_total_ms REAL NOT NULL,
              provider_latency_ms REAL,end_to_end_ms REAL NOT NULL,success INTEGER NOT NULL,
              error_category TEXT,configuration_version TEXT,metrics_snapshot_id TEXT,
              source_type TEXT NOT NULL,occurred_at TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,local_request_id));
            CREATE INDEX IF NOT EXISTS idx_scheduler_performance_request_scope
              ON scheduler_performance_requests(environment_id,traffic_class,occurred_at);
            """)
            for table in ("scheduler_performance_spans", "scheduler_performance_requests"):
                columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
                if "workspace_id" not in columns:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL "
                        "DEFAULT 'workspace_local_dev_v1'"
                    )
                db.execute(
                    f"CREATE INDEX IF NOT EXISTS ix_ent_scope_{table} "
                    f"ON {table}(tenant_id,workspace_id)"
                )

    def record_trace(
        self, *, local_request_id: str, decision_id: str | None,
        environment_id: str, tenant_id: str, strategy_id: str, model_id: str | None,
        traffic_class: str, stream: bool, stage_durations: dict[str, float],
        provider_latency_ms: float | None, end_to_end_ms: float, success: bool,
        provider_request_id: str | None = None, error_category: str | None = None,
        configuration_version: str | None = None, metrics_snapshot_id: str | None = None,
        candidate_count: int | None = None, filtered_count: int | None = None,
        source_type: str = "realtime_execution", started_at: str | None = None,
        workspace_id: str = "workspace_local_dev_v1",
    ) -> None:
        if not local_request_id or not environment_id or not tenant_id or not workspace_id:
            raise SchedulerTimingError("scheduler_performance_identity_required")
        unknown = set(stage_durations) - set(PERFORMANCE_STAGES)
        if unknown:
            raise SchedulerTimingError("unknown_scheduler_performance_stage")
        total = float(stage_durations.get("scheduler_total", 0.0))
        if total < 0 or end_to_end_ms < 0:
            raise SchedulerTimingError("scheduler_timing_duration_invalid")
        began = started_at or self.clock().isoformat()
        created = self.clock().isoformat()
        with self._connect() as db:
            db.execute("""INSERT INTO scheduler_performance_requests(
              local_request_id,provider_request_id,decision_id,environment_id,tenant_id,workspace_id,
              strategy_id,model_id,traffic_class,stream,candidate_count,filtered_count,
              scheduler_total_ms,provider_latency_ms,end_to_end_ms,success,error_category,
              configuration_version,metrics_snapshot_id,source_type,occurred_at,created_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(tenant_id,workspace_id,local_request_id) DO UPDATE SET
              provider_request_id=excluded.provider_request_id,provider_latency_ms=excluded.provider_latency_ms,
              end_to_end_ms=excluded.end_to_end_ms,success=excluded.success,
              error_category=excluded.error_category""",(
                local_request_id,provider_request_id,decision_id,environment_id,tenant_id,workspace_id,
                strategy_id or "specified_model",model_id,traffic_class,1 if stream else 0,
                candidate_count,filtered_count,total,provider_latency_ms,float(end_to_end_ms),
                1 if success else 0,error_category,configuration_version,metrics_snapshot_id,
                source_type,began,created))
            for stage,duration in stage_durations.items():
                value=float(duration)
                if not math.isfinite(value) or value < 0:
                    raise SchedulerTimingError("scheduler_timing_duration_invalid")
                identity=f"{local_request_id}\0{stage}".encode("utf-8")
                span_id="SPN-"+hashlib.sha256(identity).hexdigest()[:24].upper()
                db.execute("""INSERT INTO scheduler_performance_spans(
                  span_id,local_request_id,provider_request_id,decision_id,environment_id,
                  tenant_id,workspace_id,model_id,traffic_class,stream,strategy_id,stage,duration_ms,
                  started_at,finished_at,success,error_category,configuration_version,
                  metrics_snapshot_id,source_type,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(tenant_id,workspace_id,span_id) DO UPDATE SET provider_request_id=excluded.provider_request_id,
                  success=excluded.success,error_category=excluded.error_category""",(
                    span_id,local_request_id,provider_request_id,decision_id,environment_id,
                    tenant_id,workspace_id,model_id,traffic_class,1 if stream else 0,strategy_id or "specified_model",
                    stage,value,began,created,1 if success else 0,error_category,
                    configuration_version,metrics_snapshot_id,source_type,created))

    def record(
        self,
        *,
        decision_id: str,
        run_id: str,
        stage: str,
        duration_ms: float,
        cold_start: bool = False,
    ) -> None:
        if stage not in SCHEDULER_STAGES:
            raise SchedulerTimingError("unknown_scheduler_timing_stage")
        if not decision_id or not run_id:
            raise SchedulerTimingError("scheduler_timing_identity_required")
        if isinstance(duration_ms, bool) or not math.isfinite(duration_ms) or duration_ms < 0:
            raise SchedulerTimingError("scheduler_timing_duration_invalid")
        identity = f"{decision_id}\0{stage}".encode("utf-8")
        sample_id = "ST-" + hashlib.sha256(identity).hexdigest()[:24].upper()
        with self._connect() as db:
            existing = db.execute(
                "SELECT duration_ms,cold_start,run_id FROM scheduler_timing_samples WHERE sample_id=?",
                (sample_id,),
            ).fetchone()
            if existing:
                if (
                    abs(float(existing["duration_ms"]) - float(duration_ms)) > 1e-9
                    or bool(existing["cold_start"]) != bool(cold_start)
                    or existing["run_id"] != run_id
                ):
                    raise SchedulerTimingError("scheduler_timing_idempotency_conflict")
                return
            db.execute(
                """INSERT INTO scheduler_timing_samples(
                sample_id,decision_id,run_id,stage,duration_ms,cold_start,recorded_at)
                VALUES(?,?,?,?,?,?,?)""",
                (
                    sample_id,
                    decision_id,
                    run_id,
                    stage,
                    float(duration_ms),
                    1 if cold_start else 0,
                    self.clock().isoformat(),
                ),
            )

    def start_session(
        self,
        *,
        decision_id: str,
        run_id: str,
        cold_start: bool = False,
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> "SchedulerTimingSession":
        return SchedulerTimingSession(
            recorder=self,
            decision_id=decision_id,
            run_id=run_id,
            cold_start=cold_start,
            monotonic=monotonic,
        )

    def summary(self, *, window_minutes: int = 60) -> dict:
        if isinstance(window_minutes, bool) or window_minutes < 1 or window_minutes > 10080:
            raise SchedulerTimingError("scheduler_timing_window_invalid")
        cutoff = (self.clock() - timedelta(minutes=window_minutes)).isoformat()
        with self._connect() as db:
            rows = db.execute(
                "SELECT stage,duration_ms,cold_start FROM scheduler_timing_samples WHERE recorded_at>=?",
                (cutoff,),
            ).fetchall()
        by_stage: dict[str, list[float]] = {stage: [] for stage in SCHEDULER_STAGES}
        cold_count = 0
        for row in rows:
            by_stage[row["stage"]].append(float(row["duration_ms"]))
            cold_count += int(row["cold_start"])
        stages = []
        for stage in SCHEDULER_STAGES:
            values = by_stage[stage]
            stages.append({
                "stage": stage,
                "sample_count": len(values),
                "p50_ms": _percentile(values, 0.50),
                "p95_ms": _percentile(values, 0.95),
                "p99_ms": _percentile(values, 0.99),
                "max_ms": max(values) if values else None,
            })
        return {
            "status": "ready" if rows else "never_measured",
            "window_minutes": window_minutes,
            "sample_count": len(rows),
            "cold_start_sample_count": cold_count,
            "stages": stages,
            "provider_latency_included": False,
            "network_called": False,
        }


@dataclass
class SchedulerTimingSession:
    recorder: SchedulerTimingRecorder
    decision_id: str
    run_id: str
    cold_start: bool
    monotonic: Callable[[], float]
    started_at: float = field(init=False)
    measurements: dict[str, float] = field(default_factory=dict)
    finished: bool = False

    def __post_init__(self) -> None:
        self.started_at = self.monotonic()

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        if stage == "scheduler_total" or stage not in SCHEDULER_STAGES:
            raise SchedulerTimingError("invalid_measured_scheduler_stage")
        if self.finished:
            raise SchedulerTimingError("scheduler_timing_session_finished")
        start = self.monotonic()
        try:
            yield
        finally:
            duration = max(0.0, (self.monotonic() - start) * 1000.0)
            self.measurements[stage] = self.measurements.get(stage, 0.0) + duration

    def finish(self) -> None:
        if self.finished:
            return
        total_ms = max(0.0, (self.monotonic() - self.started_at) * 1000.0)
        for stage, duration_ms in self.measurements.items():
            self.recorder.record(
                decision_id=self.decision_id,
                run_id=self.run_id,
                stage=stage,
                duration_ms=duration_ms,
                cold_start=self.cold_start,
            )
        self.recorder.record(
            decision_id=self.decision_id,
            run_id=self.run_id,
            stage="scheduler_total",
            duration_ms=total_ms,
            cold_start=self.cold_start,
        )
        self.finished = True
