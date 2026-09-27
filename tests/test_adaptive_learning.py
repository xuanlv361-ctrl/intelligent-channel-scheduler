import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from adaptive_learning import (
    analyze_history, compare_models, generate_recommendations,
)


def feedback(model, success, rating, latency, cost):
    return {
        "task_type": "coding", "selected_model": model, "success": success,
        "user_rating": rating, "latency_ms": latency, "cost": cost,
    }


def test_detect_better_model_with_sufficient_samples():
    rows = (
        [feedback("current-model", True, 3, 1200, 0.02) for _ in range(6)]
        + [feedback("better-model", True, 5, 700, 0.005) for _ in range(5)]
    )
    analysis = analyze_history(rows, [], [])
    comparison = compare_models(analysis)[0]
    assert comparison["current_preference"] == "current-model"
    assert comparison["best_model"] == "better-model"
    assert comparison["action"] == "increase_candidate_priority"


def test_insufficient_samples_have_low_confidence():
    analysis = analyze_history([
        feedback("model-a", True, 5, 500, 0.001),
        feedback("model-b", False, 2, 1000, 0.01),
    ], [], [])
    recommendation = generate_recommendations(analysis)[0]
    assert recommendation["confidence"] == "low"
    assert recommendation["action"] == "insufficient_samples"


def test_recommendations_are_deterministic_and_non_mutating():
    rows = [feedback("model-a", True, 4, 800, 0.002) for _ in range(5)]
    analysis = analyze_history(rows, [], [])
    first = generate_recommendations(analysis)
    assert first == generate_recommendations(analysis)
    assert all(row["automatic_weight_update"] is False for row in first)
    json.dumps(first)
