"""Deterministic offline mock executors for supported agent roles."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from agent_skill_router import select_formal_skills, select_tools
from model_capability_router import route_model
from tool_executor import execute_tool


ROLE_REQUIREMENTS = {
    "research_agent": {"long_context": 0.9, "reasoning": 0.6},
    "coding_agent": {"coding": 0.9, "reasoning": 0.7},
    "reasoning_agent": {"reasoning": 0.95, "math": 0.4},
    "writer_agent": {"reasoning": 0.4, "long_context": 0.5},
    "review_agent": {"reasoning": 0.8, "agent": 0.6},
}

MOCK_TEMPLATES = {
    "research_agent": "Offline research summary for: {task}. Evidence collection was simulated.",
    "coding_agent": "Offline implementation outline for: {task}. Code execution was not performed.",
    "reasoning_agent": "Offline reasoning analysis for: {task}. Assumptions were evaluated deterministically.",
    "writer_agent": "Offline drafted response for: {task}. It synthesizes available agent context.",
    "review_agent": "Offline review for: {task}. Completeness and consistency checks passed.",
}


def _tool_arguments(
    tool_id: str, task: str, agent: Mapping[str, Any]
) -> dict[str, Any]:
    overrides = agent.get("tool_arguments", {}).get(tool_id)
    if overrides is not None:
        return dict(overrides)
    defaults = {
        "calculator_tool": {"expression": "2 + 2"},
        "code_analyzer_tool": {
            "source_code": "def offline_example():\n    return 1\n"
        },
        "python_executor_tool": {
            "python_code": "print('offline simulation')"
        },
        "file_analyzer_tool": {"file_content": task},
        "search_mock_tool": {"query": task},
    }
    return defaults[tool_id]


def execute_agent(
    agent: Mapping[str, Any],
    *,
    context: list[Mapping[str, Any]] | None = None,
    model_router: Callable[..., dict[str, Any]] = route_model,
    formal_skill_runtime: Any | None = None,
    principal: Any | None = None,
) -> dict[str, Any]:
    """Route and execute one role as a deterministic offline mock."""
    role = str(agent.get("role", ""))
    task = str(agent.get("task", "")).strip()
    if role not in ROLE_REQUIREMENTS:
        raise ValueError(f"unsupported agent role: {role}")
    if not task:
        raise ValueError("agent task must be non-empty")
    routing = model_router({
        "task_type": role.removesuffix("_agent"),
        "requirements": ROLE_REQUIREMENTS[role],
    })
    requested_model = agent.get("model") or agent.get("preferred_model")
    available_models = {row["model"] for row in routing["ranking"]}
    if requested_model is not None and requested_model not in available_models:
        raise ValueError(f"unknown preferred model: {requested_model}")
    selected_model = str(requested_model or routing["selected_model"])
    tool_selection = select_tools(role, task)
    tool_results = []
    formal_skill_results = []
    execution_steps = []
    for selected_tool in tool_selection["selected_tools"]:
        tool_result = execute_tool(
            selected_tool["tool_id"],
            _tool_arguments(selected_tool["tool_id"], task, agent),
        )
        tool_results.append({
            **tool_result,
            "reason": selected_tool["reason"],
        })
        execution_steps.append({
            "type": "tool_call",
            "tool": selected_tool["tool_id"],
            "status": tool_result["status"],
            "execution_time": tool_result["execution_time"],
            "failure_reason": tool_result["failure_reason"],
        })
    if formal_skill_runtime is not None:
        selected = select_formal_skills(task, formal_skill_runtime)
        configured_arguments = agent.get("formal_skill_arguments", {})
        if not isinstance(configured_arguments, Mapping):
            raise ValueError("formal_skill_arguments must be an object")
        for skill_id in selected["selected_skills"]:
            arguments = configured_arguments.get(skill_id, {})
            if not isinstance(arguments, Mapping):
                raise ValueError("formal skill arguments must be objects")
            result = formal_skill_runtime.invoke_from_agent(
                skill_id=skill_id, arguments=dict(arguments),
                decision_id=str(agent.get("decision_id") or f"AGENT-{role}"),
                evidence_ids=list(agent.get("evidence_ids") or []),
                actor_id=role,
                principal=principal,
            )
            formal_skill_results.append(result)
            execution_steps.append({
                "type": "formal_skill_call", "skill": skill_id,
                "status": result["status"], "audit_id": result["audit_id"],
                "network_called": result["network_called"],
            })
    base = {
        "role": role,
        "task": task,
        "selected_model": selected_model,
        "router_selected_model": routing["selected_model"],
        "routing_source_type": routing["source_type"],
        "execution_type": (
            "hybrid_offline_formal_skills"
            if formal_skill_runtime is not None
            else "offline_deterministic_mock"
        ),
        "context_item_count": len(context or []) + len(tool_results) + len(formal_skill_results),
        "tool_results": tool_results,
        "formal_skill_results": formal_skill_results,
        "real_api_calls_performed": 0,
    }
    if agent.get("simulate_failure") is True:
        return {
            **base,
            "status": "failed",
            "output": "",
            "error": "simulated_agent_failure",
            "steps": execution_steps + [{
                "type": "agent_reasoning",
                "status": "failed",
                "failure_reason": "simulated_agent_failure",
            }],
        }
    return {
        **base,
        "status": "success",
        "output": str(
            agent.get("mock_output", MOCK_TEMPLATES[role].format(task=task))
        ),
        "error": None,
        "steps": execution_steps + [{
            "type": "agent_reasoning",
            "status": "success",
            "failure_reason": None,
        }],
    }
