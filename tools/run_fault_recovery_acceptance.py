"""Run bounded fault, fallback and circuit acceptance against China UAT.

Secret material is loaded from the encrypted Windows vault and never printed or
persisted in the evidence artifact.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.acceptance_run_service import AcceptanceRunService
from backend.call_log_service import CallLogService
from backend.circuit_breaker_service import CircuitBreakerService
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.tenant_security import DEFAULT_DEVELOPMENT_TENANT_ID, DEFAULT_DEVELOPMENT_WORKSPACE_ID, TenantScope
from backend.uat_fault_injection_service import UatFaultInjectionService
from backend.uat_http_workbench_service import default_transport, normalize_request
from backend.uat_live_recovery_service import UatLiveRecoveryService
from backend.uat_service import UatSettings


ROOT=Path(__file__).resolve().parents[1]
DB=Path.home()/"AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"
SCOPE=TenantScope.local_development()
PROMPT="请用一句简洁中文说明水循环包含蒸发、凝结、降水和地表径流。"


def key_from_vault()->str:
    scope=CredentialScope("windows-dev:lx",DEFAULT_DEVELOPMENT_TENANT_ID,
                          DEFAULT_DEVELOPMENT_WORKSPACE_ID,"china_uat")
    loaded=PersistentCredentialVault().load(scope,required=True)
    assert loaded is not None
    return loaded[0]


def body(model:str,run_id:str,*,stream:bool=False)->dict[str,Any]:
    return {"method":"POST","environment_id":"china_uat","path":"/v1/chat/completions",
      "query_params":[],"headers":[],"auth":{"method":"bearer","header_name":"Authorization","prefix":"Bearer"},
      "body":{"type":"json","value":{"model":model,"messages":[{"role":"user","content":PROMPT}],
        "stream":stream,"max_tokens":160,"temperature":0.2}},"content_type":"application/json",
      "timeout_seconds":45,"stream":stream,"model":model,"model_selection_mode":"specified",
      "routing_policy":"acceptance_recovery","request_type":"text","acceptance_run_id":run_id}


def category(status:int|None)->str|None:
    if status is None:return "network_error"
    if 200<=status<300:return None
    if status in {401,403}:return "user_authentication_error"
    if status==429:return "rate_limited"
    if status==524:return "upstream_timeout"
    if status>=500:return "upstream_5xx"
    return "user_parameter_error"


class AttemptExecutor:
    def __init__(self,key:str,run_id:str,faults:UatFaultInjectionService,stream:bool=False):
        self.key,self.run_id,self.faults,self.stream=key,run_id,faults,stream
        self.settings=replace(UatSettings.load("china_uat"),enabled=True)
    def __call__(self,model:str,_attempt:int)->dict[str,Any]:
        pre=self.faults.pre_request_outcome(environment_id="china_uat",model=model,
            acceptance_run_id=self.run_id)
        if pre.get("is_fault_injected"):
            kind=str(pre["error_type"]);delay=int(pre.get("delay_ms") or 0)
            if delay:time.sleep(delay/1000)
            if kind in {"connection_timeout","connection_reset"}:
                return {"execution_status":"failed","http_status":None,
                    "error_category":"network_timeout" if kind=="connection_timeout" else "network_error",
                    "total_latency_ms":delay,"error_source":"uat_fault_injection","is_fault_injected":True,
                    "fault_id":pre["fault_id"],"fault_audit_id":pre["audit_id"],
                    "fault_affects_circuit":pre["affects_circuit"],"output_started":False}
            if kind in {"sse_malformed","stream_interrupted"}:
                return {"execution_status":"failed","http_status":502,"error_category":"sse_protocol_error",
                    "total_latency_ms":delay,"error_source":"uat_fault_injection","is_fault_injected":True,
                    "fault_id":pre["fault_id"],"fault_audit_id":pre["audit_id"],
                    "fault_affects_circuit":pre["affects_circuit"],
                    "output_started":kind=="stream_interrupted"}
            status=int(kind) if kind.isdigit() else 502
            return {"execution_status":"failed","http_status":status,"error_category":category(status),
                "total_latency_ms":delay,"error_source":"uat_fault_injection","is_fault_injected":True,
                "fault_id":pre["fault_id"],"fault_audit_id":pre["audit_id"],
                "fault_affects_circuit":pre["affects_circuit"],"output_started":False}
        spec=normalize_request(body(model,self.run_id,stream=self.stream),self.settings)
        started=time.perf_counter()
        try: observed=default_transport(spec,self.key)
        except Exception:
            return {"execution_status":"failed","http_status":None,"error_category":"network_timeout",
                "total_latency_ms":(time.perf_counter()-started)*1000,"error_source":"provider_live",
                "is_fault_injected":False,"output_started":False}
        status=int(observed.get("status") or 0);raw=observed.get("body") or b""
        payload={}
        try:
            if self.stream:
                for line in raw.decode("utf-8","replace").splitlines():
                    if line.startswith("data:") and line[5:].strip() not in {"","[DONE]"}:
                        item=json.loads(line[5:].strip());payload.update({k:v for k,v in item.items() if k in {"id","model","usage"}})
            else:payload=json.loads(raw.decode("utf-8","replace"))
        except Exception:payload={}
        usage=payload.get("usage") if isinstance(payload.get("usage"),dict) else {}
        return {"execution_status":"success" if 200<=status<300 else "failed","http_status":status,
          "error_category":category(status),"total_latency_ms":float(observed.get("elapsed_ms") or 0),
          "first_token_latency_ms":observed.get("first_token_latency_ms"),"response_id":payload.get("id"),
          "actual_model":payload.get("model") or model,"input_tokens":usage.get("prompt_tokens"),
          "cached_input_tokens":((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
             if isinstance(usage.get("prompt_tokens_details"),dict) else None),
          "output_tokens":usage.get("completion_tokens"),"cost_amount":usage.get("cost"),
          "currency":"CNY" if usage.get("cost") is not None else None,
          "cost_source":"actual_provider_cost" if usage.get("cost") is not None else None,
          "error_source":"provider_live","is_fault_injected":False,
          "fault_affects_circuit":True,"output_started":bool(observed.get("first_token_latency_ms"))}


def create_fault(faults:UatFaultInjectionService,run_id:str,model:str,error_type:str,
                 count:int=1,*,affects:bool=True)->dict[str,Any]:
    rule=faults.create(acceptance_run_id=run_id,environment_id="china_uat",model=model,
      error_type=error_type,count=count,delay_ms=0,ttl_seconds=900,error_layer="pre_request",
      affects_circuit=affects)
    return faults.enable(rule["fault_id"])


def main()->None:
    runs=AcceptanceRunService(DB,SCOPE);run=runs.active_run("china_uat")
    if not run:raise RuntimeError("active_acceptance_run_required")
    run_id=run["acceptance_run_id"];key=key_from_vault();logs=CallLogService(DB,SCOPE)
    faults=UatFaultInjectionService(DB,SCOPE)
    circuit=CircuitBreakerService(DB,ROOT/"config/circuit_breaker_policy_v1.json")
    recovery=UatLiveRecoveryService(call_logs=logs,circuit_breakers=circuit,acceptance_runs=runs)
    cases={}
    def execute(name:str,models:list[str],*,stream:bool=False):
        cases[name]=recovery.execute(environment_id="china_uat",acceptance_run_id=run_id,
          models=models,stream=stream,endpoint_type="uat_http_post",strategy_variant=name,
          executor=AttemptExecutor(key,run_id,faults,stream),maximum_attempts=min(2,len(models)))
    execute("primary_success",["deepseek-v4-flash"])
    for name,kind in (("fallback_429","429"),("fallback_524","524")):
        create_fault(faults,run_id,"kimi-k3",kind)
        execute(name,["kimi-k3","deepseek-v4-flash"])
    create_fault(faults,run_id,"kimi-k3","400",affects=False);execute("parameter_400_no_fallback",["kimi-k3","deepseek-v4-flash"])
    create_fault(faults,run_id,"kimi-k3","401",affects=False);execute("user_401_no_fallback",["kimi-k3","deepseek-v4-flash"])
    create_fault(faults,run_id,"kimi-k3","stream_interrupted",affects=True);execute("stream_started_no_fallback",["kimi-k3","deepseek-v4-flash"],stream=True)
    create_fault(faults,run_id,"kimi-k3","503",count=2);create_fault(faults,run_id,"deepseek-v4-flash","503",count=2)
    execute("all_candidates_failed",["kimi-k3","deepseek-v4-flash"])
    # Open and recover a dedicated model-level circuit using UAT-labelled injected outcomes.
    create_fault(faults,run_id,"glm-5.2","503",count=5)
    for index in range(5):execute(f"circuit_failure_{index+1}",["glm-5.2"])
    opened=circuit.get_state("china_uat:model:glm-5.2")
    time.sleep(int(circuit.policy["cooldown_seconds"])+1)
    execute("half_open_probe_1",["glm-5.2"]);execute("half_open_probe_2",["glm-5.2"])
    recovered=circuit.get_state("china_uat:model:glm-5.2")
    disabled=[]
    for rule in faults.list(acceptance_run_id=run_id):
        if rule["enabled"]:disabled.append(faults.disable(rule["fault_id"])["fault_id"])
    evidence={"acceptance_run_id":run_id,"created_at":datetime.now(timezone.utc).isoformat(),
      "cases":cases,"circuit_open":opened,"circuit_recovered":recovered,
      "fault_audit":faults.list_audit(acceptance_run_id=run_id),"disabled_fault_ids":disabled}
    target=ROOT/"evidence"/"final_acceptance"/run_id;target.mkdir(parents=True,exist_ok=True)
    (target/"fault_fallback_circuit.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"case_count":len(cases),"circuit_open":opened["state"],
      "circuit_recovered":recovered["state"],"fault_audit_count":len(evidence["fault_audit"])},ensure_ascii=True))

if __name__=="__main__":main()
