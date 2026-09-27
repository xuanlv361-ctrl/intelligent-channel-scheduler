"""Run an interleaved 90-call China-UAT strategy-effect acceptance."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from backend.call_log_service import CallLogService
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from backend.live_acceptance_service import LiveAcceptanceService
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.price_catalog_service import PriceCatalogService
from backend.tenant_security import (DEFAULT_DEVELOPMENT_TENANT_ID,
    DEFAULT_DEVELOPMENT_WORKSPACE_ID, TenantScope)
from backend.uat_http_workbench_service import execute_workbench
from backend.uat_service import UatSettings

DB = Path.home()/"AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
SCOPE = TenantScope.local_development()
MODELS = ["kimi-k3", "deepseek-v4-flash", "glm-5.2", "kimi-k2.7-code", "kimi-k2.6"]
STRATEGIES = ["current_actual", "latency_first", "cost_first"]
CASES = [
    ("short_zh", "请只用一句中文回答：水的化学式是什么？", ["水", "H2O"]),
    ("english_qa", "Answer in one short English sentence: What is the capital of France?", ["Paris"]),
    ("summary_zh", "用不超过80字总结：水从海洋蒸发，凝结成云，形成降水，再经地表径流返回海洋。", ["蒸发", "降水"]),
    ("summary_en", "Summarize in under 45 words: Evaporation forms clouds; precipitation falls and runoff returns water to the sea.", ["water"]),
    ("code_explain", "用两句话解释 Python 表达式 sum(x*x for x in range(5)) 的作用和结果。", ["30"]),
    ("code_generate", "生成一个Python函数add(a,b)，只输出简短代码和一句说明。", ["def", "add"]),
    ("extract", "从文本提取JSON：订单A102，金额36.50元，状态已完成。字段为order_id、amount、status。", ["A102", "36.50"]),
    ("translate", "将这句话翻译成英文：智能调度需要可解释的决策日志。", ["routing"]),
    ("reasoning", "若每分钟处理12个请求，连续5分钟成功率为95%，请给出总请求数和预计成功数。", ["60", "57"]),
    ("long_context", "阅读并回答：甲服务延迟800毫秒成本0.02元，乙服务延迟500毫秒成本0.05元。若目标是最低延迟，应选哪个？若目标最低成本，应选哪个？", ["乙", "甲"]),
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def credential() -> str:
    scope = CredentialScope("windows-dev:lx", DEFAULT_DEVELOPMENT_TENANT_ID,
                            DEFAULT_DEVELOPMENT_WORKSPACE_ID, "china_uat")
    loaded = PersistentCredentialVault().load(scope, required=True)
    if not loaded:
        raise RuntimeError("uat_secure_credential_unavailable")
    return loaded[0]


def baseline(db: sqlite3.Connection) -> dict[str, Any]:
    watermark = db.execute("SELECT COALESCE(MAX(cursor_id),0) FROM standardized_call_logs").fetchone()[0]
    latest_log = db.execute("SELECT MAX(updated_at) FROM standardized_call_logs").fetchone()[0]
    configuration = db.execute("""SELECT configuration_version FROM configuration_versions
      WHERE is_active=1 ORDER BY created_at DESC LIMIT 1""").fetchone()
    return {"database_watermark": watermark, "log_sync_time": latest_log,
            "configuration_version": configuration[0] if configuration else None}


def observed_metrics(db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for model in MODELS:
        rows = db.execute("""SELECT request_status,total_latency_ms FROM standardized_call_logs
          WHERE source_type='realtime_execution' AND requested_model=?
          AND COALESCE(traffic_class,'business')='business'
          AND COALESCE(is_fault_injected,0)=0 ORDER BY cursor_id DESC LIMIT 100""", (model,)).fetchall()
        latencies = [float(row[1]) for row in rows if row[0] == "SUCCESS" and row[1] is not None]
        result[model] = {"success": sum(row[0] == "SUCCESS" for row in rows),
                         "failure": sum(row[0] != "SUCCESS" for row in rows),
                         "average_latency_ms": sum(latencies)/len(latencies) if latencies else None,
                         "sample_count": len(rows)}
    return result


def estimate(price: dict[str, Any] | None, input_tokens: int, output_tokens: int) -> Decimal | None:
    if not price or price.get("verification_status") != "confirmed":
        return None
    if price.get("per_request_price") is not None:
        return Decimal(price["per_request_price"])
    input_price, output_price = price.get("input_price_per_million_tokens"), price.get("output_price_per_million_tokens")
    if input_price is None or output_price is None:
        return None
    return ((Decimal(input_tokens) * Decimal(input_price) +
             Decimal(output_tokens) * Decimal(output_price)) / Decimal(1_000_000))


def select(strategy: str, metrics: dict[str, dict[str, Any]], prices: dict[str, dict[str, Any]],
           prompt: str) -> tuple[str, dict[str, Any]]:
    approximate_input = max(1, len(prompt) // 2)
    candidates, exclusions = [], []
    for model in MODELS:
        metric = metrics[model]
        estimated = estimate(prices.get(model), approximate_input, 96)
        if strategy == "cost_first" and estimated is None:
            exclusions.append({"model_id": model, "reason": "版本化价格证据缺失"})
            continue
        if strategy == "latency_first":
            score = -(metric["average_latency_ms"] if metric["average_latency_ms"] is not None else 10**9)
            reason = "真实历史平均延迟"
        elif strategy == "cost_first":
            score = -float(estimated)
            reason = "当前有效价格版本估算"
        else:
            score = metric["success"]*10000 - metric["failure"]*5000 - (metric["average_latency_ms"] or 100000)/1000
            reason = "当前实际策略：成功证据优先"
        candidates.append({"model_id": model, "score": round(score, 9), "reason": reason,
            "sample_count": metric["sample_count"], "average_latency_ms": metric["average_latency_ms"],
            "estimated_cost": str(estimated) if estimated is not None else None})
    if not candidates:
        raise RuntimeError(f"no_candidates_for_{strategy}")
    candidates.sort(key=lambda item: (-item["score"], item["model_id"]))
    selected = candidates[0]["model_id"]
    decision = {"policy": strategy, "selected_model": selected, "selected_channel": None,
        "selection_reason": candidates[0]["reason"], "confidence": "observed",
        "candidates": candidates, "excluded": exclusions,
        "catalog_candidate_count": len(candidates)}
    return selected, decision


def response_text(result: dict[str, Any]) -> str:
    raw = result.get("response_body") or ""
    try:
        payload = json.loads(raw)
        return str((((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or raw))
    except Exception:
        return str(raw)


def main() -> None:
    repetitions = max(1, min(3, int(os.environ.get("STRATEGY_REPETITIONS", "3"))))
    run_id = "SER-" + uuid.uuid4().hex.upper()
    key = credential()
    runtime = EnvironmentRuntimeSettings(DB).execution_state("china_uat")
    if not runtime or not runtime["enabled"]:
        raise RuntimeError("china_uat_environment_disabled")
    settings = replace(UatSettings.load("china_uat"), enabled=True, timeout_seconds=45)
    calls = CallLogService(DB, SCOPE)
    service = LiveAcceptanceService(DB, SCOPE)
    prices_service = PriceCatalogService(DB, ROOT/"config/price_sync_policy_v1.json", development_mode=True)
    price_status = prices_service.model_catalog_status()
    prices = {row["model_id"]: row for row in prices_service.list_model_prices()}
    with sqlite3.connect(DB) as db:
        base = baseline(db)
    git_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    service.create_strategy_run({"strategy_effect_run_id": run_id, "environment_id": "china_uat",
        "started_at": utcnow(), "git_commit": git_commit, **base,
        "price_version": price_status.get("price_version"), "model_catalog_version": "live-uat-catalog",
        "uat_environment_enabled": True,
        "planned_requests": len(CASES) * len(STRATEGIES) * repetitions})
    evidence_dir = ROOT/"evidence"/"strategy_effect"/run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir/"baseline.json").write_text(json.dumps({"strategy_effect_run_id": run_id,
        "git_commit": git_commit, **base, "price_version": price_status.get("price_version"),
        "model_catalog_version": "live-uat-catalog", "uat_environment_state": runtime},
        ensure_ascii=False, indent=2), encoding="utf-8")
    for repetition in range(1, repetitions + 1):
        for case_index, (case_id, prompt, assertions) in enumerate(CASES):
            order = STRATEGIES[(case_index + repetition - 1) % 3:] + STRATEGIES[:(case_index + repetition - 1) % 3]
            for strategy in order:
                with sqlite3.connect(DB) as db:
                    metrics = observed_metrics(db)
                selected, decision = select(strategy, metrics, prices, prompt)
                metric_snapshot_id = "MS-" + uuid.uuid4().hex.upper()
                body = {"method": "POST", "environment_id": "china_uat", "path": "/v1/chat/completions",
                    "query_params": [], "headers": [], "auth": {"method": "bearer", "header_name": "Authorization", "prefix": "Bearer"},
                    "body": {"type": "json", "value": {"model": selected, "messages": [{"role": "user", "content": prompt}],
                        "stream": False, "max_tokens": 96, "temperature": 0}},
                    "content_type": "application/json", "timeout_seconds": 45, "stream": False,
                    "model": selected, "model_selection_mode": "specified", "routing_policy": strategy,
                    "request_type": "text", "strategy_effect_run_id": run_id,
                    "_strategy_variant": strategy, "traffic_class": "business",
                    "_configuration_version": base.get("configuration_version"),
                    "_metric_snapshot_id": metric_snapshot_id}
                try:
                    result = execute_workbench(body, settings, key, connection_verified=True, call_logs=calls)
                except Exception as exc:
                    result = {"execution_status": "failed", "http_status": None, "request_id": None,
                        "response_id": None, "decision_id": None, "actual_model": None,
                        "total_latency_ms": None, "first_token_latency_ms": None,
                        "input_tokens": None, "cached_input_tokens": None, "output_tokens": None,
                        "cost_amount": None, "error": {"error_category": type(exc).__name__}}
                if result.get("decision_id"):
                    calls.save_routing_decision(result=result, routing_decision=decision)
                text = response_text(result)
                checks = {term: term.casefold() in text.casefold() for term in assertions}
                actual_input = result.get("input_tokens")
                actual_output = result.get("output_tokens")
                estimated = estimate(prices.get(selected), actual_input or max(1, len(prompt)//2), actual_output or 96)
                actual = result.get("cost_amount")
                row = {"request_case_id": case_id, "repetition": repetition, "strategy": strategy,
                    "strategy_version": strategy + "-live-v1", "configuration_version": base.get("configuration_version"),
                    "metric_snapshot_id": metric_snapshot_id, "selected_model": selected,
                    "candidates_json": json.dumps(decision["candidates"], ensure_ascii=False),
                    "exclusions_json": json.dumps(decision["excluded"], ensure_ascii=False),
                    "scores_json": json.dumps(decision["candidates"], ensure_ascii=False),
                    "request_id": result.get("request_id"), "response_id": result.get("response_id"),
                    "decision_id": result.get("decision_id"), "actual_model": result.get("actual_model"),
                    "http_status": result.get("http_status"), "success": int(result.get("execution_status") == "success"),
                    "first_token_latency_ms": result.get("first_token_latency_ms"), "total_latency_ms": result.get("total_latency_ms"),
                    "input_tokens": actual_input, "cached_input_tokens": result.get("cached_input_tokens"),
                    "output_tokens": actual_output, "total_tokens": result.get("total_tokens") or ((actual_input or 0)+(actual_output or 0) or None),
                    "actual_provider_cost": actual, "estimated_versioned_price": str(estimated) if estimated is not None else None,
                    "cost_status": "actual_provider_cost" if actual is not None else "pending_provider_sync",
                    "price_version": price_status.get("price_version"), "retry_count": max(0, int(result.get("attempts") or 1)-1),
                    "fallback_used": int(bool(result.get("fallback_used"))), "output_complete": int(bool(text.strip())),
                    "assertion_passed": int(all(checks.values())), "assertion_json": json.dumps(checks, ensure_ascii=False),
                    "error_category": ((result.get("error") or {}).get("error_category") if isinstance(result.get("error"), dict) else None),
                    "created_at": utcnow()}
                service.record_strategy_result(run_id, row)
                print(json.dumps({"run_id": run_id, "completed": service.strategy_run(run_id)["completed_requests"],
                    "case": case_id, "strategy": strategy, "model": selected,
                    "http_status": result.get("http_status"), "request_id": result.get("request_id")}, ensure_ascii=True), flush=True)
    summary = service.summarize_strategy(run_id)
    (evidence_dir/"results.json").write_text(json.dumps(service.strategy_run(run_id), ensure_ascii=False, indent=2), encoding="utf-8")
    (evidence_dir/"summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    service.finish_strategy_run(run_id, summary, str(evidence_dir.relative_to(ROOT)))
    print(json.dumps({"strategy_effect_run_id": run_id, "summary": summary}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
