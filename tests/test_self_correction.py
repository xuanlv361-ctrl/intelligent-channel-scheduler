import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from self_correction import apply_correction


def test_missing_content_and_format_are_repaired():
    result = apply_correction({
        "original_result": {"output": "Short answer."},
        "correction_plan": ["missing_content", "format_error"],
    })
    assert result["status"] == "corrected"
    assert "## Required Information" in result["corrected_result"]["output"]
    assert result["corrected_result"]["output"].startswith("# Corrected Response")
    assert [row["action"] for row in result["changes"]] == [
        "missing_content", "format_error"
    ]


def test_coding_error_generates_corrected_mock_code():
    result = apply_correction({
        "original_result": "broken code",
        "correction_plan": ["coding_error"],
    })
    assert "```python" in result["corrected_result"]
    assert "assert solution(3) == 3" in result["corrected_result"]
    assert result["real_model_calls_performed"] == 0


def test_incomplete_answer_is_expanded_deterministically():
    payload = {
        "original_result": "Initial answer.",
        "correction_plan": ["incomplete_answer"],
    }
    assert apply_correction(payload) == apply_correction(payload)
    assert "## Expanded Answer" in apply_correction(payload)["corrected_result"]
