"""Run one live UAT probe and compare only exact Provider identifiers."""
from __future__ import annotations

import hashlib
import json
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import websocket

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from backend.call_log_service import CallLogService
from backend.domestic_uat_chrome_manager import DomesticUatChromeManager
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.realtime_log_sync_service import RealtimeLogSyncService
from backend.tenant_security import (
    DEFAULT_DEVELOPMENT_TENANT_ID, DEFAULT_DEVELOPMENT_WORKSPACE_ID,
    TenantScope,
)
from backend.uat_http_workbench_service import execute_workbench
from backend.uat_service import UatSettings


DB = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/" \
    "routing_quality_console.sqlite3"
SCOPE = TenantScope.local_development()


class Runtime:
    def active(self, environment: str, key: str):
        return None


def credential() -> str:
    scope = CredentialScope(
        "windows-dev:lx", DEFAULT_DEVELOPMENT_TENANT_ID,
        DEFAULT_DEVELOPMENT_WORKSPACE_ID, "china_uat")
    loaded = PersistentCredentialVault().load(scope, required=True)
    if not loaded:
        raise RuntimeError("uat_secure_credential_unavailable")
    return loaded[0]


def cdp_json(console_path: str, api_path: str) -> dict:
    manager = DomesticUatChromeManager()
    targets = manager._request_json("/json/list")
    target = next(item for item in targets if
                  urlsplit(str(item.get("url") or "")).path == console_path)
    socket = websocket.create_connection(
        str(target["webSocketDebuggerUrl"]), timeout=20,
        suppress_origin=True)
    try:
        socket.send(json.dumps({"id": 1, "method": "Network.enable"}))
        socket.send(json.dumps({"id": 2, "method": "Page.reload",
                                "params": {"ignoreCache": True}}))
        request_id = None
        while request_id is None:
            event = json.loads(socket.recv())
            if event.get("method") == "Network.responseReceived":
                params = event.get("params") or {}
                response = params.get("response") or {}
                if urlsplit(str(response.get("url") or "")).path == api_path:
                    if int(response.get("status") or 0) != 200:
                        raise RuntimeError("provider_billing_http_failed")
                    request_id = params["requestId"]
        socket.send(json.dumps({
            "id": 3, "method": "Network.getResponseBody",
            "params": {"requestId": request_id},
        }))
        while True:
            event = json.loads(socket.recv())
            if event.get("id") == 3:
                return json.loads(event["result"]["body"])
    finally:
        socket.close()


def main() -> dict:
    state = EnvironmentRuntimeSettings(DB).execution_state("china_uat")
    if state.get("enabled") is not True:
        raise RuntimeError("china_uat_environment_disabled")
    correlation_test_id = "CORR-" + uuid.uuid4().hex.upper()
    logs = CallLogService(DB, SCOPE)
    settings = replace(UatSettings.load("china_uat"), enabled=True,
                       timeout_seconds=60)
    body = {
        "method": "POST", "environment_id": "china_uat",
        "path": "/v1/chat/completions", "query_params": [], "headers": [],
        "auth": {"method": "bearer", "header_name": "Authorization",
                 "prefix": "Bearer"},
        "body": {"type": "json", "value": {
            "model": "kimi-k3",
            "messages": [{"role": "user",
                          "content": "请只回答：费用关联验证成功"}],
            "stream": False, "max_tokens": 32, "temperature": 0,
        }},
        "content_type": "application/json", "timeout_seconds": 60,
        "stream": False, "model": "kimi-k3",
        "model_selection_mode": "specified",
        "routing_policy": "provider_correlation_probe_no_fallback",
        "request_type": "text", "_strategy_variant": "provider_correlation_probe",
        "correlation_test_id": correlation_test_id,
    }
    result = execute_workbench(
        body, settings, credential(), connection_verified=True,
        call_logs=logs, capabilities=None)
    local_request_id = result["request_id"]
    response_ids = {
        key: result.get(key) for key in (
            "provider_request_id", "provider_response_id",
            "provider_trace_id", "client_correlation_id")
        if result.get(key)
    }
    for header, value in (result.get("response_headers") or {}).items():
        if value and any(token in str(header).casefold() for token in
                         ("request-id", "response-id", "trace-id")):
            response_ids[f"response_header:{str(header).casefold()}"] = str(value)
    time.sleep(8)
    billing = cdp_json("/console/billing/logs", "/api/log/self")
    items = ((billing.get("data") or {}).get("items") or [])
    comparisons = []
    provider_fields = (
        "request_id", "response_id", "trace_id", "client_correlation_id")
    for local_field, local_value in response_ids.items():
        for item in items:
            other = item.get("other")
            if isinstance(other, str):
                try:
                    other = json.loads(other)
                except ValueError:
                    other = {}
            other = other if isinstance(other, dict) else {}
            for provider_field in provider_fields:
                provider_value = item.get(provider_field)
                if provider_value is None:
                    provider_value = other.get(provider_field)
                if provider_value is not None:
                    comparisons.append({
                        "local_field": local_field,
                        "call_response_value": str(local_value),
                        "provider_log_field": provider_field,
                        "provider_value": str(provider_value),
                        "exact_equal": str(local_value) == str(provider_value),
                        "provider_log_id": str(item.get("id") or ""),
                    })
    now = datetime.now(timezone.utc)
    sync = RealtimeLogSyncService(DB, ROOT / "output" / "uat_executions.jsonl",
                                  Runtime())
    for prior in sync.list_jobs("china_uat"):
        if prior.get("state") in {"syncing", "synchronizing",
                                  "waiting_for_manual_login",
                                  "waiting_for_operator_confirmation"}:
            sync.stop(prior["sync_job_id"])
    job = sync.create_job(
        "china_uat", (now - timedelta(hours=2)).isoformat(),
        (now + timedelta(minutes=1)).isoformat(), "Asia/Shanghai", 5, 100)
    sync.update_job(job["sync_job_id"], state="waiting_for_manual_login",
                    browser_context_active=1,
                    current_safe_url=job["safe_log_page_url"])
    sync.confirm_login(job["sync_job_id"], True)
    ingest = sync.ingest(
        job["sync_job_id"], billing,
        "https://uat.weimeta.cn/api/log/self",
        billing_context={"quota_per_unit": "500000",
                         "quota_display_type": "CNY",
                         "usd_exchange_rate": "7.3"})
    sync.finalize_batch(job["sync_job_id"], end_reason="provider_no_more_records")
    with logs.connect() as db:
        row = db.execute("""SELECT request_id,local_request_id,decision_id,
          provider_request_id,provider_response_id,provider_trace_id,
          client_correlation_id,provider_log_id,cost_amount,cost_status,
          provider_sync_failure_reason FROM standardized_call_logs
          WHERE local_request_id=?""", (local_request_id,)).fetchone()
    evidence = {
        "correlation_test_id": correlation_test_id,
        "environment_id": "china_uat", "model": "kimi-k3",
        "local_execution": dict(row),
        "response_identifier_fields": sorted(response_ids),
        "billing_page_count": len(items),
        "billing_total": int((billing.get("data") or {}).get("total") or 0),
        "billing_item_fields": sorted(items[0]) if items else [],
        "billing_other_fields": sorted({key for item in items for key in
            ((json.loads(item["other"]) if isinstance(item.get("other"), str)
              and str(item.get("other")).startswith("{") else
              item.get("other") if isinstance(item.get("other"), dict) else {}) or {})}),
        "diagnostic_table": comparisons,
        "exact_common_fields": [item for item in comparisons
                                if item["exact_equal"]],
        "ingest": {key: ingest.get(key) for key in (
            "collected_count", "inserted_count", "duplicate_count",
            "correlated_count", "ambiguous_count")},
        "automatic_fuzzy_matching_used": False,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    target = ROOT / "evidence" / "provider-correlation" / \
        f"{correlation_test_id}.json"
    target.write_text(json.dumps(
        evidence, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "correlation_test_id": correlation_test_id,
        "local_request_id": local_request_id,
        "decision_id": result.get("decision_id"),
        "http_status": result.get("http_status"),
        "exact_common_field_count": len(evidence["exact_common_fields"]),
        "cost_status": dict(row).get("cost_status"),
        "failure_reason": dict(row).get("provider_sync_failure_reason"),
        "evidence_file": str(target.relative_to(ROOT)),
        "evidence_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    }


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, sort_keys=True))
