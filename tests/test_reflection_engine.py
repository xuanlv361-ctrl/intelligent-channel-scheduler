import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from reflection_engine import reflect_execution


POLICY = {
    "policy_version": "test-policy",
    "max_reflection_rounds": 2,
    "accept_threshold": 0.8,
    "retry_enabled": True,
}


def payload(score, round_number=0, task_type="qa", issues=None):
    return {
        "task_type": task_type,
        "agent_result": {"output": "offline"},
        "evaluation_result": {
            "evaluation_score": score,
            "issues": issues or [],
        },
        "reflection_round": round_number,
    }


def test_good_response_is_accepted():
    result = reflect_execution(payload(0.9), POLICY)
    assert result["decision"] == "accept"
    assert result["correction_plan"] == []


def test_bad_response_is_corrected():
    result = reflect_execution(
        payload(0.5, task_type="coding", issues=["code incomplete"]), POLICY
    )
    assert result["decision"] == "correct"
    assert result["correction_plan"] == ["coding_error"]


def test_retry_limit_is_respected():
    result = reflect_execution(payload(0.5, round_number=2), POLICY)
    assert result["decision"] == "retry_limit_reached"


def test_reflection_is_deterministic():
    value = payload(0.6, issues=["missing required information"])
    assert reflect_execution(value, POLICY) == reflect_execution(value, POLICY)
