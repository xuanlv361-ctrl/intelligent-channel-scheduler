"""Offline runtime for deterministic agent-plan execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from agent_executor import execute_agent
from agent_result_aggregator import aggregate_results
from online_evaluator import evaluate_response
from reflection_engine import load_policy as load_reflection_policy
from reflection_engine import reflect_execution
from self_correction import apply_correction


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "agent_runtime_policy_v1.json"
DEFAULT_OUTPUT = ROOT / "output" / "agent_execution_trace_v1.json"
ROLE_TASK_TYPES = {
    "research_agent": "reasoning",
    "coding_agent": "coding",
    "reasoning_agent": "reasoning",
    "writer_agent": "writing",
    "review_agent": "reasoning",
}


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        policy = json.load(handle)
    order = policy.get("execution_order", [])
    if len(order) != len(set(order)) or int(policy.get("max_agents", 0)) <= 0:
        raise ValueError("invalid agent runtime policy")
    if policy.get("failure_policy") not in {"continue_and_report", "stop_on_failure"}:
        raise ValueError("unsupported failure policy")
    return policy


def _ordered_agents(
    agents: list[Mapping[str, Any]], policy: Mapping[str, Any]
) -> list[dict[str, Any]]:
    order = {role: index for index, role in enumerate(policy["execution_order"])}
    if len(agents) > int(policy["max_agents"]):
        raise ValueError("agent plan exceeds configured max_agents")
    if any(str(row.get("role")) not in order for row in agents):
        raise ValueError("agent plan contains unsupported role")
    indexed = list(enumerate(agents))
    indexed.sort(key=lambda item: (order[str(item[1]["role"])], item[0]))
    return [dict(row) for _, row in indexed]


def _evaluation_reflection_cycle(
    result: dict[str, Any],
    *,
    reflection_policy: Mapping[str, Any],
) -> dict[str, Any]:
    if result["status"] != "success":
        return result
    task_type = ROLE_TASK_TYPES[result["role"]]
    round_number = 0
    while True:
        evaluation = evaluate_response({
            "task_type": task_type,
            "prompt": result["task"],
            "response": result["output"],
        })
        result["steps"].append({
            "type": "evaluation",
            "task_type": task_type,
            "score": evaluation["evaluation_score"],
            "issues": evaluation["issues"],
            "reflection_round": round_number,
        })
        reflection = reflect_execution({
            "task_type": task_type,
            "agent_result": result,
            "evaluation_result": evaluation,
            "reflection_round": round_number,
        }, policy=reflection_policy)
        result["steps"].append({
            "type": "reflection",
            "task_type": task_type,
            "decision": reflection["decision"],
            "confidence": reflection["confidence"],
            "reflection_round": round_number,
        })
        if reflection["decision"] != "correct":
            result["final_evaluation"] = evaluation
            result["final_reflection"] = reflection
            result["reflection_rounds"] = round_number
            result["final_status"] = (
                "accepted"
                if reflection["decision"] == "accept"
                else "retry_limit_reached"
            )
            return result
        correction = apply_correction({
            "original_result": result,
            "correction_plan": reflection["correction_plan"],
        })
        corrected = correction["corrected_result"]
        result["output"] = corrected["output"]
        result["steps"].append({
            "type": "correction",
            "actions": [
                change["action"] for change in correction["changes"]
            ],
            "status": correction["status"],
            "reflection_round": round_number + 1,
        })
        round_number += 1


def execute_plan(
    plan: Mapping[str, Any],
    *,
    execution_id: str = "EXEC-001",
    policy: Mapping[str, Any] | None = None,
    reflection_policy: Mapping[str, Any] | None = None,
    executor: Callable[..., dict[str, Any]] = execute_agent,
    formal_skill_runtime: Any | None = None,
) -> dict[str, Any]:
    """Dispatch a plan sequentially and produce a complete execution trace."""
    if not execution_id:
        raise ValueError("execution_id must be non-empty")
    policy = dict(policy or load_policy())
    reflection_policy = dict(reflection_policy or load_reflection_policy())
    mode = str(plan.get("execution_mode", ""))
    if mode not in {"single_model", "multi_agent"}:
        raise ValueError("execution_mode must be single_model or multi_agent")
    agents = list(plan.get("agents", []))
    if mode == "single_model" and not agents:
        agents = [{
            "role": "reasoning_agent",
            "task": str(plan.get("task", "respond to the user request")),
            **({"preferred_model": plan["selected_model"]}
               if plan.get("selected_model") else {}),
        }]
    ordered = _ordered_agents(agents, policy)
    results: list[dict[str, Any]] = []
    steps = []
    for index, agent in enumerate(ordered, 1):
        if formal_skill_runtime is None:
            result = executor(agent, context=results)
        else:
            result = executor(
                agent, context=results, formal_skill_runtime=formal_skill_runtime)
        result = _evaluation_reflection_cycle(
            result, reflection_policy=reflection_policy
        )
        results.append(result)
        steps.append({
            "step": index,
            "agent": result["role"],
            "model": result["selected_model"],
            "status": result["status"],
            "error": result.get("error"),
            "steps": result.get("steps", []),
        })
        if (
            result["status"] != "success"
            and policy["failure_policy"] == "stop_on_failure"
        ):
            break
    aggregate = aggregate_results(
        results, method=str(policy["aggregation_method"])
    )
    failed = any(row["status"] != "success" for row in results)
    return {
        "execution_id": execution_id,
        "status": "completed_with_errors" if failed else "completed",
        "execution_mode": mode,
        "agents": results,
        "steps": steps,
        "aggregate": aggregate,
        "policy_version": policy["policy_version"],
        "real_api_calls_performed": 0,
    }


def build_demo() -> dict[str, Any]:
    plan = {
        "execution_mode": "multi_agent",
        "agents": [
            {
                "role": "writer_agent",
                "task": "write the final offline summary from a document",
                "preferred_model": "qwen",
            },
            {
                "role": "research_agent",
                "task": "collect configured offline information",
                "preferred_model": "gemini-flash",
            },
            {
                "role": "reasoning_agent",
                "task": "calculate and analyze the offline information",
                "preferred_model": "claude-sonnet",
            },
        ],
    }
    return execute_plan(plan)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    result = build_demo()
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "status": result["status"], "execution_id": result["execution_id"],
        "step_count": len(result["steps"]), "real_api_calls_performed": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
