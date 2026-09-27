import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import channel_health as health  # noqa: E402
from metrics_store import MetricsStore  # noqa: E402


def policy():
    return health.load_json(health.DEFAULT_POLICY_PATH)


def thresholds():
    return health.load_json(health.DEFAULT_THRESHOLDS_PATH)


def metric(sample=100, success=0.99, latency=700, cost=0.002):
    return {
        "channel_id": "test",
        "metrics": {
            "success_rate": success,
            "avg_latency_ms": latency,
            "p95_latency_ms": latency + 100,
            "avg_cost": cost,
        },
        "sample_count": sample,
    }


def test_same_input_is_deterministic_and_score_bounded():
    first = health.calculate_health_score(metric(), policy())
    second = health.calculate_health_score(metric(), policy())
    assert first == second
    assert 0 <= first["health_score"] <= 1
    assert all(0 <= value <= 1 for value in first.values())


def test_weight_sum_validation():
    invalid = copy.deepcopy(policy())
    invalid["weights"]["confidence"] = 0.2
    with pytest.raises(health.HealthPolicyError, match="sum to 1"):
        health.calculate_health_score(metric(), invalid)


def test_confidence_monotonic_bounded_and_deterministic():
    values = [health.calculate_confidence(n, 100) for n in (0, 1, 10, 100, 1000)]
    assert values == sorted(values)
    assert values == [0, 0.01, 0.1, 1, 1]
    assert health.calculate_confidence(10, 100) == health.calculate_confidence(10, 100)


def test_small_perfect_sample_cannot_dominate_large_near_perfect_sample():
    small = health.calculate_health_score(metric(sample=1, success=1.0), policy())
    large = health.calculate_health_score(metric(sample=100, success=0.99), policy())
    assert large["health_score"] > small["health_score"]
    assert large["confidence"] > small["confidence"]


@pytest.mark.parametrize(
    "score,expected",
    [(0.85, "healthy"), (0.849, "warning"), (0.60, "warning"), (0.599, "degraded")],
)
def test_status_classification(score, expected):
    assert health.classify_status(score, thresholds()) == expected


def test_snapshot_schema_and_offline_label():
    snapshot = health.build_health_snapshot(MetricsStore.load(), policy(), thresholds())
    assert snapshot["health_version"] == "v1.0.0"
    assert snapshot["estimation_type"] == health.ESTIMATION_TYPE
    assert len(snapshot["channels"]) == 3
    assert all(
        {"channel_id", "health_score", "confidence", "status", "reason"} <= row.keys()
        for row in snapshot["channels"]
    )


def test_dry_run_does_not_write(tmp_path):
    target = tmp_path / "health.json"
    result = subprocess.run(
        [
            sys.executable, str(ROOT / "src" / "channel_health.py"),
            "--dry-run", "--output", str(target),
        ],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert not target.exists()


def test_repeated_output_is_byte_stable(tmp_path):
    target = tmp_path / "health.json"
    command = [
        sys.executable, str(ROOT / "src" / "channel_health.py"),
        "--output", str(target),
    ]
    subprocess.run(command, check=True, capture_output=True)
    first = target.read_bytes()
    subprocess.run(command, check=True, capture_output=True)
    assert first == target.read_bytes()
    assert json.loads(first)["estimation_type"] == health.ESTIMATION_TYPE
