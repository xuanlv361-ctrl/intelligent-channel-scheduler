"""Restore the pinned official MCP Builder Skill through the lifecycle service.

This maintenance command is idempotent and never writes lifecycle tables directly.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from backend.formal_agent_skill_lifecycle_service import FormalAgentSkillLifecycleService  # noqa: E402
SOURCE = "https://raw.githubusercontent.com/anthropics/skills/main/skills/mcp-builder/SKILL.md"


def main() -> None:
    with urllib.request.urlopen(SOURCE, timeout=30) as response:  # noqa: S310 - pinned HTTPS host
        content = response.read(1_000_000).decode("utf-8")
    service = FormalAgentSkillLifecycleService(ROOT / "data" / "routing_quality_console.sqlite3")
    current = {item["skill_id"]: item for item in service.list()["items"]}
    if "mcp-builder" not in current:
        service.import_external(
            skill_id="mcp-builder", display_name="MCP Builder",
            description="设计高质量 MCP Server 的 instruction-only Skill。",
            source_type="github_skill_md", source_uri=SOURCE, content=content,
            operator_id="system-maintenance", repository_commit="main",
            license_name="Complete terms in upstream LICENSE.txt")
    item = service.get("mcp-builder")["skill"]
    if item["version_status"] == "draft":
        service.transition_version("mcp-builder", item["version"], "validate", "system-maintenance")
        item = service.get("mcp-builder")["skill"]
    if item["version_status"] == "validated":
        service.transition_version("mcp-builder", item["version"], "publish", "system-maintenance")
    state = service.get("mcp-builder")["installation"]["installation_status"]
    if state in {"not_installed", "uninstalled"}:
        service.install_action("mcp-builder", "install", "system-maintenance",
                               version=service.get("mcp-builder")["skill"]["version"])
        state = service.get("mcp-builder")["installation"]["installation_status"]
    if state in {"installed", "disabled"}:
        service.install_action("mcp-builder", "enable", "system-maintenance")
    print("mcp-builder lifecycle ready")


if __name__ == "__main__":
    main()
