import ast
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import channel_health  # noqa: E402
from channel_observation import ChannelObservation  # noqa: E402
import health_updater  # noqa: E402


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


def scenario():
    return health_updater.load_json(health_updater.DEFAULT_SCENARIO)


def test_failure_burst_decreases_health_and_changes_status():
    snapshot = health_updater.run_simulation(scenario())
    channel = snapshot["channels"][0]
    assert channel["before"]["status"] == "healthy"
    assert channel["after"]["status"] == "warning"
    assert channel["after"]["health_score"] < channel["before"]["health_score"]
    assert "failure burst detected" in channel["reason"]
    assert "recent latency increase" in channel["reason"]


def test_recovery_observations_improve_health():
    channel = health_updater.run_simulation(scenario())["channels"][0]
    assert channel["recovered"]["health_score"] > channel["after"]["health_score"]
    assert channel["recovered"]["status"] == "healthy"
    assert channel["transition_matches_expected"] is True


def test_low_sample_confidence_remains_protected():
    updater = health_updater.build_updater()
    result = updater.update(ChannelObservation.from_dict({
        "channel_id": "LOW-SAMPLE",
        "timestamp": "2026-07-24T10:00:00+08:00",
        "success": True,
        "latency_ms": 500,
        "cost": 0.001,
        "error_type": None,
    }))
    assert result["confidence"] == 0.01
    assert result["status"] != "healthy"


def test_dynamic_output_is_deterministic():
    first = health_updater.run_simulation(scenario())
    second = health_updater.run_simulation(scenario())
    assert first == second
    assert first["validation_type"] == health_updater.VALIDATION_TYPE
    assert first["real_api_calls_performed"] == 0


def test_dry_run_and_repeated_output(tmp_path):
    target = tmp_path / "dynamic.json"
    command = [
        sys.executable, str(ROOT / "src" / "health_updater.py"),
        "--output", str(target),
    ]
    subprocess.run(command, check=True, capture_output=True)
    first = target.read_bytes()
    subprocess.run(command, check=True, capture_output=True)
    assert first == target.read_bytes()
    dry = tmp_path / "dry.json"
    subprocess.run(
        [
            sys.executable, str(ROOT / "src" / "health_updater.py"),
            "--dry-run", "--output", str(dry),
        ],
        check=True, capture_output=True,
    )
    assert not dry.exists()


def test_no_network_imports_and_protected_files_unchanged(tmp_path):
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in PROTECTED}
    imported = set()
    for name in (
        "channel_observation.py", "health_window.py", "time_decay.py",
        "health_updater.py",
    ):
        tree = ast.parse((ROOT / "src" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
    assert not (imported & {"requests", "httpx", "urllib", "aiohttp"})
    subprocess.run(
        [
            sys.executable, str(ROOT / "src" / "health_updater.py"),
            "--output", str(tmp_path / "snapshot.json"),
        ],
        check=True, capture_output=True,
    )
    after = {path: hashlib.sha256(path.read_bytes()).digest() for path in PROTECTED}
    assert before == after
