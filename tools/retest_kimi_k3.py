"""Bounded Kimi K3 stability retest using the encrypted China-UAT credential."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from backend.acceptance_run_service import AcceptanceRunError, AcceptanceRunService
from backend.call_log_service import CallLogService
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.tenant_security import (DEFAULT_DEVELOPMENT_TENANT_ID,
    DEFAULT_DEVELOPMENT_WORKSPACE_ID, TenantScope)
from backend.uat_http_workbench_service import WorkbenchError, execute_workbench
from backend.uat_service import UatSettings


DB = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
SCOPE = TenantScope.local_development()
RUN_ID = "AR-c2e60d08-9ff7-4a23-98a0-b6fb4d55b951"
PROMPT = "请只用一句中文回答：水的化学式是什么？"


def credential() -> str:
    scope = CredentialScope("windows-dev:lx", DEFAULT_DEVELOPMENT_TENANT_ID,
                            DEFAULT_DEVELOPMENT_WORKSPACE_ID, "china_uat")
    loaded = PersistentCredentialVault().load(scope, required=True)
    assert loaded is not None
    return loaded[0]


def request_body(stream: bool) -> dict:
    return {
      "method": "POST", "environment_id": "china_uat", "path": "/v1/chat/completions",
      "query_params": [], "headers": [],
      "auth": {"method": "bearer", "header_name": "Authorization", "prefix": "Bearer"},
      "body": {"type": "json", "value": {"model": "kimi-k3",
        "messages": [{"role": "user", "content": PROMPT}], "stream": stream,
        "max_tokens": 64, "temperature": 0}},
      "content_type": "application/json", "timeout_seconds": 60, "stream": stream,
      "model": "kimi-k3", "model_selection_mode": "specified",
      "routing_policy": "kimi_k3_stability_retest_no_fallback", "request_type": "text",
      "acceptance_run_id": RUN_ID, "_strategy_variant": "kimi_k3_stability_retest",
    }


def latest(stream: bool) -> dict:
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT * FROM standardized_call_logs
          WHERE acceptance_run_id=? AND requested_model='kimi-k3' AND stream=?
          AND strategy_variant='kimi_k3_stability_retest'
          ORDER BY cursor_id DESC LIMIT 1""", (RUN_ID, int(stream))).fetchone()
    if not row:
        return {"request_status": "FAILED", "error_category": "record_not_found"}
    item = dict(row)
    allowed = {"occurred_at", "request_id", "response_id", "decision_id", "actual_model",
               "channel_id", "provider", "request_status", "http_status", "error_code",
               "error_category", "total_latency_ms", "first_token_latency_ms", "input_tokens",
               "cached_input_tokens", "output_tokens", "cost_amount", "currency", "cost_source",
               "error_source", "is_fault_injected", "total_attempts", "fallback_used"}
    return {key: item.get(key) for key in allowed}


def existing_attempts(stream: bool) -> list[dict]:
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT * FROM standardized_call_logs
          WHERE acceptance_run_id=? AND requested_model='kimi-k3' AND stream=?
          AND strategy_variant='kimi_k3_stability_retest'
          ORDER BY cursor_id""", (RUN_ID, int(stream))).fetchall()
    allowed = {"occurred_at", "request_id", "response_id", "decision_id", "actual_model",
               "channel_id", "provider", "request_status", "http_status", "error_code",
               "error_category", "total_latency_ms", "first_token_latency_ms", "input_tokens",
               "cached_input_tokens", "output_tokens", "cost_amount", "currency", "cost_source",
               "error_source", "is_fault_injected", "total_attempts", "fallback_used"}
    result = []
    for number, row in enumerate(rows, 1):
        item = {key: row[key] for key in allowed}
        item.update({"attempt_number": number, "started_at": row["occurred_at"], "stream": stream,
                     "provider_timeout": row["error_category"] in {"network_timeout", "upstream_timeout"}})
        item["estimated_cost"] = estimated_cost(item)
        result.append(item)
    return result


def estimated_cost(item: dict) -> dict | None:
    if not isinstance(item.get("input_tokens"), int) or not isinstance(item.get("output_tokens"), int):
        return None
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT r.*,v.price_version FROM model_price_catalog_records r
          JOIN model_price_catalog_versions v ON v.price_version=r.price_version
          WHERE r.model_id='kimi-k3' AND v.environment_id='china_uat'
          AND r.verification_status='confirmed' ORDER BY v.captured_at DESC LIMIT 1""").fetchone()
    if not row:
        return None
    cost = ((Decimal(item["input_tokens"]) * Decimal(row["input_price_per_million_tokens"])
             + Decimal(item["output_tokens"]) * Decimal(row["output_price_per_million_tokens"]))
            / Decimal(1_000_000))
    return {"amount": str(cost.quantize(Decimal("0.000001"))), "currency": row["currency"],
            "cost_type": "estimated_versioned_price", "price_version": row["price_version"]}


def main() -> None:
    state = EnvironmentRuntimeSettings(DB).execution_state("china_uat")
    if state.get("enabled") is not True:
        raise RuntimeError("china_uat_environment_disabled")
    key = credential()
    logs = CallLogService(DB, SCOPE)
    runs = AcceptanceRunService(DB, SCOPE)
    before = []
    with sqlite3.connect(DB) as db:
        db.row_factory = sqlite3.Row
        before = [dict(row) for row in db.execute("""SELECT occurred_at,request_id,http_status,
          error_category,total_latency_ms,stream FROM standardized_call_logs
          WHERE acceptance_run_id=? AND requested_model='kimi-k3' AND is_fault_injected=0
          AND request_status<>'SUCCESS' ORDER BY occurred_at""", (RUN_ID,))]
    evidence = {"acceptance_run_id": RUN_ID, "model": "kimi-k3", "prompt": PROMPT,
                "parameters": {"max_tokens": 64, "temperature": 0, "fallback": False,
                               "maximum_attempts_per_mode": 3},
                "environment_state": state, "previous_live_failures": before, "modes": []}
    settings = replace(UatSettings.load("china_uat"), enabled=True)
    for stream in (False, True):
        prior = existing_attempts(stream)
        mode = {"stream": stream, "attempts": prior}
        for attempt_number in range(len(prior) + 1, 4):
            if any(item.get("request_status") == "SUCCESS" for item in mode["attempts"]):
                break
            started = datetime.now(timezone.utc).isoformat()
            try:
                result = execute_workbench(request_body(stream), settings, key,
                    connection_verified=True, call_logs=logs, capabilities=None)
                record = latest(stream)
                record["response_excerpt"] = str(result.get("response_body") or "")[:300]
            except WorkbenchError as exc:
                record = latest(stream)
                record["workbench_error"] = exc.code
            record.update({"attempt_number": attempt_number, "started_at": started,
                           "stream": stream, "provider_timeout":
                           record.get("error_category") in {"network_timeout", "upstream_timeout"},
                           "estimated_cost": estimated_cost(record)})
            mode["attempts"].append(record)
            if record.get("request_id"):
                try:
                    runs.link_evidence(RUN_ID, request_id=record.get("request_id"),
                                       decision_id=record.get("decision_id"))
                except AcceptanceRunError as exc:
                    if str(exc) != "acceptance_run_finalized":
                        raise
                    record["evidence_link"] = "revision_index_required"
            if record.get("request_status") == "SUCCESS":
                break
            time.sleep(2)
        mode["status"] = ("live_verified" if any(
            item.get("request_status") == "SUCCESS" for item in mode["attempts"])
            else "live_attempted_provider_unstable")
        evidence["modes"].append(mode)
    evidence["status"] = ("live_verified_with_instability_history" if all(
        mode["status"] == "live_verified" for mode in evidence["modes"])
        else "live_attempted_provider_unstable")
    evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
    target = ROOT / "evidence" / "final_acceptance" / RUN_ID / "kimi_k3_stability_retest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": evidence["status"], "attempts": sum(
        len(mode["attempts"]) for mode in evidence["modes"]), "evidence": str(target.relative_to(ROOT)),
        "request_ids": [item.get("request_id") for mode in evidence["modes"]
                        for item in mode["attempts"] if item.get("request_id")]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
