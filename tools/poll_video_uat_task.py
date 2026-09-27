"""Poll the submitted China-UAT video task to a terminal state."""
from __future__ import annotations
import json,time
from dataclasses import replace
from pathlib import Path
from backend.acceptance_run_service import AcceptanceRunService
from backend.call_log_service import CallLogService
from backend.persistent_credential_vault import CredentialScope,PersistentCredentialVault
from backend.tenant_security import DEFAULT_DEVELOPMENT_TENANT_ID,DEFAULT_DEVELOPMENT_WORKSPACE_ID,TenantScope
from backend.uat_http_workbench_service import execute_workbench
from backend.uat_service import UatSettings

ROOT=Path(__file__).resolve().parents[1];DB=Path.home()/"AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3";SCOPE=TenantScope.local_development()
def main()->None:
 run=AcceptanceRunService(DB,SCOPE).active_run("china_uat");assert run
 source=json.loads((ROOT/"evidence"/"final_acceptance"/run["acceptance_run_id"]/"multimodal_quota_retry.json").read_text(encoding="utf-8"))
 task=str(source["video"]["response_id"]);scope=CredentialScope("windows-dev:lx",DEFAULT_DEVELOPMENT_TENANT_ID,DEFAULT_DEVELOPMENT_WORKSPACE_ID,"china_uat")
 loaded=PersistentCredentialVault().load(scope,required=True);assert loaded
 polls=[]
 for _ in range(24):
  body={"method":"GET","environment_id":"china_uat","path":f"/v1/videos/{task}","query_params":[],"headers":[],
   "auth":{"method":"bearer","header_name":"Authorization","prefix":"Bearer"},"body":{"type":"none","value":None},
   "timeout_seconds":30,"stream":False,"model":"dreamina-seedance-2-0-fast-260128","model_selection_mode":"specified",
   "routing_policy":"video_task_poll","request_type":"video","acceptance_run_id":run["acceptance_run_id"]}
  result=execute_workbench(body,replace(UatSettings.load("china_uat"),enabled=True),loaded[0],connection_verified=True,call_logs=CallLogService(DB,SCOPE))
  excerpt=str(result.get("response_body") or "");status="unknown"
  try: status=str(json.loads(excerpt).get("status") or "unknown").casefold()
  except Exception: pass
  polls.append({k:result.get(k) for k in ("request_id","decision_id","response_id","http_status","total_latency_ms","cost_amount","currency")}|{"task_status":status,"response_excerpt":excerpt[:1000]})
  AcceptanceRunService(DB,SCOPE).link_evidence(run["acceptance_run_id"],request_id=result.get("request_id"),decision_id=result.get("decision_id"))
  if status in {"completed","succeeded","success","failed","cancelled","canceled"} or result.get("execution_status")!="success":break
  time.sleep(5)
 target=ROOT/"evidence"/"final_acceptance"/run["acceptance_run_id"]/"video_task_poll.json";target.write_text(json.dumps({"task_id":task,"polls":polls},ensure_ascii=False,indent=2),encoding="utf-8")
 print(json.dumps({"task_id":task,"poll_count":len(polls),"final_status":polls[-1]["task_status"],"http_status":polls[-1]["http_status"]},ensure_ascii=True))
if __name__=="__main__":main()
