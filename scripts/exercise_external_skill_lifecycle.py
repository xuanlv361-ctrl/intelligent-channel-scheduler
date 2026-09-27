"""Exercise the approved instruction-only Skill lifecycle against persistent data."""
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"src"))
from backend.formal_agent_skill_audit_service import FormalAgentSkillAuditService
from backend.formal_agent_skill_lifecycle_service import FormalAgentSkillLifecycleService
from formal_agent_skill_registry import FormalAgentSkillRegistry
from formal_agent_skill_runtime import FormalAgentSkillRuntime

class Bindings: authorization_service=None

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--database",required=True);args=parser.parse_args()
    service=FormalAgentSkillLifecycleService(args.database);operator="local-console-operator"
    current=service.get("frontend-design")
    if current["skill"]["version_status"]=="draft":service.transition_version("frontend-design","0.1.0","validate",operator)
    if service.get("frontend-design")["skill"]["version_status"]=="validated":service.transition_version("frontend-design","0.1.0","publish",operator)
    installation=service.get("frontend-design")["installation"]
    if installation["installation_status"] in {"not_installed","uninstalled"}:service.install_action("frontend-design","install",operator,"0.1.0")
    if service.get("frontend-design")["installation"]["installation_status"]=="installed":service.install_action("frontend-design","enable",operator)
    runtime=FormalAgentSkillRuntime.build(bindings=Bindings(),audit=FormalAgentSkillAuditService(args.database),
      registry=FormalAgentSkillRegistry(),lifecycle=service,managed_executor=lambda **_:{
        "status":"success","execution_mode":"instruction_only","network_called":False,
        "write_performed":False,"message":"指令已由Formal Runtime安全加载"})
    invoked=runtime.invoke_managed_from_agent(skill_id="frontend-design",arguments={},actor_id=operator)
    source=service.get("frontend-design")["source"]["content_text"]
    if not any(v["version"]=="0.2.0" for v in service.get("frontend-design")["versions"]):
        service.create_version("frontend-design",version="0.2.0",content=source+"\n\nLifecycle validation revision.",operator_id=operator,change_summary="验证升级与回滚")
        service.transition_version("frontend-design","0.2.0","validate",operator);service.transition_version("frontend-design","0.2.0","publish",operator)
    service.install_action("frontend-design","upgrade",operator,"0.2.0")
    service.install_action("frontend-design","rollback",operator,"0.1.0")
    service.install_action("frontend-design","disable",operator);service.install_action("frontend-design","enable",operator)
    service.install_action("frontend-design","disable",operator);service.install_action("frontend-design","uninstall",operator)
    print(json.dumps({"invocation_id":invoked["invocation_id"],"audit_id":invoked["audit_id"],
      "network_called":invoked["network_called"],"final":service.get("frontend-design")["installation"],
      "audit_events":len(service.audits())},ensure_ascii=False))
if __name__=="__main__":main()
