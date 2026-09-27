"""Import and enable the official Frontend Design instruction Skill idempotently."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from backend.formal_agent_skill_lifecycle_service import FormalAgentSkillLifecycleService  # noqa: E402

SOURCE = "https://raw.githubusercontent.com/anthropics/skills/main/skills/frontend-design/SKILL.md"


def main() -> None:
    service = FormalAgentSkillLifecycleService(
        ROOT / "data" / "routing_quality_console.sqlite3"
    )
    current = {item["skill_id"]: item for item in service.list()["items"]}
    if "frontend-design" not in current:
        with urllib.request.urlopen(SOURCE, timeout=30) as response:  # noqa: S310
            content = response.read(1_000_000).decode("utf-8")
        service.import_external(
            skill_id="frontend-design",
            display_name="Frontend Design",
            description="用于生成有明确视觉层级、响应式边界和实现细节的前端设计方案。",
            source_type="github_skill_md",
            source_uri=SOURCE,
            content=content,
            operator_id="system-maintenance",
            repository_commit="main",
            license_name="Complete terms in upstream LICENSE.txt",
        )
    item = service.get("frontend-design")["skill"]
    if item["version_status"] == "draft":
        service.transition_version(
            "frontend-design", item["version"], "validate", "system-maintenance"
        )
        item = service.get("frontend-design")["skill"]
    if item["version_status"] == "validated":
        service.transition_version(
            "frontend-design", item["version"], "publish", "system-maintenance"
        )
    installation = service.get("frontend-design")["installation"]
    state = installation["installation_status"]
    if state in {"not_installed", "uninstalled"}:
        service.install_action(
            "frontend-design",
            "install",
            "system-maintenance",
            version=service.get("frontend-design")["skill"]["version"],
        )
        state = service.get("frontend-design")["installation"]["installation_status"]
    if state in {"installed", "disabled"}:
        service.install_action("frontend-design", "enable", "system-maintenance")
    print("frontend-design lifecycle ready")


if __name__ == "__main__":
    main()
