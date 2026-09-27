"""Run an isolated China-UAT injected circuit open/half-open/recovery acceptance."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "src"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from backend.call_log_service import CallLogService
from backend.circuit_breaker_service import CircuitBreakerService
from backend.live_acceptance_service import LiveAcceptanceService
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.tenant_security import (
    DEFAULT_DEVELOPMENT_TENANT_ID,
    DEFAULT_DEVELOPMENT_WORKSPACE_ID,
    TenantScope,
)
from backend.uat_fault_injection_service import UatFaultInjectionService
from backend.uat_http_workbench_service import default_transport, execute_workbench
from backend.uat_service import UatSettings

DB = Path.home() / "AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
SCOPE = TenantScope.local_development()
MODEL = "deepseek-v4-flash"


def credential() -> str:
    row = PersistentCredentialVault().load(CredentialScope(
        "windows-dev:lx", DEFAULT_DEVELOPMENT_TENANT_ID,
        DEFAULT_DEVELOPMENT_WORKSPACE_ID, "china_uat"), required=True)
    assert row
    return row[0]


def request_body(run_id: str) -> dict:
    return {
        "method": "POST", "environment_id": "china_uat",
        "path": "/v1/chat/completions", "query_params": [], "headers": [],
        "auth": {"method": "bearer", "header_name": "Authorization", "prefix": "Bearer"},
        "body": {"type": "json", "value": {
            "model": MODEL, "messages": [{"role": "user", "content": "请只回答：OK"}],
            "stream": False, "max_tokens": 16, "temperature": 0}},
        "content_type": "application/json", "timeout_seconds": 30,
        "stream": False, "model": MODEL, "model_selection_mode": "specified",
        "routing_policy": "probe_circuit_control", "request_type": "text",
        "probe_run_id": run_id, "traffic_class": "probe",
        "_strategy_variant": "probe_circuit_control",
    }


def main() -> None:
    repository = LiveAcceptanceService(DB, SCOPE)
    created = repository.create_probe_run({
        "task_name": "UAT受控故障注入熔断恢复", "environment_id": "china_uat",
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "database_watermark": 0,
        "configuration": {"models": [MODEL], "traffic_class": "probe",
                          "fault_injection": True, "cooldown_seconds": 10},
        "created_by": "cli-local-operator",
    })
    run_id = created["probe_run_id"]
    repository.update_probe(run_id, status="RUNNING")
    faults = UatFaultInjectionService(DB, SCOPE)
    circuit = CircuitBreakerService(DB, ROOT / "config/circuit_breaker_policy_v1.json")
    logs = CallLogService(DB, SCOPE)
    settings = replace(UatSettings.load("china_uat"), enabled=True, timeout_seconds=30)
    secret = credential()
    circuit_id = f"china_uat:probe:{run_id}:{MODEL}"
    rule = faults.create(
        acceptance_run_id=run_id, probe_run_id=run_id, traffic_class="probe",
        environment_id="china_uat", model=MODEL, error_type="503", count=5,
        delay_ms=0, ttl_seconds=120, error_layer="pre_request", affects_circuit=True)
    faults.enable(rule["fault_id"])
    repository.probe_event(run_id, "circuit_control_fault_enabled", "uat_fault_injection",
                           {"fault_id": rule["fault_id"], "count": 5})

    def injected(spec: dict, key: str) -> dict:
        outcome = faults.pre_request_outcome(
            environment_id="china_uat", model=MODEL, acceptance_run_id=run_id)
        if outcome.get("is_fault_injected"):
            return {"status": 503, "headers": {"content-type": "application/json"},
                    "body": b'{"error":{"code":"uat_injected_503"}}',
                    "elapsed_ms": 1, "_fault_metadata": outcome}
        return default_transport(spec, key)

    failed_ids: list[str] = []
    recovered_ids: list[str] = []
    opened: dict = {}
    recovered: dict = {}
    try:
        for index in range(5):
            admission = circuit.before_request(circuit_id, f"{run_id}:failure:{index}")
            result = execute_workbench(request_body(run_id), settings, secret,
                                       connection_verified=True, call_logs=logs,
                                       transport=injected)
            failed_ids.append(result["request_id"])
            state = circuit.record_failure(circuit_id, result["request_id"],
                                           admission.get("probe_lease_id"))
            repository.probe_event(run_id, "circuit_control_failure", "uat_fault_injection",
                                   {"request_id": result["request_id"],
                                    "state": state["state"], "index": index + 1})
        opened = circuit.get_state(circuit_id)
        repository.probe_event(run_id, "circuit_opened", "local_control_event",
                               {"state": opened["state"], "model": MODEL, "threshold": 5})
        time.sleep(int(circuit.policy["cooldown_seconds"]) + 1)
        faults.disable(rule["fault_id"])
        for index in range(2):
            admission = circuit.before_request(circuit_id, f"{run_id}:half-open:{index}")
            result = execute_workbench(request_body(run_id), settings, secret,
                                       connection_verified=True, call_logs=logs,
                                       transport=default_transport)
            recovered_ids.append(result["request_id"])
            state = circuit.record_success(circuit_id, result["request_id"],
                                           admission.get("probe_lease_id"))
            repository.probe_event(run_id, "half_open_probe", "provider_live",
                                   {"request_id": result["request_id"],
                                    "state": state["state"], "index": index + 1})
        recovered = circuit.get_state(circuit_id)
        repository.probe_event(run_id, "circuit_recovered", "local_control_event",
                               {"state": recovered["state"], "model": MODEL})
        repository.update_probe(run_id, status="COMPLETED", sent_count=7,
                                completed_count=7, success_count=2, failure_count=5,
                                pause_count=1, circuit_open_count=1, recovery_count=1)
    finally:
        for item in faults.list(acceptance_run_id=run_id):
            if item["enabled"]:
                faults.disable(item["fault_id"])
    enabled = sum(item["enabled"] for item in faults.list(acceptance_run_id=run_id))
    print(json.dumps({
        "probe_run_id": run_id, "circuit_id": circuit_id,
        "fault_id": rule["fault_id"], "opened": opened.get("state"),
        "recovered": recovered.get("state"), "failure_request_ids": failed_ids,
        "recovery_request_ids": recovered_ids, "enabled_fault_rules": enabled,
    }, ensure_ascii=True))


if __name__ == "__main__":
    main()
