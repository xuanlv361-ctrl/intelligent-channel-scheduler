import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from feedback_store import query_feedback  # noqa: E402
from model_feedback_analyzer import analyze_model_performance, build_analysis  # noqa: E402


def test_analysis_groups_by_model_and_task():
    rows = query_feedback()
    results = analyze_model_performance(rows)
    keys = {(row["model_id"], row["task_type"]) for row in results}
    assert ("gpt-4o-4-1", "coding") in keys
    assert ("qwen", "writing") in keys
    assert all(
        {"sample_count", "success_rate", "average_rating", "average_latency", "recommendation"}
        <= row.keys() for row in results
    )


def test_empty_feedback_handling():
    assert analyze_model_performance([]) == []


def test_insufficient_sample_warning():
    rows = query_feedback(model_id="qwen", task_type="writing")
    result = analyze_model_performance(rows, minimum_sample_count=5)
    assert result[0]["sample_count"] == 2
    assert result[0]["recommendation"] == "insufficient_sample_warning"


def test_analysis_never_updates_weights():
    output = build_analysis()
    assert output["automatic_model_weight_update"] is False
    assert output["real_api_calls_performed"] == 0
    assert output["analysis_type"] == "offline_feedback_analysis_only"
