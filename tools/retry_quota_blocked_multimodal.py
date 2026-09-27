"""Retry only the quota-blocked audio and video China-UAT evidence."""
from __future__ import annotations

import json
from pathlib import Path

from backend.acceptance_run_service import AcceptanceRunService
from backend.persistent_credential_vault import CredentialScope, PersistentCredentialVault
from backend.tenant_security import DEFAULT_DEVELOPMENT_TENANT_ID, DEFAULT_DEVELOPMENT_WORKSPACE_ID, TenantScope
from tools.run_multimodal_live_acceptance import audio_case, workbench_case

ROOT=Path(__file__).resolve().parents[1]
DB=Path.home()/"AppData/Local/IntelligentChannelScheduler/data/routing_quality_console.sqlite3"

def main()->None:
    runs=AcceptanceRunService(DB,TenantScope.local_development())
    run=runs.active_run("china_uat")
    if not run:raise RuntimeError("active_acceptance_run_required")
    scope=CredentialScope("windows-dev:lx",DEFAULT_DEVELOPMENT_TENANT_ID,
      DEFAULT_DEVELOPMENT_WORKSPACE_ID,"china_uat")
    loaded=PersistentCredentialVault().load(scope,required=True);assert loaded is not None
    key=loaded[0];run_id=run["acceptance_run_id"]
    result={"audio":audio_case(key,run_id),"video":workbench_case(key,run_id,
      model="dreamina-seedance-2-0-fast-260128",path="/v1/videos",request_type="video",timeout=60,
      payload={"model":"dreamina-seedance-2-0-fast-260128",
        "prompt":"生成一段高质量短视频：从海面蒸发开始，水汽形成云，随后降雨，雨水经过河流回到海洋。画面清晰、阶段明确、无人物、无品牌标志。",
        "seconds":"4","size":"1280x720"})}
    target=ROOT/"evidence"/"final_acceptance"/run_id/"multimodal_quota_retry.json"
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:{f:v.get(f) for f in ("execution_status","request_id","decision_id","response_id","http_status","total_latency_ms")} for k,v in result.items()},ensure_ascii=True))

if __name__=="__main__":main()
