import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from online_evaluator import evaluate_response


def test_high_quality_response_scores_higher():
    prompt = "Write a Python add function with a test."
    high = evaluate_response({
        "task_type": "coding", "prompt": prompt,
        "response": (
            "```python\ndef add(a, b):\n    return a + b\n"
            "assert add(1, 2) == 3\n```\nThis test verifies the result."
        ),
    })
    low = evaluate_response({
        "task_type": "coding", "prompt": prompt, "response": "Add the values."
    })
    assert high["evaluation_score"] > low["evaluation_score"]


def test_incomplete_response_is_detected():
    result = evaluate_response({
        "task_type": "qa",
        "prompt": "What is a model gateway?",
        "response": "A gateway.",
    })
    assert "answer appears incomplete" in result["issues"]


def test_evaluation_is_deterministic():
    payload = {
        "task_type": "reasoning",
        "prompt": "Compare two approaches.",
        "response": (
            "First, approach A is cheaper because it is smaller. Second, "
            "approach B is stronger. Therefore the choice depends on the task."
        ),
    }
    assert evaluate_response(payload) == evaluate_response(payload)
    assert set(evaluate_response(payload)["metrics"]) == {
        "completeness", "correctness", "format"
    }
