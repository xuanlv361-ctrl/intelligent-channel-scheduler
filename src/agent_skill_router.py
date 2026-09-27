"""Explainable deterministic routing from agent tasks to offline mock tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "data" / "tool_catalog_v1.json"
DEFAULT_POLICY = ROOT / "config" / "agent_skill_policy_v1.json"


def _load(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def select_tools(
    agent_role: str,
    task: str,
    available_tools: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select configured tools in stable preference order."""
    policy = dict(policy or _load(DEFAULT_POLICY))
    preferences = policy.get("role_preferences", {})
    if agent_role not in preferences:
        raise ValueError(f"unsupported agent role: {agent_role}")
    catalog = available_tools or _load(DEFAULT_CATALOG)
    tools = catalog.get("tools", []) if isinstance(catalog, Mapping) else catalog
    available = {str(row["tool_id"]): row for row in tools}
    task_text = str(task).casefold()
    selected = []
    for index, tool_id in enumerate(preferences[agent_role]):
        if tool_id not in available:
            continue
        keywords = policy.get("tool_keywords", {}).get(tool_id, [])
        matched = [word for word in keywords if word.casefold() in task_text]
        always = (
            agent_role in policy.get("always_select_first_preference", [])
            and index == 0
        )
        if not matched and not always:
            continue
        reason = (
            f"matched task indicators: {', '.join(matched)}"
            if matched else f"default preferred tool for {agent_role}"
        )
        selected.append({
            "tool_id": tool_id,
            "tool_name": available[tool_id]["tool_name"],
            "reason": reason,
        })
    return {
        "agent_role": agent_role,
        "selected_tools": selected,
        "policy_version": policy["policy_version"],
        "selection_type": "offline_deterministic_tool_routing",
    }


def select_formal_skills(task: str, runtime: Any) -> dict[str, Any]:
    """Discover formal Skills through their validated runtime registry."""
    selected = runtime.select_for_task(str(task))
    return {
        "selected_skills": selected,
        "selection_type": "formal_registered_read_only_skill_routing",
        "registry_version": runtime.registry.document["registry_version"],
    }
