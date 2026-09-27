"""Offline deterministic planning for single-model or multi-agent execution."""

from __future__ import annotations

from typing import Any, Mapping


ROLE_MODELS = {
    "research_agent": "gemini-flash",
    "coding_agent": "deepseek-reasoner",
    "reasoning_agent": "claude-sonnet",
    "writer_agent": "qwen",
    "review_agent": "gpt-4o-4.1",
}


def _agent(role: str, task: str) -> dict[str, str]:
    return {"role": role, "task": task, "preferred_model": ROLE_MODELS[role]}


def plan_agents(request_profile: Mapping[str, Any]) -> dict[str, Any]:
    """Create a stable execution plan without invoking agents or models."""
    complexity = request_profile.get("complexity", request_profile)
    level = str(complexity.get("complexity_level", "low"))
    scores = complexity.get("scores", {})
    task_type = str(request_profile.get("task_type", "qa"))
    if level not in {"low", "medium", "high"}:
        raise ValueError("complexity_level must be low, medium, or high")

    multi_agent = (
        level == "high"
        or task_type in {"research", "agent"}
        or float(scores.get("tool_need", 0)) >= 0.6
    )
    if not multi_agent:
        return {
            "execution_mode": "single_model",
            "agents": [],
            "reason": f"{level} complexity does not require task decomposition",
            "source_type": "offline_deterministic_plan",
        }

    agents = []
    if task_type in {"research", "long_document", "agent"} or float(
        scores.get("context", 0)
    ) >= 0.6:
        agents.append(_agent("research_agent", "collect and organize evidence"))
    if task_type == "coding" or float(scores.get("coding", 0)) >= 0.6:
        agents.append(_agent("coding_agent", "implement and verify code"))
    if float(scores.get("reasoning", 0)) >= 0.45 or task_type in {
        "reasoning", "research", "agent"
    }:
        agents.append(_agent("reasoning_agent", "analyze evidence and trade-offs"))
    if task_type in {"writing", "research", "long_document"} or float(
        scores.get("output", 0)
    ) >= 0.6:
        agents.append(_agent("writer_agent", "compose the requested deliverable"))
    agents.append(_agent("review_agent", "check completeness and consistency"))
    return {
        "execution_mode": "multi_agent",
        "agents": agents,
        "reason": "complex request decomposed into specialized offline roles",
        "source_type": "offline_deterministic_plan",
    }
