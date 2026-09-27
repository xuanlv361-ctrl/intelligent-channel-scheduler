"""Managed, durable execution for bounded China-UAT probe runs.

The control-plane owns run creation and identifiers.  This module owns only the
execution lease and the real request loop.  It deliberately does not resume a
stale billable run after process restart: reconciliation records an explicit
interruption so an operator can clone/restart it with a new run id.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.call_log_service import CallLogService
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from backend.live_acceptance_service import percentile
from backend.persistent_credential_vault import (
    CredentialScope,
    PersistentCredentialVault,
)
from backend.price_catalog_service import PriceCatalogService
from backend.tenant_security import (
    DEFAULT_DEVELOPMENT_TENANT_ID,
    DEFAULT_DEVELOPMENT_WORKSPACE_ID,
    TenantScope,
)
from backend.uat_http_workbench_service import default_transport, execute_workbench
from backend.uat_service import UatSettings


DEFAULT_MODELS = (
    "kimi-k3",
    "deepseek-v4-flash",
    "glm-5.2",
    "kimi-k2.7-code",
    "kimi-k2.6",
)
ROOT = Path(__file__).resolve().parents[1]
TERMINAL_STATUSES = {"COMPLETED", "STOPPED", "AUTO_STOPPED", "FAILED"}
RUNNABLE_STATUSES = {"CREATED", "PAUSED"}


class ContinuousProbeExecutionError(RuntimeError):
    """Stable control-plane error; routes should expose ``code`` after translation."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProbeExecutionConfig:
    environment_id: str
    endpoint: str
    method: str
    models: tuple[str, ...]
    stream: bool
    prompt: str
    duration_seconds: int
    interval_seconds: float
    max_concurrency: int
    timeout_seconds: int
    max_tokens: int
    temperature: float
    max_requests: int | None
    stop_thresholds: dict[str, float | int]
    cooldown_seconds: float
    price_policy_path: str | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProbeExecutionConfig":
        environment = str(value.get("environment_id") or "china_uat").strip()
        if environment != "china_uat":
            raise ContinuousProbeExecutionError("probe_environment_not_allowed")
        endpoint = str(value.get("endpoint") or "/v1/chat/completions").strip()
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ContinuousProbeExecutionError("probe_endpoint_invalid")
        method = str(value.get("method") or "POST").upper().strip()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
            raise ContinuousProbeExecutionError("probe_method_invalid")
        models = tuple(dict.fromkeys(str(item).strip() for item in
                                    (value.get("models") or DEFAULT_MODELS)
                                    if str(item).strip()))
        if not models:
            raise ContinuousProbeExecutionError("probe_models_required")
        duration_raw = value.get("duration_seconds")
        duration = int(60 if duration_raw is None else duration_raw)
        interval_raw = value.get("interval_seconds")
        interval = float(5.0 if interval_raw is None else interval_raw)
        concurrency = int(value.get("max_concurrency") or 1)
        timeout = int(value.get("timeout_seconds") or 30)
        max_tokens = int(value.get("max_tokens") or 16)
        maximum = value.get("max_requests")
        maximum = int(maximum) if maximum not in (None, "") else None
        if not 1 <= duration <= 24 * 60 * 60:
            raise ContinuousProbeExecutionError("probe_duration_out_of_range")
        if not 0.25 <= interval <= 3600:
            raise ContinuousProbeExecutionError("probe_interval_out_of_range")
        if not 1 <= concurrency <= 16:
            raise ContinuousProbeExecutionError("probe_concurrency_out_of_range")
        if not 1 <= timeout <= 300 or not 1 <= max_tokens <= 4096:
            raise ContinuousProbeExecutionError("probe_request_limit_out_of_range")
        if maximum is not None and not 1 <= maximum <= 100_000:
            raise ContinuousProbeExecutionError("probe_max_requests_out_of_range")
        prompt = str(value.get("prompt") or "请只回答：OK").strip()
        if not prompt or len(prompt) > 8_000:
            raise ContinuousProbeExecutionError("probe_prompt_invalid")
        thresholds = {
            "consecutive_failures": 5,
            "success_rate": 0.90,
            "rate_429": 0.10,
            "rate_5xx": 0.05,
            "timeout_rate": 0.05,
            "p95_ms": 20_000,
            **dict(value.get("stop_thresholds") or {}),
        }
        return cls(
            environment_id=environment,
            endpoint=endpoint,
            method=method,
            models=models,
            stream=bool(value.get("stream", False)),
            prompt=prompt,
            duration_seconds=duration,
            interval_seconds=interval,
            max_concurrency=concurrency,
            timeout_seconds=timeout,
            max_tokens=max_tokens,
            temperature=float(value.get("temperature", 0)),
            max_requests=maximum,
            stop_thresholds=thresholds,
            cooldown_seconds=max(1.0, min(float(value.get("cooldown_seconds") or 10), 600.0)),
            price_policy_path=(str(value["price_policy_path"])
                               if value.get("price_policy_path") else None),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "endpoint": self.endpoint,
            "method": self.method,
            "models": list(self.models),
            "stream": self.stream,
            "prompt": self.prompt,
            "duration_seconds": self.duration_seconds,
            "interval_seconds": self.interval_seconds,
            "max_concurrency": self.max_concurrency,
            "timeout_seconds": self.timeout_seconds,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "max_requests": self.max_requests,
            "stop_thresholds": self.stop_thresholds,
            "cooldown_seconds": self.cooldown_seconds,
        }


class ContinuousProbeExecutionService:
    """Owns real probe worker threads and their durable execution leases."""

    def __init__(
        self,
        database_path: str | Path,
        scope: TenantScope | None = None,
        *,
        credential_provider: Callable[[], str] | None = None,
        request_executor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        environment_guard: Callable[[str], bool] | None = None,
        transport: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        lease_seconds: int = 30,
    ) -> None:
        self.path = Path(database_path)
        self.scope = scope or TenantScope.local_development()
        self._credential_provider = credential_provider or self._default_credential
        self._external_executor = request_executor
        self._environment_guard = environment_guard or self._default_environment_guard
        self._transport = transport or default_transport
        self._clock = clock
        self._sleep = sleeper
        self._lease_seconds = max(10, int(lease_seconds))
        self._worker_id = "PRW-" + uuid.uuid4().hex.upper()
        self._workers: dict[str, threading.Thread] = {}
        self._worker_lock = threading.RLock()
        self._shutdown = threading.Event()
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _migrate(self) -> None:
        columns = {
            "execution_worker_id": "TEXT",
            "execution_lease_expires_at": "TEXT",
            "execution_heartbeat_at": "TEXT",
            "interruption_reason": "TEXT",
            "max_concurrency_observed": "INTEGER NOT NULL DEFAULT 0",
        }
        with self.connect() as db:
            existing = {str(row[1]) for row in db.execute(
                "PRAGMA table_info(continuous_probe_runs)")}
            if not existing:
                raise ContinuousProbeExecutionError("continuous_probe_schema_missing")
            for name, declaration in columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE continuous_probe_runs ADD COLUMN {name} {declaration}")

    def _default_credential(self) -> str:
        credential_scope = CredentialScope(
            "windows-dev:lx",
            DEFAULT_DEVELOPMENT_TENANT_ID,
            DEFAULT_DEVELOPMENT_WORKSPACE_ID,
            "china_uat",
        )
        loaded = PersistentCredentialVault().load(credential_scope, required=True)
        if not loaded:
            raise ContinuousProbeExecutionError("uat_secure_credential_unavailable")
        return loaded[0]

    def _default_environment_guard(self, environment_id: str) -> bool:
        state = EnvironmentRuntimeSettings(self.path).execution_state(environment_id)
        return bool(state and state.get("enabled"))

    def _run_row(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM continuous_probe_runs WHERE probe_run_id=?
                   AND tenant_id=? AND workspace_id=?""",
                (run_id, *self.scope.sql_parameters()),
            ).fetchone()
        if not row:
            raise ContinuousProbeExecutionError("probe_run_not_found")
        return dict(row)

    def _event(self, run_id: str, event_type: str, source_type: str,
               details: Mapping[str, Any]) -> None:
        safe_details = {key: value for key, value in dict(details).items()
                        if key.casefold() not in {"authorization", "api_key", "cookie", "secret"}}
        with self.connect() as db:
            db.execute(
                """INSERT INTO continuous_probe_events(
                   probe_run_id,event_type,source_type,created_at,details_json,
                   tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)""",
                (run_id, event_type, source_type, utcnow(),
                 json.dumps(safe_details, ensure_ascii=False, sort_keys=True),
                 *self.scope.sql_parameters()),
            )

    def _update(self, run_id: str, **changes: Any) -> None:
        if not changes:
            return
        assignments = ",".join(f"{key}=?" for key in changes)
        with self.connect() as db:
            db.execute(
                f"""UPDATE continuous_probe_runs SET {assignments}
                    WHERE probe_run_id=? AND tenant_id=? AND workspace_id=?""",
                (*changes.values(), run_id, *self.scope.sql_parameters()),
            )

    def _claim(self, run_id: str) -> bool:
        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=self._lease_seconds)).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT status,execution_lease_expires_at FROM continuous_probe_runs
                   WHERE probe_run_id=? AND tenant_id=? AND workspace_id=?""",
                (run_id, *self.scope.sql_parameters()),
            ).fetchone()
            if not row:
                raise ContinuousProbeExecutionError("probe_run_not_found")
            active_lease = (_parse_utc(row["execution_lease_expires_at"])
                            if row["execution_lease_expires_at"] else None)
            if str(row["status"]) not in RUNNABLE_STATUSES or (
                active_lease and active_lease > now
            ):
                return False
            changed = db.execute(
                """UPDATE continuous_probe_runs SET status='RUNNING',
                   execution_worker_id=?,execution_heartbeat_at=?,execution_lease_expires_at=?,
                   interruption_reason=NULL,stop_requested=0
                   WHERE probe_run_id=? AND tenant_id=? AND workspace_id=?
                   AND status IN ('CREATED','PAUSED')""",
                (self._worker_id, now.isoformat(), lease_until, run_id,
                 *self.scope.sql_parameters()),
            ).rowcount
        return changed == 1

    def _heartbeat(self, run_id: str) -> None:
        now = datetime.now(timezone.utc)
        lease = (now + timedelta(seconds=self._lease_seconds)).isoformat()
        with self.connect() as db:
            changed = db.execute(
                """UPDATE continuous_probe_runs SET execution_heartbeat_at=?,
                   execution_lease_expires_at=? WHERE probe_run_id=?
                   AND tenant_id=? AND workspace_id=? AND execution_worker_id=?""",
                (now.isoformat(), lease, run_id, *self.scope.sql_parameters(),
                 self._worker_id),
            ).rowcount
        if changed != 1:
            raise ContinuousProbeExecutionError("probe_worker_lease_lost")

    def start(self, run_id: str, configuration: Mapping[str, Any], *,
              actor_id: str = "local-operator",
              wait_for_ownership_seconds: float = 2.0) -> dict[str, Any]:
        config = ProbeExecutionConfig.from_mapping(configuration)
        if not self._environment_guard(config.environment_id):
            raise ContinuousProbeExecutionError("china_uat_environment_disabled")
        # Decrypt before claiming: a run must not appear RUNNING without a usable key.
        credential = self._credential_provider()
        if not credential:
            raise ContinuousProbeExecutionError("uat_secure_credential_unavailable")
        with self._worker_lock:
            existing = self._workers.get(run_id)
            if existing and existing.is_alive():
                return self.status(run_id)
            if not self._claim(run_id):
                raise ContinuousProbeExecutionError("probe_run_not_startable")
            started = threading.Event()
            worker = threading.Thread(
                target=self._run,
                name=f"continuous-probe-{run_id[-12:]}",
                args=(run_id, config, credential, started),
                daemon=True,
            )
            self._workers[run_id] = worker
            worker.start()
        if not started.wait(max(0.1, wait_for_ownership_seconds)):
            self._update(run_id, status="FAILED", finished_at=utcnow(),
                         interruption_reason="probe_worker_start_timeout",
                         execution_lease_expires_at=utcnow())
            raise ContinuousProbeExecutionError("probe_worker_start_timeout")
        self._event(run_id, "probe_started", "local_control_event", {
            "actor_id": actor_id,
            "worker_id": self._worker_id,
            "duration_seconds": config.duration_seconds,
            "max_concurrency": config.max_concurrency,
        })
        return self.status(run_id)

    def pause(self, run_id: str, *, actor_id: str = "local-operator") -> dict[str, Any]:
        row = self._run_row(run_id)
        if row["status"] != "RUNNING":
            raise ContinuousProbeExecutionError("probe_pause_invalid_state")
        self._update(run_id, status="PAUSED", pause_count=int(row["pause_count"] or 0) + 1)
        self._event(run_id, "probe_paused_by_operator", "local_control_event",
                    {"actor_id": actor_id})
        return self.status(run_id)

    def resume(self, run_id: str, *, actor_id: str = "local-operator") -> dict[str, Any]:
        row = self._run_row(run_id)
        if row["status"] != "PAUSED":
            raise ContinuousProbeExecutionError("probe_resume_invalid_state")
        with self._worker_lock:
            worker = self._workers.get(run_id)
            if not worker or not worker.is_alive():
                raise ContinuousProbeExecutionError("probe_worker_not_available_after_restart")
        self._update(run_id, status="RUNNING")
        self._event(run_id, "probe_resumed_by_operator", "local_control_event",
                    {"actor_id": actor_id})
        return self.status(run_id)

    def stop(self, run_id: str, *, actor_id: str = "local-operator",
             wait_seconds: float = 0) -> dict[str, Any]:
        row = self._run_row(run_id)
        if row["status"] in TERMINAL_STATUSES:
            return self.status(run_id)
        self._update(run_id, stop_requested=1)
        self._event(run_id, "probe_stop_requested", "local_control_event",
                    {"actor_id": actor_id})
        if wait_seconds > 0:
            with self._worker_lock:
                worker = self._workers.get(run_id)
            if worker:
                worker.join(wait_seconds)
        return self.status(run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        row = self._run_row(run_id)
        return {
            "probe_run_id": run_id,
            "status": row["status"],
            "worker_owned": row.get("execution_worker_id") == self._worker_id,
            "worker_id": row.get("execution_worker_id"),
            "heartbeat_at": row.get("execution_heartbeat_at"),
            "lease_expires_at": row.get("execution_lease_expires_at"),
            "sent_count": int(row.get("sent_count") or 0),
            "completed_count": int(row.get("completed_count") or 0),
            "max_concurrency_observed": int(row.get("max_concurrency_observed") or 0),
            "stop_requested": bool(row.get("stop_requested")),
            "interruption_reason": row.get("interruption_reason"),
        }

    def reconcile(self, *, stale_after_seconds: int = 30) -> dict[str, Any]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, stale_after_seconds))
        interrupted: list[str] = []
        with self.connect() as db:
            rows = db.execute(
                """SELECT probe_run_id,status,execution_heartbeat_at FROM continuous_probe_runs
                   WHERE tenant_id=? AND workspace_id=? AND status IN ('RUNNING','PAUSED')""",
                self.scope.sql_parameters(),
            ).fetchall()
            for row in rows:
                heartbeat = _parse_utc(row["execution_heartbeat_at"])
                with self._worker_lock:
                    local = self._workers.get(str(row["probe_run_id"]))
                if local and local.is_alive():
                    continue
                if heartbeat is None or heartbeat < cutoff:
                    run_id = str(row["probe_run_id"])
                    db.execute(
                        """UPDATE continuous_probe_runs SET status='FAILED',finished_at=?,
                           interruption_reason='worker_restart_interrupted',
                           execution_lease_expires_at=? WHERE probe_run_id=?
                           AND tenant_id=? AND workspace_id=?""",
                        (utcnow(), utcnow(), run_id, *self.scope.sql_parameters()),
                    )
                    interrupted.append(run_id)
        for run_id in interrupted:
            self._event(run_id, "probe_interrupted_after_restart", "local_control_event",
                        {"reason": "worker_restart_interrupted"})
        return {"interrupted_count": len(interrupted), "probe_run_ids": interrupted}

    def shutdown(self, timeout_seconds: float = 10) -> dict[str, Any]:
        self._shutdown.set()
        with self._worker_lock:
            workers = list(self._workers.items())
        deadline = self._clock() + max(0, timeout_seconds)
        for _run_id, worker in workers:
            worker.join(max(0, deadline - self._clock()))
        alive = [run_id for run_id, worker in workers if worker.is_alive()]
        return {"stopped": not alive, "active_probe_run_ids": alive}

    def _request_body(self, run_id: str, config: ProbeExecutionConfig,
                      model: str) -> dict[str, Any]:
        value: dict[str, Any] | None = None
        if config.method in {"POST", "PUT", "PATCH"}:
            value = {
                "model": model,
                "messages": [{"role": "user", "content": config.prompt}],
                "stream": config.stream,
                "max_tokens": config.max_tokens,
                "temperature": config.temperature,
            }
        return {
            "method": config.method,
            "environment_id": config.environment_id,
            "path": config.endpoint,
            "query_params": [],
            "headers": [],
            "auth": {"method": "bearer", "header_name": "Authorization", "prefix": "Bearer"},
            "body": {"type": "json", "value": value} if value is not None else None,
            "content_type": "application/json",
            "timeout_seconds": config.timeout_seconds,
            "stream": config.stream,
            "model": model,
            "model_selection_mode": "specified",
            "routing_policy": "continuous_probe",
            "request_type": "text",
            "probe_run_id": run_id,
            "traffic_class": "probe",
            "_strategy_variant": "continuous_probe",
        }

    def _real_execute(self, body: dict[str, Any], credential: str,
                      config: ProbeExecutionConfig) -> dict[str, Any]:
        settings = replace(UatSettings.load(config.environment_id), enabled=True,
                           timeout_seconds=config.timeout_seconds)
        logs = CallLogService(self.path, self.scope)
        return execute_workbench(
            body, settings, credential, connection_verified=True,
            call_logs=logs, transport=self._transport,
        )

    def _execute_one(self, body: dict[str, Any], credential: str,
                     config: ProbeExecutionConfig) -> dict[str, Any]:
        try:
            result = (self._external_executor(body) if self._external_executor
                      else self._real_execute(body, credential, config))
            if not isinstance(result, dict):
                raise RuntimeError("probe_executor_invalid_result")
            return result
        except Exception as exc:  # evidence is safe; never include request/key
            return {
                "execution_status": "failed",
                "request_id": None,
                "decision_id": None,
                "http_status": None,
                "total_latency_ms": None,
                "error_source": "provider_live",
                "is_fault_injected": False,
                "error": {"error_category": type(exc).__name__},
            }

    def _finish_future(self, run_id: str, result: dict[str, Any],
                       counters: dict[str, Any]) -> None:
        counters["completed"] += 1
        successful = result.get("execution_status") == "success"
        counters["success" if successful else "failure"] += 1
        counters["consecutive_failures"] = 0 if successful else counters["consecutive_failures"] + 1
        latency = result.get("total_latency_ms")
        if isinstance(latency, (int, float)):
            counters["latencies"].append(float(latency))
        status = int(result.get("http_status") or 0)
        if status == 429:
            counters["status_429"] += 1
        if status >= 500:
            counters["status_5xx"] += 1
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        timed_out = error.get("error_category") in {"network_timeout", "upstream_timeout"}
        if timed_out:
            counters["timeouts"] += 1
        injected = bool(result.get("is_fault_injected"))
        source = "uat_fault_injection" if injected else str(result.get("error_source") or "provider_live")
        if not successful:
            counters["uat_injected_failures" if injected else "provider_live_failures"] += 1
        counters["recent"].append({
            "at": self._clock(), "success": successful, "status": status,
            "timeout": timed_out, "is_fault_injected": injected,
        })
        self._event(run_id, "probe_result", source, {
            "request_id": result.get("request_id"),
            "decision_id": result.get("decision_id"),
            "model": result.get("requested_model"),
            "http_status": result.get("http_status"),
            "success": successful,
            "latency_ms": latency,
            "is_fault_injected": injected,
            "cost_status": ("actual_provider_cost" if result.get("cost_amount") is not None
                            else "pending_provider_sync"),
        })

    def _summary(self, run_id: str, counters: dict[str, Any],
                 config: ProbeExecutionConfig, elapsed: float) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        with self.connect() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='standardized_call_logs'"
            ).fetchone()
            if table:
                rows = [dict(row) for row in db.execute(
                    """SELECT request_status,total_latency_ms,input_tokens,cached_input_tokens,
                       output_tokens,cost_amount,cost_type,error_source,is_fault_injected,
                       requested_model FROM standardized_call_logs WHERE probe_run_id=?
                       AND tenant_id=? AND workspace_id=? ORDER BY cursor_id""",
                    (run_id, *self.scope.sql_parameters()),
                )]
        latencies = [float(row["total_latency_ms"]) for row in rows
                     if row.get("total_latency_ms") is not None] or counters["latencies"]
        actual = [Decimal(str(row["cost_amount"])) for row in rows
                  if row.get("cost_amount") is not None and
                  row.get("cost_type") in {"provider_actual", "actual_provider_cost"}]
        estimates: list[Decimal] = []
        price_version: str | None = None
        try:
            policy = (Path(config.price_policy_path) if config.price_policy_path
                      else ROOT / "config" / "price_sync_policy_v1.json")
            price_service = PriceCatalogService(self.path, policy, development_mode=True)
            price_status = price_service.model_catalog_status()
            price_version = price_status.get("price_version")
            prices = {str(item["model_id"]): item
                      for item in price_service.list_model_prices()}
            for row in rows:
                price = prices.get(str(row.get("requested_model") or ""))
                inp, cached, out = (row.get("input_tokens"),
                                    row.get("cached_input_tokens") or 0,
                                    row.get("output_tokens"))
                if (not price or price.get("verification_status") != "confirmed" or
                    inp is None or out is None or
                    price.get("input_price_per_million_tokens") is None or
                    price.get("output_price_per_million_tokens") is None):
                    continue
                input_price = Decimal(str(price["input_price_per_million_tokens"]))
                cached_price_raw = price.get("cached_input_price_per_million_tokens")
                cached_price = (Decimal(str(cached_price_raw))
                                if cached_price_raw is not None else input_price)
                output_price = Decimal(str(price["output_price_per_million_tokens"]))
                uncached = max(0, int(inp) - int(cached))
                estimates.append((Decimal(uncached) * input_price +
                                  Decimal(int(cached)) * cached_price +
                                  Decimal(int(out)) * output_price) / Decimal(1_000_000))
        except Exception:
            # Missing price evidence is a coverage state, never a zero-cost claim.
            estimates = []
        successes = sum(str(row.get("request_status")) == "SUCCESS" for row in rows)
        request_count = len(rows) or counters["completed"]
        return {
            "probe_run_id": run_id,
            "duration_seconds": round(elapsed, 3),
            "request_count": request_count,
            "success_count": successes if rows else counters["success"],
            "failure_count": request_count - successes if rows else counters["failure"],
            "natural_success_rate": (
                sum(str(row.get("request_status")) == "SUCCESS" for row in rows
                    if not row.get("is_fault_injected")) /
                max(1, sum(not bool(row.get("is_fault_injected")) for row in rows))
            ) if rows else (counters["success"] / request_count if request_count else None),
            "comprehensive_success_rate": successes / request_count if rows and request_count else (
                counters["success"] / request_count if request_count else None),
            "p50_ms": percentile(latencies, 0.50),
            "p95_ms": percentile(latencies, 0.95),
            "p99_ms": percentile(latencies, 0.99),
            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in rows),
            "cached_input_tokens": sum(int(row.get("cached_input_tokens") or 0) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in rows),
            "actual_provider_cost": str(sum(actual, Decimal("0"))) if actual else None,
            "actual_cost_synced_count": len(actual),
            "pending_provider_sync_count": max(0, request_count - len(actual)),
            "estimated_versioned_price": str(sum(estimates, Decimal("0"))) if estimates else None,
            "estimated_cost_coverage": len(estimates) / request_count if request_count else 0,
            "price_version": price_version,
            "provider_live_failures": sum(
                str(row.get("request_status")) != "SUCCESS" and
                row.get("error_source") == "provider_live" for row in rows)
                if rows else counters["provider_live_failures"],
            "uat_injected_failures": sum(bool(row.get("is_fault_injected")) for row in rows)
                if rows else counters["uat_injected_failures"],
            "throttle_count": counters["throttles"],
            "pause_count": counters["pauses"],
            "circuit_open_count": counters["circuit_opens"],
            "recovery_count": counters["recoveries"],
            "max_concurrency_observed": counters["max_concurrency"],
            "configured_max_concurrency": config.max_concurrency,
            "residual_background_tasks": 0,
        }

    def _run(self, run_id: str, config: ProbeExecutionConfig, credential: str,
             started_event: threading.Event) -> None:
        started = self._clock()
        paused_started: float | None = None
        paused_total = 0.0
        next_send = started
        effective_interval = config.interval_seconds
        cooldown_until = 0.0
        circuit_state = "CLOSED"
        model_index = 0
        active: dict[Future[dict[str, Any]], str] = {}
        counters: dict[str, Any] = {
            "sent": 0, "completed": 0, "success": 0, "failure": 0,
            "consecutive_failures": 0, "status_429": 0, "status_5xx": 0,
            "timeouts": 0, "throttles": 0, "pauses": 0,
            "provider_live_failures": 0, "uat_injected_failures": 0,
            "circuit_opens": 0, "recoveries": 0,
            "max_concurrency": 0, "latencies": [],
            "recent": deque(maxlen=500),
        }
        terminal_status = "COMPLETED"
        interruption_reason: str | None = None
        started_event.set()
        try:
            with ThreadPoolExecutor(max_workers=config.max_concurrency,
                                    thread_name_prefix=f"probe-call-{run_id[-6:]}") as pool:
                while True:
                    now = self._clock()
                    row = self._run_row(run_id)
                    if self._shutdown.is_set():
                        terminal_status = "FAILED"
                        interruption_reason = "service_shutdown_interrupted"
                        break
                    if bool(row.get("stop_requested")):
                        terminal_status = "STOPPED"
                        break
                    if row.get("execution_worker_id") != self._worker_id:
                        terminal_status = "FAILED"
                        interruption_reason = "probe_worker_lease_lost"
                        break
                    if row["status"] == "PAUSED":
                        if paused_started is None:
                            paused_started = now
                            counters["pauses"] += 1
                        # Requests already on the wire are allowed to finish, but
                        # PAUSED never submits a new request.
                        for future in list(active):
                            if future.done():
                                result = future.result()
                                result.setdefault("requested_model", active[future])
                                self._finish_future(run_id, result, counters)
                                active.pop(future, None)
                        self._update(
                            run_id,
                            current_concurrency=len(active),
                            completed_count=counters["completed"],
                            success_count=counters["success"],
                            failure_count=counters["failure"],
                        )
                        self._heartbeat(run_id)
                        self._sleep(0.1)
                        continue
                    if paused_started is not None:
                        paused_total += now - paused_started
                        next_send += now - paused_started
                        paused_started = None
                    elapsed = now - started - paused_total
                    for future in list(active):
                        if future.done():
                            result = future.result()
                            result.setdefault("requested_model", active[future])
                            self._finish_future(run_id, result, counters)
                            active.pop(future, None)
                            successful = result.get("execution_status") == "success"
                            if successful and effective_interval > config.interval_seconds:
                                old_interval = effective_interval
                                effective_interval = max(config.interval_seconds,
                                                         effective_interval * 0.8)
                                if effective_interval < old_interval:
                                    self._event(run_id, "frequency_recovered",
                                                "local_control_event", {
                                                    "old_interval_seconds": old_interval,
                                                    "new_interval_seconds": effective_interval,
                                                    "reason": "recent_requests_stable",
                                                })
                            elif not successful:
                                old_interval = effective_interval
                                effective_interval = min(60.0,
                                                         max(config.interval_seconds,
                                                             effective_interval * 1.5))
                                if effective_interval > old_interval:
                                    counters["throttles"] += 1
                                    self._event(run_id, "frequency_throttled",
                                                "local_control_event", {
                                                    "old_interval_seconds": old_interval,
                                                    "new_interval_seconds": effective_interval,
                                                    "reason": "recent_failure",
                                                    "error_source": result.get("error_source") or "provider_live",
                                                    "http_status": result.get("http_status"),
                                                })
                            threshold = int(config.stop_thresholds.get(
                                "consecutive_failures", 5))
                            if counters["consecutive_failures"] >= threshold and circuit_state != "OPEN":
                                circuit_state = "OPEN"
                                counters["circuit_opens"] += 1
                                counters["pauses"] += 1
                                cooldown_until = now + config.cooldown_seconds
                                counters["consecutive_failures"] = 0
                                self._event(run_id, "circuit_opened", "local_control_event", {
                                    "reason": "consecutive_failures",
                                    "threshold": threshold,
                                    "cooldown_seconds": config.cooldown_seconds,
                                    "request_id": result.get("request_id"),
                                })
                            elif circuit_state == "HALF_OPEN":
                                if successful:
                                    circuit_state = "CLOSED"
                                    counters["recoveries"] += 1
                                    self._event(run_id, "circuit_recovered",
                                                "local_control_event", {
                                                    "request_id": result.get("request_id"),
                                                    "result": "probe_succeeded",
                                                })
                                else:
                                    circuit_state = "OPEN"
                                    counters["circuit_opens"] += 1
                                    cooldown_until = now + config.cooldown_seconds
                                    self._event(run_id, "circuit_opened",
                                                "local_control_event", {
                                                    "reason": "half_open_probe_failed",
                                                    "cooldown_seconds": config.cooldown_seconds,
                                                    "request_id": result.get("request_id"),
                                                })
                                    if counters["circuit_opens"] >= 3:
                                        terminal_status = "AUTO_STOPPED"
                                        interruption_reason = "repeated_circuit_open"
                                        break
                    if terminal_status == "AUTO_STOPPED":
                        break
                    recent = [item for item in counters["recent"]
                              if now - float(item["at"]) <= 60]
                    if len(recent) >= 5:
                        total = len(recent)
                        rates = {
                            "rate_429": sum(item["status"] == 429 for item in recent) / total,
                            "rate_5xx": sum(item["status"] >= 500 for item in recent) / total,
                            "timeout_rate": sum(bool(item["timeout"]) for item in recent) / total,
                            "success_rate": sum(bool(item["success"]) for item in recent) / total,
                        }
                        violated = next((name for name in ("rate_429", "rate_5xx", "timeout_rate")
                            if rates[name] >= float(config.stop_thresholds.get(name, 2))), None)
                        if not violated and rates["success_rate"] < float(
                            config.stop_thresholds.get("success_rate", -1)):
                            violated = "success_rate"
                        if violated and circuit_state == "CLOSED":
                            circuit_state = "OPEN"
                            counters["circuit_opens"] += 1
                            counters["pauses"] += 1
                            cooldown_until = now + config.cooldown_seconds
                            self._event(run_id, "circuit_opened", "local_control_event", {
                                "reason": violated,
                                "observed": rates[violated],
                                "threshold": config.stop_thresholds.get(violated),
                                "window_seconds": 60,
                                "sample_size": total,
                            })
                    if circuit_state == "OPEN":
                        if now < cooldown_until:
                            self._heartbeat(run_id)
                            self._sleep(0.05)
                            continue
                        circuit_state = "HALF_OPEN"
                        self._event(run_id, "half_open_probe", "local_control_event", {
                            "reason": "cooldown_completed",
                            "allowed_requests": 1,
                        })
                        next_send = now
                    if elapsed >= config.duration_seconds or (
                        config.max_requests is not None and counters["sent"] >= config.max_requests
                    ):
                        if not active:
                            break
                    elif (now >= next_send and len(active) < config.max_concurrency and
                          not (circuit_state == "HALF_OPEN" and active)):
                        model = config.models[model_index % len(config.models)]
                        model_index += 1
                        body = self._request_body(run_id, config, model)
                        active[pool.submit(self._execute_one, body, credential, config)] = model
                        counters["sent"] += 1
                        counters["max_concurrency"] = max(counters["max_concurrency"], len(active))
                        next_send = now + effective_interval
                    self._heartbeat(run_id)
                    self._update(
                        run_id,
                        current_interval_seconds=effective_interval,
                        current_concurrency=len(active),
                        sent_count=counters["sent"],
                        completed_count=counters["completed"],
                        success_count=counters["success"],
                        failure_count=counters["failure"],
                        pause_count=counters["pauses"],
                        max_concurrency_observed=counters["max_concurrency"],
                        last_error=None if counters["consecutive_failures"] == 0 else "recent_failure",
                        next_run_at=(datetime.now(timezone.utc) +
                                     timedelta(seconds=effective_interval)).isoformat(),
                    )
                    self._sleep(0.05)
        except Exception as exc:
            terminal_status = "FAILED"
            interruption_reason = type(exc).__name__
            self._event(run_id, "probe_execution_failed", "local_control_event",
                        {"reason": interruption_reason})
        finally:
            elapsed = self._clock() - started - paused_total
            summary = self._summary(run_id, counters, config, elapsed)
            self._update(
                run_id,
                status=terminal_status,
                finished_at=utcnow(),
                current_concurrency=0,
                sent_count=counters["sent"],
                completed_count=counters["completed"],
                success_count=counters["success"],
                failure_count=counters["failure"],
                max_concurrency_observed=counters["max_concurrency"],
                throttle_count=counters["throttles"],
                pause_count=counters["pauses"],
                circuit_open_count=counters["circuit_opens"],
                recovery_count=counters["recoveries"],
                next_run_at=None,
                interruption_reason=interruption_reason,
                execution_lease_expires_at=utcnow(),
                summary_json=json.dumps(summary, ensure_ascii=False, sort_keys=True),
            )
            self._event(run_id, "probe_completed" if terminal_status == "COMPLETED"
                        else "probe_stopped" if terminal_status == "STOPPED"
                        else "probe_failed", "local_control_event", summary)
            with self._worker_lock:
                self._workers.pop(run_id, None)
            credential = ""  # drop the local reference as soon as the run finishes
