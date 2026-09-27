import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import cost_aware_model_router as router  # noqa: E402
from task_classifier import build_task_profile  # noqa: E402


def test_coding_quality_first_prefers_high_capability_model():
    profile = build_task_profile("Implement Python code and debug this function")
    result = router.select_model(profile, objective="quality_first")
    assert result["selected_model"] == "deepseek-reasoner"
    assert result["ranking"][0]["score_breakdown"]["capability_score"] >= 0.9


def test_simple_question_prefers_low_cost_low_latency_model():
    profile = build_task_profile("What is a transformer?")
    result = router.select_model(profile, objective="balanced")
    assert result["selected_model"] == "gemini-flash"
    assert result["ranking"][0]["score_breakdown"]["cost_score"] > 0.9


def test_cost_first_policy_changes_coding_ranking():
    profile = build_task_profile("Implement Python code and debug this function")
    quality = router.select_model(profile, objective="quality_first")
    cost = router.select_model(profile, objective="cost_first")
    assert quality["selected_model"] == "deepseek-reasoner"
    assert cost["selected_model"] != quality["selected_model"]
    assert cost["selected_model"] in {"gemini-flash", "qwen"}


def test_output_is_deterministic_and_has_breakdowns():
    profile = build_task_profile("Summarize this long document")
    first = router.select_model(profile)
    assert first == router.select_model(profile)
    assert {
        "capability_score", "cost_score", "latency_score",
        "weighted_capability", "weighted_cost", "weighted_latency",
    } <= first["score_breakdown"].keys()
    assert first["source_type"] == "offline_configured_model_selection"


def test_catalog_and_policy_validation():
    cost_catalog = router.load_json(router.DEFAULT_COST_CATALOG)
    assert len(cost_catalog["models"]) == 5
    assert cost_catalog["price_unit"] == "assumed_currency_per_1m_tokens"
    policy = router.load_json(router.DEFAULT_POLICY)
    router.validate_policy(policy)
    invalid = copy.deepcopy(policy)
    invalid["objectives"]["balanced"]["weights"]["cost"] = 0.4
    try:
        router.validate_policy(invalid)
        assert False, "invalid weight sum should fail"
    except router.CostAwareModelError:
        pass


def test_demo_output_has_three_scenarios_and_no_api_calls():
    payload = router.build_demo()
    assert len(payload["scenarios"]) == 3
    assert payload["real_api_calls_performed"] == 0
    assert {row["scenario_id"] for row in payload["scenarios"]} == {
        "coding_request", "simple_question", "long_document_analysis"
    }


def test_protected_core_is_unchanged():
    assert hashlib.sha256((ROOT / "src" / "strategy_engine.py").read_bytes()).hexdigest().upper() == \
        "1E6D5A1BC9B47D38C4E334FD1C7D698DFE49DC2C180D446079CF753430152AA0"
