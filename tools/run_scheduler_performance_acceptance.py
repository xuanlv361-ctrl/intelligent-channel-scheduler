"""Generate small, real China-UAT samples for the scheduler performance view.

The credential is read from the existing encrypted vault and is never printed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from backend.call_log_service import CallLogService
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.tenant_security import DEFAULT_DEVELOPMENT_TENANT_ID, DEFAULT_DEVELOPMENT_WORKSPACE_ID, TenantScope
from backend.uat_http_workbench_service import WorkbenchError, execute_workbench
from backend.uat_service import UatSettings
from scheduler_timing import SchedulerTimingRecorder

DB = Path(os.environ.get("ROUTING_CONSOLE_DATABASE_PATH") or
          Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3")
MODELS = {
    "current_actual": "kimi-k2.6",
    "latency_first": "deepseek-v4-flash",
    "cost_first": "deepseek-v4-flash",
}


def credential() -> str:
    scope = CredentialScope("windows-dev:lx", DEFAULT_DEVELOPMENT_TENANT_ID,
                            DEFAULT_DEVELOPMENT_WORKSPACE_ID, "china_uat")
    loaded = PersistentCredentialVault().load(scope, required=True)
    if not loaded:
        raise RuntimeError("uat_secure_credential_unavailable")
    return loaded[0]


def scheduler_decision(strategy: str, selected_model: str) -> tuple[dict, dict[str, float]]:
    stages: dict[str, float] = {}
    with sqlite3.connect(DB) as db:
        started = time.perf_counter()
        known = [str(row[0]) for row in db.execute(
            "SELECT DISTINCT requested_model FROM standardized_call_logs "
            "WHERE environment_id='china_uat' AND requested_model IS NOT NULL")]
        candidates = sorted(model for model in MODELS.values() if model in known)
        stages["candidate_discovery"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        unsupported = {str(row[0]) for row in db.execute(
            "SELECT subject_id FROM capability_evidence WHERE state='unsupported'")}
        eligible = [model for model in candidates if model not in unsupported]
        stages["capability_filter"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        metrics = {str(row[0]): {"samples": int(row[1]), "average_ms": row[2]} for row in db.execute(
            "SELECT requested_model,COUNT(*),AVG(total_latency_ms) FROM standardized_call_logs "
            "WHERE environment_id='china_uat' AND source_type='realtime_execution' "
            "AND requested_model IN ('kimi-k2.6','deepseek-v4-flash') GROUP BY requested_model")}
        stages["metrics_read"] = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    constrained = [model for model in eligible if model in MODELS.values()]
    stages["constraint_filter"] = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    scored = [{"model_id": model, "score": float(-(metrics.get(model, {}).get("average_ms") or 1e9))}
              for model in constrained]
    stages["scoring"] = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    scored.sort(key=lambda row: (-row["score"], row["model_id"]))
    stages["deterministic_sort"] = (time.perf_counter() - started) * 1000
    return ({"policy": strategy, "selected_model": selected_model,
             "candidates": scored, "excluded": [
                 {"model_id": model, "reason": "明确能力不支持"} for model in sorted(unsupported & set(candidates))]}, stages)


def main() -> None:
    if not EnvironmentRuntimeSettings(DB).execution_state("china_uat")["enabled"]:
        raise RuntimeError("china_uat_environment_disabled")
    key = credential()
    calls = CallLogService(DB, TenantScope.local_development())
    recorder = SchedulerTimingRecorder(DB)
    settings = replace(UatSettings.load("china_uat"), enabled=True, timeout_seconds=45)
    run_id = "SPR-" + uuid.uuid4().hex.upper()
    results: list[dict] = []
    for sample_index in range(5):
        for strategy in ("current_actual", "latency_first", "cost_first"):
            model = MODELS[strategy]
            decision, stages = scheduler_decision(strategy, model)
            stream = (sample_index + list(MODELS).index(strategy)) % 2 == 0
            body = {
                "method": "POST", "environment_id": "china_uat", "path": "/v1/chat/completions",
                "query_params": [], "headers": [],
                "auth": {"method": "bearer", "header_name": "Authorization", "prefix": "Bearer"},
                "body": {"type": "json", "value": {"model": model,
                    "messages": [{"role": "user", "content": "请只回答：OK"}],
                    "stream": stream, "max_tokens": 16, "temperature": 0}},
                "content_type": "application/json", "timeout_seconds": 45, "stream": stream,
                "model": model, "model_selection_mode": "automatic", "routing_policy": strategy,
                "request_type": "text", "traffic_class": "business", "acceptance_run_id": run_id,
                "_strategy_variant": strategy, "_routing_decision": decision,
                "_scheduler_stage_durations": stages, "_tenant_id": DEFAULT_DEVELOPMENT_TENANT_ID,
            }
            try:
                result = execute_workbench(body, settings, key, connection_verified=True,
                                           call_logs=calls, performance_recorder=recorder)
                if result.get("decision_id"):
                    calls.save_routing_decision(result=result, routing_decision=decision)
                results.append({"strategy": strategy, "stream": stream,
                                "request_id": result.get("request_id"),
                                "decision_id": result.get("decision_id"),
                                "http_status": result.get("http_status")})
            except WorkbenchError as exc:
                results.append({"strategy": strategy, "stream": stream,
                                "request_id": None, "decision_id": None,
                                "http_status": exc.status_code, "error": exc.code})
            print(json.dumps({"run_id": run_id, "completed": len(results), **results[-1]},
                             ensure_ascii=False), flush=True)

    # A real UAT 404 provides one failure sample without fault injection or fallback masking.
    decision, stages = scheduler_decision("current_actual", "deepseek-v4-flash")
    failure = {
        "method": "POST", "environment_id": "china_uat", "path": "/v1/does-not-exist",
        "query_params": [], "headers": [],
        "auth": {"method": "bearer", "header_name": "Authorization", "prefix": "Bearer"},
        "body": {"type": "json", "value": {"model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "请只回答：OK"}], "stream": False}},
        "content_type": "application/json", "timeout_seconds": 15, "stream": False,
        "model": "deepseek-v4-flash", "model_selection_mode": "automatic",
        "routing_policy": "current_actual", "traffic_class": "business", "acceptance_run_id": run_id,
        "_strategy_variant": "current_actual", "_routing_decision": decision,
        "_scheduler_stage_durations": stages, "_tenant_id": DEFAULT_DEVELOPMENT_TENANT_ID,
    }
    result = execute_workbench(failure, settings, key, connection_verified=True,
                               call_logs=calls, performance_recorder=recorder)
    results.append({"strategy": "current_actual", "stream": False,
                    "request_id": result.get("request_id"), "decision_id": result.get("decision_id"),
                    "http_status": result.get("http_status")})
    print(json.dumps({"run_id": run_id, "completed": len(results), **results[-1]},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
