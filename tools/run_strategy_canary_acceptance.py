"""Create a real config version and exercise China-UAT model canary/rollback."""
from __future__ import annotations
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from backend.acceptance_run_service import AcceptanceRunService
from backend.call_log_service import CallLogService
from backend.config_review_service import ConfigReviewService
from backend.persistent_credential_vault import CredentialScope,PersistentCredentialVault
from backend.tenant_security import DEFAULT_DEVELOPMENT_TENANT_ID,DEFAULT_DEVELOPMENT_WORKSPACE_ID,TenantScope
from backend.traffic_change_governance_service import TrafficChangeGovernanceService
from backend.circuit_breaker_service import CircuitBreakerService
from backend.uat_fault_injection_service import UatFaultInjectionService
from backend.uat_live_recovery_service import UatLiveRecoveryService
from tools.run_fault_recovery_acceptance import AttemptExecutor,create_fault
from backend.uat_http_workbench_service import execute_workbench
from backend.uat_service import UatSettings

ROOT=Path(__file__).resolve().parents[1];DB=Path.home()/"AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3";SCOPE=TenantScope.local_development()
PROMPT="请用简洁中文解释水循环，必须包含蒸发、凝结、降水和地表径流四个阶段，最后用一句话总结。不得超过300字。"

def credential()->str:
 scope=CredentialScope("windows-dev:lx",DEFAULT_DEVELOPMENT_TENANT_ID,DEFAULT_DEVELOPMENT_WORKSPACE_ID,"china_uat");loaded=PersistentCredentialVault().load(scope,required=True);assert loaded;return loaded[0]

def live(key:str,run_id:str,proposal_id:str,model:str,strategy:str)->dict[str,Any]:
 body={"method":"POST","environment_id":"china_uat","path":"/v1/chat/completions","query_params":[],"headers":[],
  "auth":{"method":"bearer","header_name":"Authorization","prefix":"Bearer"},"body":{"type":"json","value":{"model":model,"messages":[{"role":"user","content":PROMPT}],"stream":False,"max_tokens":256,"temperature":0.2}},
  "content_type":"application/json","timeout_seconds":60,"stream":False,"model":model,"model_selection_mode":"specified",
  "routing_policy":strategy,"request_type":"text","acceptance_run_id":run_id,"_traffic_proposal_id":proposal_id,"_strategy_variant":strategy}
 result=execute_workbench(body,replace(UatSettings.load("china_uat"),enabled=True),key,connection_verified=True,call_logs=CallLogService(DB,SCOPE))
 CallLogService(DB,SCOPE).save_routing_decision(result=result,routing_decision={"policy":strategy,"candidates":[{"model_id":model,"score":1.0,"reason":"canary_assignment"}],"excluded":[],"selected_model":model,"selection_reason":"model_level_canary_assignment","confidence":"high","catalog_candidate_count":1})
 AcceptanceRunService(DB,SCOPE).link_evidence(run_id,request_id=result.get("request_id"),decision_id=result.get("decision_id"))
 return {k:result.get(k) for k in ("execution_status","request_id","decision_id","response_id","actual_model","http_status","total_latency_ms","first_token_latency_ms","input_tokens","cached_input_tokens","output_tokens","cost_amount","currency")}

def main()->None:
 runs=AcceptanceRunService(DB,SCOPE);run=runs.active_run("china_uat");assert run;run_id=run["acceptance_run_id"]
 config=ConfigReviewService(DB,ROOT/"config",SCOPE);active=next(v for v in config.versions() if v["is_active"])
 candidate=config.create_version(base_version=active["configuration_version"],patch={"timeout":55},created_by="acceptance-operator",change_reason=f"{run_id} UAT model canary candidate")
 traffic=TrafficChangeGovernanceService(DB,ROOT/"config/traffic_change_governance_policy_v1.json")
 payload={"environment_mode":"china_uat","source_model_id":"deepseek-v4-flash","target_model_id":"glm-5.2",
  "source_policy_version":active["configuration_version"],"target_policy_version":candidate["configuration_version"],
  "rollout_percent":5,"reason":f"{run_id} model-level UAT canary","observation_seconds":900,"minimum_sample_count":1,
  "stop_conditions":{"error_rate_above":0.01,"consecutive_failures":1},"rollback_condition":"error rate threshold",
  "acceptance_run_id":run_id,"baseline_binding":{"model_id":"deepseek-v4-flash","policy_version":active["configuration_version"]},
  "candidate_binding":{"model_id":"glm-5.2","policy_version":candidate["configuration_version"]}}
 draft=traffic.create_control_proposal(payload,actor_id="acceptance-operator");pid=draft["proposal_id"]
 states=[draft["state"],traffic.control_validate(pid,actor_id="acceptance-operator")["state"],traffic.control_submit(pid,actor_id="acceptance-operator")["state"],traffic.control_approve(pid,actor_id="acceptance-approver",approval_reference=f"UAT-{run_id}")["state"],traffic.control_activate(pid,actor_id="acceptance-operator")["state"]]
 key=credential();calls=[];history=[]
 for percent in (5,10,25):
  if percent!=5:traffic.control_adjust(pid,actor_id="acceptance-operator",rollout_percent=percent)
  selected=None
  for index in range(1000):
   assignment=traffic.control_assignment(f"{run_id}:{percent}:{index}",proposal_id=pid)
   if assignment["variant"]=="candidate":selected=assignment;break
  assert selected
  result=live(key,run_id,pid,"glm-5.2",f"canary_{percent}");traffic.control_assignment(selected["assignment_key"],proposal_id=pid,request_id=result["request_id"],decision_id=result["decision_id"])
  calls.append(result);history.append({"percent":percent,"assignment":selected,"metrics":traffic.control_read_metrics(pid)})
 # Add one explicitly labelled UAT fault under this proposal so the real-log
 # monitor, rather than a front-end state change, triggers the stop condition.
 faults=UatFaultInjectionService(DB,SCOPE);create_fault(faults,run_id,"glm-5.2","503",affects=True)
 recovery=UatLiveRecoveryService(call_logs=CallLogService(DB,SCOPE),
   circuit_breakers=CircuitBreakerService(DB,ROOT/"config/circuit_breaker_policy_v1.json"),acceptance_runs=runs)
 failed=recovery.execute(environment_id="china_uat",acceptance_run_id=run_id,models=["glm-5.2"],
   stream=False,endpoint_type="uat_http_post",strategy_variant="canary_auto_stop_fault",
   executor=AttemptExecutor(key,run_id,faults,False),maximum_attempts=1,traffic_proposal_id=pid)
 traffic.control_assignment(f"{run_id}:auto-stop-fault",proposal_id=pid,
   request_id=failed["request_id"],decision_id=failed["decision_id"])
 calls.append(failed)
 stopped=traffic.control_evaluate_stop(pid,actor_id="acceptance-monitor")
 if stopped["proposal_state"]!="AUTO_STOPPED":
  traffic.control_pause(pid,actor_id="acceptance-operator")
 rolled=traffic.control_rollback(pid,actor_id="acceptance-operator",reason="acceptance automatic stop rollback")
 rollback_config=config.rollback(target_version=active["configuration_version"],active_version=candidate["configuration_version"],created_by="acceptance-operator",change_reason=f"{run_id} restore baseline")
 evidence={"acceptance_run_id":run_id,"baseline_version":active["configuration_version"],"candidate_version":candidate["configuration_version"],"candidate_diff":candidate["diff"],"rollback_configuration_version":rollback_config["configuration_version"],"proposal_id":pid,"states":states,"rollout_history":history,"calls":calls,"auto_stop_fault":failed,"stop_result":stopped,"rollback":rolled,"audit":traffic.control_audit(pid)}
 target=ROOT/"evidence"/"final_acceptance"/run_id/"strategy_canary_rollback.json";target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
 print(json.dumps({"proposal_id":pid,"states":states,"call_count":len(calls),"stop_state":stopped["proposal_state"],"rollback_state":rolled["state"],"baseline":active["configuration_version"],"candidate":candidate["configuration_version"],"rollback_config":rollback_config["configuration_version"]},ensure_ascii=True))
if __name__=="__main__":main()
