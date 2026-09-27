import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_runtime import execute_plan


def test_single_model_task_still_executes():
    result = execute_plan({
        "execution_mode": "single_model",
        "task": "answer a simple question",
    })
    assert result["status"] == "completed"
    assert len(result["agents"]) == 1
    assert result["agents"][0]["status"] == "success"


def test_multi_agent_executes_in_deterministic_order():
    plan = {
        "execution_mode": "multi_agent",
        "agents": [
            {"role": "writer_agent", "task": "write", "model": "qwen"},
            {"role": "research_agent", "task": "research", "model": "gemini-flash"},
            {"role": "reasoning_agent", "task": "reason", "model": "claude-sonnet"},
        ],
    }
    first = execute_plan(plan)
    second = execute_plan(plan)
    assert [step["agent"] for step in first["steps"]] == [
        "research_agent", "reasoning_agent", "writer_agent"
    ]
    assert first == second
    assert first["aggregate"]["successful_agent_count"] == 3


def test_failed_agent_is_handled_and_trace_is_complete():
    plan = {
        "execution_mode": "multi_agent",
        "agents": [
            {"role": "research_agent", "task": "research", "simulate_failure": True},
            {"role": "writer_agent", "task": "write"},
        ],
    }
    result = execute_plan(plan)
    assert result["status"] == "completed_with_errors"
    assert len(result["steps"]) == 2
    assert result["steps"][0]["error"] == "simulated_agent_failure"
    assert result["aggregate"]["failed_agents"] == ["research_agent"]
    assert all("status" in step and "model" in step for step in result["steps"])
    json.dumps(result)


def test_tool_aware_execution_injects_results_and_traces_calls():
    result = execute_plan({
        "execution_mode": "multi_agent",
        "agents": [{
            "role": "coding_agent",
            "task": "Analyze this source code and run a Python test.",
            "tool_arguments": {
                "code_analyzer_tool": {"source_code": "def f():\n    return 1\n"},
                "python_executor_tool": {"python_code": "print(2 + 2)"},
            },
        }],
    })
    agent = result["agents"][0]
    assert [row["tool_id"] for row in agent["tool_results"]] == [
        "code_analyzer_tool", "python_executor_tool"
    ]
    assert [row["type"] for row in agent["steps"][:3]] == [
        "tool_call", "tool_call", "agent_reasoning"
    ]
    assert result["steps"][0]["steps"] == agent["steps"]
    assert agent["context_item_count"] == 2


def test_reflection_accepts_successful_first_attempt():
    response = (
        "First, the requirements are identified because they define success. "
        "Second, evidence and assumptions are compared carefully. However, "
        "uncertainty remains explicit. Therefore the answer provides a complete "
        "conclusion, validation approach, limitations, and next steps for the "
        "offline task without claiming external execution."
    )
    result = execute_plan({
        "execution_mode": "single_model",
        "agents": [{
            "role": "reasoning_agent", "task": "analyze the request",
            "mock_output": response,
        }],
    })
    agent = result["agents"][0]
    assert agent["final_status"] == "accepted"
    assert agent["reflection_rounds"] == 0
    assert [row["type"] for row in agent["steps"][-2:]] == [
        "evaluation", "reflection"
    ]


def test_reflection_correction_and_re_evaluation_are_traced():
    result = execute_plan({
        "execution_mode": "single_model",
        "agents": [{
            "role": "coding_agent", "task": "write code",
            "mock_output": "Not implemented.",
        }],
    })
    agent = result["agents"][0]
    types = [row["type"] for row in agent["steps"]]
    assert "correction" in types
    assert types.count("evaluation") >= 2
    assert types.count("reflection") >= 2
    assert agent["final_status"] == "accepted"


def test_runtime_retry_limit_is_traced():
    result = execute_plan(
        {
            "execution_mode": "single_model",
            "agents": [{
                "role": "writer_agent", "task": "write",
                "mock_output": "Too short.",
            }],
        },
        reflection_policy={
            "policy_version": "limit-test",
            "max_reflection_rounds": 0,
            "accept_threshold": 0.99,
            "retry_enabled": True,
        },
    )
    agent = result["agents"][0]
    assert agent["final_status"] == "retry_limit_reached"
    assert agent["final_reflection"]["decision"] == "retry_limit_reached"
    assert [row["type"] for row in agent["steps"][-2:]] == [
        "evaluation", "reflection"
    ]
