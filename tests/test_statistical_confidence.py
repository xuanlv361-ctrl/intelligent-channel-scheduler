from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from statistical_confidence import (  # noqa: E402
    ConfidencePolicyError,
    calculate_weighted_wilson,
    load_confidence_policy,
    wilson_interval,
)

NOW = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("successes", "total", "expected"),
    [
        (0, 1, (0.0, 0.7934506856)),
        (1, 1, (0.2065493144, 1.0)),
        (0, 5, (0.0, 0.4344824648)),
        (5, 5, (0.5655175352, 1.0)),
        (0, 20, (0.0, 0.1611251581)),
        (20, 20, (0.8388748419, 1.0)),
        (50, 100, (0.4038315304, 0.5961684696)),
    ],
)
def test_reference_wilson_intervals(successes, total, expected):
    actual = wilson_interval(successes / total, total, 0.95)
    assert actual == pytest.approx(expected, abs=1e-10)


def event(identifier, success=True, *, age=0, reliability=1.0, missing=()):
    return {
        "evidence_id": identifier,
        "observed_at": (NOW - timedelta(seconds=age)).isoformat(),
        "success": success,
        "source_type": "measured_unified_uat",
        "source_reliability": reliability,
        "missing_fields": list(missing),
    }


def calculate(events, window="1h", policy=None):
    return calculate_weighted_wilson(
        events,
        window_name=window,
        window_end=NOW,
        metric_snapshot_id="MS-TEST",
        policy=policy,
    )


def test_zero_samples_and_all_zero_weights_are_insufficient():
    assert calculate([])["confidence_state"] == "insufficient"
    zero = calculate([event("zero", reliability=0.0)])
    assert zero["effective_sample_size"] == 0
    assert zero["interval_lower"] is None


def test_freshness_half_life_and_missingness_reduce_absolute_evidence_mass():
    fresh = calculate([event("fresh")])
    half = calculate([event("half", age=3600)])
    missing = calculate([event("missing", missing=("latency_ms",))])
    assert fresh["freshness_factor"] == 1
    assert half["freshness_factor"] == pytest.approx(0.5)
    assert half["effective_sample_size"] == pytest.approx(0.5)
    assert missing["completeness_factor"] == pytest.approx(7 / 8)
    assert missing["effective_sample_size"] == pytest.approx(7 / 8)


def test_heterogeneous_weight_reference_uses_absolute_mass_cap():
    result = calculate(
        [event("success", True, reliability=1.0), event("failure", False, reliability=0.5)]
    )
    assert result["weight_sum"] == pytest.approx(1.5)
    assert result["weight_square_sum"] == pytest.approx(1.25)
    assert result["kish_effective_sample_size"] == pytest.approx(1.8)
    assert result["effective_sample_size"] == pytest.approx(1.5)
    assert result["weighted_success_rate"] == pytest.approx(2 / 3)
    assert (result["interval_lower"], result["interval_upper"]) == pytest.approx(
        (0.1294496105, 0.9641577759), abs=1e-10
    )


def test_outcome_denominator_and_effective_sample_are_conservative():
    result = calculate([event("known"), event("unknown", success=None)])
    assert result["valid_outcome_count"] == 1
    assert result["raw_success_rate"] == 1
    assert result["outcome_coverage_factor"] == 0.5
    assert result["effective_sample_size"] <= result["valid_outcome_count"]


def test_unknown_source_blocks_and_cannot_improve_raw_score():
    row = event("unknown")
    row.pop("source_reliability")
    row["source_type"] = "unregistered"
    result = calculate([row])
    assert result["confidence_state"] == "blocked"
    assert result["unknown_source_types"] == ["unregistered"]
    assert result["adjusted_success_score"] is None


def test_input_cannot_raise_governed_source_reliability():
    row = event("fixture", reliability=1.0)
    row["source_type"] = "integration_test_fixture"
    result = calculate([row])
    assert result["source_reliability_factor"] == 0.5
    assert result["effective_sample_size"] == 0.5


def test_policy_content_hash_is_bound_to_result_identity():
    baseline_policy = load_confidence_policy()
    baseline = calculate([event("one")], policy=baseline_policy)
    changed_policy = {**baseline_policy, "minimum_effective_sample_size": 6}
    changed_policy.pop("_policy_sha256")
    canonical_path_policy = {
        key: value for key, value in changed_policy.items()
    }
    import hashlib
    import json
    canonical_path_policy["_policy_sha256"] = hashlib.sha256(
        json.dumps(
            canonical_path_policy,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    changed = calculate([event("one")], policy=canonical_path_policy)
    assert baseline["policy_sha256"] != changed["policy_sha256"]
    assert baseline["input_fingerprint"] != changed["input_fingerprint"]


def test_input_order_determinism_and_interval_monotonicity():
    small = [event(str(index)) for index in range(5)]
    large = [event(str(index)) for index in range(20)]
    forward = calculate(small)
    reverse = calculate(list(reversed(small)))
    assert forward["input_fingerprint"] == reverse["input_fingerprint"]
    assert forward["interval_width"] == reverse["interval_width"]
    assert calculate(large)["interval_width"] < forward["interval_width"]


def test_invalid_confidence_level_rejected():
    with pytest.raises(ConfidencePolicyError, match="invalid_confidence_level"):
        wilson_interval(0.5, 10, 1.0)
    policy = load_confidence_policy()
    policy["confidence_level"] = 0
    with pytest.raises(ConfidencePolicyError, match="invalid_confidence_level"):
        wilson_interval(0.5, 10, policy["confidence_level"])


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"required_fields": []}, "invalid_required_fields"),
        (
            {"freshness_half_life_seconds_by_window": {"5m": 0, "1h": 1, "24h": 1}},
            "invalid_freshness_half_lives",
        ),
        ({"source_reliability": {"measured_uat": 1.1}}, "invalid_source_reliability"),
        (
            {"confidence_width_thresholds": {"high": 0.3, "medium": 0.2}},
            "invalid_confidence_width_thresholds",
        ),
        ({"unknown_source_action": "allow"}, "invalid_unknown_source_action"),
    ],
)
def test_policy_validation_fails_closed(tmp_path, patch, message):
    import json

    policy = load_confidence_policy()
    policy.pop("_policy_sha256")
    policy.update(patch)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ConfidencePolicyError, match=message):
        load_confidence_policy(path)


def test_all_success_and_all_failure_boundaries_are_finite():
    success = calculate([event(str(index), True) for index in range(5)])
    failure = calculate([event(str(index), False) for index in range(5)])
    assert success["boundary_case"] == "all_success"
    assert failure["boundary_case"] == "all_failure"
    assert success["interval_lower"] > failure["interval_lower"]
    assert all(
        math.isfinite(value)
        for value in (
            success["interval_lower"],
            success["interval_upper"],
            failure["interval_lower"],
            failure["interval_upper"],
        )
    )
