import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import channel_health  # noqa: E402
import generate_health_simulation_dataset as generator  # noqa: E402


PROTECTED = [
    ROOT / "src" / "strategy_engine.py",
    ROOT / "config" / "strategy_catalog_v2.json",
    ROOT / "data" / "simulation_requests_v3.csv",
    ROOT / "data" / "simulation_potential_outcomes_v3.csv",
    ROOT / "output" / "strategy_benchmark_summary_v3.csv",
    ROOT / "output" / "weight_sensitivity_summary_v2.csv",
    ROOT / "data" / "metrics_snapshot_v1.csv",
    ROOT / "data" / "real_channel_measurements_v1.csv",
]


def generated():
    return generator.generate()


def simulation_policy():
    config = generator.load_json(generator.DEFAULT_CONFIG_PATH)
    return generator.simulation_policy(
        channel_health.load_json(channel_health.DEFAULT_POLICY_PATH), config
    )


def by_id(rows):
    return {row["channel_id"]: row for row in rows}


def test_large_sample_confidence_is_one():
    assert channel_health.calculate_confidence(5000, 5000) == 1.0
    _, snapshot = generated()
    large = by_id(snapshot["channels"])["SIM-HEALTHY-001"]
    assert large["confidence"] == 1.0


def test_small_sample_confidence_is_low_and_not_highly_reliable():
    _, snapshot = generated()
    small = by_id(snapshot["channels"])["SIM-SMALL-001"]
    assert small["confidence"] < 0.1
    assert small["status"] == "warning"
    assert all("high configured reliability" not in reason for reason in small["reason"])
    assert any("uncertain" in reason for reason in small["reason"])


def test_confidence_strictly_increases_for_10_100_1000_samples():
    threshold = simulation_policy()["confidence"]["sample_threshold"]
    values = [
        channel_health.calculate_confidence(sample, threshold)
        for sample in (10, 100, 1000)
    ]
    assert values[0] < values[1] < values[2]


def test_small_optimistic_channel_cannot_outscore_comparable_large_channel():
    policy = simulation_policy()
    base = {
        "metrics": {
            "success_rate": 1.0, "avg_latency_ms": 500,
            "p95_latency_ms": 800, "avg_cost": 0.001,
        }
    }
    small = channel_health.calculate_health_score(
        {**base, "sample_count": 5}, policy
    )
    large = channel_health.calculate_health_score(
        {**base, "sample_count": 5000}, policy
    )
    assert small["confidence"] < large["confidence"]
    assert small["health_score"] < large["health_score"]


def test_generated_dataset_has_eight_configured_conditions():
    metrics, snapshot = generated()
    assert metrics["source_type"] == "offline_simulation"
    assert metrics["validation_type"] == generator.VALIDATION_TYPE
    assert metrics["metrics_version"] == "simulation-v1.0.0"
    assert len(metrics["channels"]) == 8
    assert len({row["channel_id"] for row in metrics["channels"]}) == 8
    assert snapshot["source_type"] == "offline_simulation"
    assert snapshot["validation_type"] == generator.VALIDATION_TYPE
    assert snapshot["all_statuses_match_expected"] is True
    assert {row["status"] for row in snapshot["channels"]} == {
        "healthy", "warning", "degraded"
    }


def test_same_input_produces_identical_metrics_scores_and_statuses():
    first = generated()
    second = generated()
    assert first == second
    assert [
        (row["health_score"], row["status"], row["confidence"])
        for row in first[1]["channels"]
    ] == [
        (row["health_score"], row["status"], row["confidence"])
        for row in second[1]["channels"]
    ]


def test_repeated_files_are_byte_identical_and_dry_run_writes_nothing(tmp_path):
    metrics_path = tmp_path / "metrics.json"
    health_path = tmp_path / "health.json"
    command = [
        sys.executable, str(ROOT / "src" / "generate_health_simulation_dataset.py"),
        "--metrics-output", str(metrics_path), "--health-output", str(health_path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    first = (metrics_path.read_bytes(), health_path.read_bytes())
    subprocess.run(command, check=True, capture_output=True)
    assert first == (metrics_path.read_bytes(), health_path.read_bytes())
    dry_metrics = tmp_path / "dry-metrics.json"
    dry_health = tmp_path / "dry-health.json"
    subprocess.run(
        command[:2] + [
            "--dry-run", "--metrics-output", str(dry_metrics),
            "--health-output", str(dry_health),
        ],
        check=True, capture_output=True,
    )
    assert not dry_metrics.exists() and not dry_health.exists()


def test_generator_has_no_network_imports():
    tree = ast.parse(
        (ROOT / "src" / "generate_health_simulation_dataset.py").read_text(
            encoding="utf-8"
        )
    )
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"requests", "httpx", "urllib", "aiohttp"})


def test_protected_files_remain_unchanged(tmp_path):
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in PROTECTED}
    subprocess.run(
        [
            sys.executable, str(ROOT / "src" / "generate_health_simulation_dataset.py"),
            "--metrics-output", str(tmp_path / "metrics.json"),
            "--health-output", str(tmp_path / "health.json"),
        ],
        check=True, capture_output=True,
    )
    after = {path: hashlib.sha256(path.read_bytes()).digest() for path in PROTECTED}
    assert before == after
