import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import model_capability_router as router  # noqa: E402
from task_classifier import build_task_profile  # noqa: E402


def test_catalog_contains_five_models_and_valid_scores():
    catalog = router.load_catalog()
    assert len(catalog["models"]) == 5
    assert {row["model_id"] for row in catalog["models"]} == {
        "claude-sonnet", "deepseek-reasoner", "gpt-4o-4-1",
        "gemini-flash", "qwen",
    }
    assert catalog["is_benchmark_data"] is False
    assert all(
        0 <= score <= 1
        for model in catalog["models"]
        for score in model["capabilities"].values()
    )


def test_dot_product_manual_score():
    model = next(
        row for row in router.load_catalog()["models"]
        if row["model_id"] == "claude-sonnet"
    )
    score, explanation = router.compatibility_score(
        {"reasoning": 0.8, "coding": 0.2}, model
    )
    assert score == pytest.approx(0.8 * 0.92 + 0.2 * 0.90)
    assert sum(row["contribution"] for row in explanation) == pytest.approx(score)


def test_ranking_is_deterministic_and_explained():
    profile = build_task_profile("Write Python code and solve a math equation")
    first = router.route_model(profile)
    assert first == router.route_model(profile)
    assert first["ranking"][0]["score"] >= first["ranking"][-1]["score"]
    assert first["reason"].startswith("high compatibility with")
    assert first["source_type"] == "offline_capability_routing"


def test_chinese_writing_prefers_qwen_under_configured_assumptions():
    result = router.route_model(build_task_profile("请写一篇中文文章"))
    assert result["selected_model"] == "qwen"


def test_invalid_score_and_requirement_rejected():
    catalog = router.load_catalog()
    invalid = copy.deepcopy(catalog)
    invalid["models"][0]["capabilities"]["coding_score"] = 1.1
    with pytest.raises(router.ModelCapabilityError, match="\\[0,1\\]"):
        router.validate_catalog(invalid)
    with pytest.raises(router.ModelCapabilityError, match="requirements"):
        router.route_model({"task_type": "coding", "requirements": {}})


def test_protected_core_hashes_unchanged():
    assert hashlib.sha256((ROOT / "src" / "strategy_engine.py").read_bytes()).hexdigest().upper() == \
        "1E6D5A1BC9B47D38C4E334FD1C7D698DFE49DC2C180D446079CF753430152AA0"
    assert hashlib.sha256((ROOT / "src" / "channel_health.py").read_bytes()).hexdigest().upper() == \
        "6093F5D2934002CB10F0DDC4F49C2CA8004FC900B30440FA2D8278EEA0AEC169"
