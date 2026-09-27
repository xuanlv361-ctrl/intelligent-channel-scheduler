import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import strategy_engine  # noqa: E402
from strategies.health_aware_strategy import (  # noqa: E402
    HealthAwareStrategyError,
    calculate_health_aware_score,
    rank_candidates,
)


POLICY = strategy_engine.load_json(ROOT / "config" / "health_aware_policy_v1.json")


def candidate(channel, health, priority=10):
    return {
        "channel_id": channel, "candidate_id": f"C-{channel}",
        "health_score": health, "reliability": 0.02,
        "latency_score": 0.2, "cost_score": 0.2, "priority": priority,
    }


def test_score_formula_matches_configured_manual_value():
    row = candidate("19", 0.92)
    expected = 0.35 * 0.92 - 0.25 * 0.02 - 0.20 * 0.2 - 0.20 * 0.2
    assert calculate_health_aware_score(row, POLICY) == pytest.approx(expected)


def test_degraded_channel_is_avoided():
    before = rank_candidates([candidate("19", 0.92), candidate("45", 0.88)], POLICY)
    after = rank_candidates([candidate("19", 0.55), candidate("45", 0.88)], POLICY)
    assert before[0]["channel_id"] == "19"
    assert after[0]["channel_id"] == "45"


def test_ranking_is_deterministic_and_ties_are_stable():
    rows = [candidate("45", 0.8), candidate("19", 0.8)]
    assert rank_candidates(rows, POLICY) == rank_candidates(rows, POLICY)
    assert rank_candidates(rows, POLICY)[0]["channel_id"] == "19"


def test_weights_validate_and_inputs_are_not_mutated():
    rows = [candidate("19", 0.9)]
    original = copy.deepcopy(rows)
    rank_candidates(rows, POLICY)
    assert rows == original
    invalid = copy.deepcopy(POLICY)
    invalid["weights"]["health"] = 0.5
    with pytest.raises(HealthAwareStrategyError, match="sum"):
        rank_candidates(rows, invalid)


def test_health_aware_remains_separate_from_versioned_v3_catalog():
    catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    assert catalog["catalog_version"] == "v3.0.0"
    assert "health_aware_v1" not in catalog["supported_strategies"]
    assert {"latency_first", "cost_first", "confidence_aware_v3"} <= set(
        catalog["supported_strategies"])
    assert catalog["strategies"]["confidence_aware_v2"]["weights"] == {
        "latency": 0.4, "cost": 0.3, "failure_risk": 0.3,
    }


def test_offline_comparison_output_has_two_5000_request_strategies():
    payload = json.loads(
        (ROOT / "output" / "health_aware_strategy_comparison_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["evaluation_type"] == "offline demonstration only"
    assert payload["real_api_calls_performed"] == 0
    assert {row["strategy"] for row in payload["summary"]} == {
        "confidence_aware_v2", "health_aware_v1",
    }
    assert {row["request_count"] for row in payload["summary"]} == {5000}
    assert all(
        {"success_rate", "average_latency_ms", "average_cost", "average_regret"}
        <= row.keys()
        for row in payload["summary"]
    )
