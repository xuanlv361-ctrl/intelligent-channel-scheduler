from __future__ import annotations

import pytest

from backend.formal_agent_skill_business_executor import FormalAgentSkillBusinessExecutor


def executor(instruction_executor=None) -> FormalAgentSkillBusinessExecutor:
    return FormalAgentSkillBusinessExecutor(
        call_logs=None, config_review=None, circuit_breakers=None,
        traffic_governance=None, validate_uat=lambda value: value,
        execute_uat=lambda value: value, instruction_executor=instruction_executor)


def test_lifecycle_validation_does_not_claim_business_success() -> None:
    result = executor()(skill_id="mcp-builder", principal=None,
                        skill={"binding": "instruction_only"},
                        arguments={"invocation_mode": "lifecycle_validation"})
    assert result["status"] == "conditions_validated"
    assert result["business_executed"] is False
    assert result["network_called"] is False


def test_instruction_only_function_requires_user_task() -> None:
    with pytest.raises(ValueError, match="instruction_skill_task_required"):
        executor(lambda **_: {"content": "unexpected"})(
            skill_id="mcp-builder", principal=None, skill={}, arguments={})


def test_instruction_only_function_requires_execution_engine() -> None:
    with pytest.raises(ValueError, match="execution_engine_not_configured"):
        executor()(skill_id="mcp-builder", principal=None, skill={},
                   arguments={"task": "design a server"})


def test_instruction_only_function_returns_nonempty_business_artifact() -> None:
    result = executor(lambda **_: {
        "status": "success", "business_executed": True,
        "content": "# MCP Server\nA real artifact", "result_summary": "MCP Server",
        "network_called": True, "write_performed": False,
    })(skill_id="mcp-builder", principal=None,
       skill={"instructions": "Build secure MCP servers"},
       arguments={"task": "design a model-health MCP server"})
    assert result["business_executed"] is True
    assert "MCP Server" in result["content"]


def test_instruction_only_empty_engine_output_is_rejected() -> None:
    with pytest.raises(ValueError, match="instruction_skill_empty_result"):
        executor(lambda **_: {"status": "success", "content": ""})(
            skill_id="frontend-design", principal=None, skill={},
            arguments={"task": "redesign the page"})
