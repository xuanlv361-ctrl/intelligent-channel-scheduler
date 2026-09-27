"""One-time idempotent import of the five approved external Skill sources."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.formal_agent_skill_lifecycle_service import FormalAgentSkillLifecycleService

ANTHROPIC_RAW = "https://raw.githubusercontent.com/anthropics/skills/main/skills"


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "IntelligentChannelScheduler/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    service = FormalAgentSkillLifecycleService(args.database)
    frontend = Path(r"C:\Users\LX\.codex\skills\frontend-design\SKILL.md").read_text("utf-8")
    react = Path(r"C:\Users\LX\.codex\.tmp\plugins\plugins\vercel\skills\react-best-practices\SKILL.md").read_text("utf-8")
    webapp = fetch(f"{ANTHROPIC_RAW}/webapp-testing/SKILL.md")
    mcp = fetch(f"{ANTHROPIC_RAW}/mcp-builder/SKILL.md")
    sources = [
        dict(skill_id="frontend-design", display_name="Frontend Design",
             description="前端页面设计与布局规范", source_type="github_skill_md",
             source_uri="https://github.com/anthropics/skills/tree/main/skills/frontend-design",
             content=frontend, license_name="Complete terms in LICENSE.txt"),
        dict(skill_id="react-best-practices", display_name="React Best Practices",
             description="React组件、性能和工程规范", source_type="github_skill_md",
             source_uri="https://github.com/vercel/vercel-plugin/tree/main/skills/react-best-practices",
             content=react, license_name="Apache-2.0"),
        dict(skill_id="webapp-testing", display_name="Webapp Testing",
             description="Web页面测试和端到端测试方法", source_type="github_skill_md",
             source_uri="https://github.com/anthropics/skills/tree/main/skills/webapp-testing",
             content=webapp, license_name="Complete terms in LICENSE.txt",
             scripts=["scripts/with_server.py"], references=["examples/"]),
        dict(skill_id="mcp-builder", display_name="MCP Builder",
             description="外部Host、MCP和工具协议设计", source_type="github_skill_md",
             source_uri="https://github.com/anthropics/skills/tree/main/skills/mcp-builder",
             content=mcp, license_name="Complete terms in LICENSE.txt",
             references=["reference/mcp_best_practices.md", "reference/node_mcp_server.md",
                         "reference/python_mcp_server.md", "reference/evaluation.md"]),
    ]
    imported = []
    for source in sources:
        imported.append(service.import_external(operator_id="system-approved-import", **source)["skill"]["skill_id"])
    toml_path = Path(r"C:\Users\LX\.codex\agents\frontend-developer.toml")
    parsed = tomllib.loads(toml_path.read_text("utf-8"))
    selected = {key: parsed.get(key) for key in ("name", "description", "developer_instructions")}
    service.import_external(
        skill_id="frontend-developer-toml", display_name=str(selected["name"]),
        description=str(selected["description"]), source_type="codex_agent_toml",
        source_uri=str(toml_path), content="\n\n".join(str(value or "") for value in selected.values()),
        operator_id="system-approved-import", toml_source=selected)
    imported.append("frontend-developer-toml")
    print(json.dumps({"imported": imported, "summary": service.list()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
