import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import run_full_scheduler_benchmark as benchmark  # noqa: E402


CONFIG = ROOT / "config" / "full_scheduler_benchmark_v1.json"


def small_config():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config["dataset"].update({"request_count": 12, "seeds": [4101],
                              "repetitions_per_seed": 1})
    config["sensitivity"]["request_counts"] = [6]
    return config


def test_execute_variant_calls_real_scheduler_route_and_all_components(monkeypatch, tmp_path):
    calls = 0
    reservations = 0
    original = benchmark.Scheduler.route
    original_reserve = benchmark.ExplorationGovernanceService.reserve_recommendation

    def counted(self, payload):
        nonlocal calls
        calls += 1
        return original(self, payload)

    monkeypatch.setattr(benchmark.Scheduler, "route", counted)
    def reserved(self, **kwargs):
        nonlocal reservations
        reservations += 1
        return original_reserve(self, **kwargs)
    monkeypatch.setattr(benchmark.ExplorationGovernanceService,
                        "reserve_recommendation", reserved)
    rows, component_calls = benchmark.execute_variant(
        small_config(), tmp_path, strategy="confidence_aware_v2", variant_id="A0")
    assert calls == len(rows) == 12
    assert reservations == len(rows)
    assert all(component_calls[name] > 0 for name in small_config()["component_requirements"])
    assert not any(row["network_called"] for row in rows)
    assert all(row["authoritative_actual_channel"] is None for row in rows)


def test_full_benchmark_is_deterministic_and_paired(tmp_path):
    config = small_config()
    first = {}
    second = {}
    for strategy in config["dataset"]["strategies"]:
        first[strategy], _ = benchmark.execute_variant(
            config, tmp_path / "one" / strategy, strategy=strategy, variant_id="A0")
        second[strategy], _ = benchmark.execute_variant(
            config, tmp_path / "two" / strategy, strategy=strategy, variant_id="A0")
        assert first[strategy] == second[strategy]
    assert {row["pair_id"] for row in first["confidence_aware_v2"]} == {
        row["pair_id"] for row in first["fastest_first"]}


def test_results_contain_metrics_statistics_sensitivity_and_provenance(tmp_path):
    config = small_config()
    local = tmp_path / "config.json"
    local.write_text(json.dumps(config), encoding="utf-8")
    result = benchmark.run(local.resolve(), tmp_path / "out", "BENCH-TEST")
    assert result["scheduler_route_executed"] is True
    assert {row["strategy"] for row in result["summaries"]} == set(config["dataset"]["strategies"])
    assert {row["metric"] for row in result["paired_comparisons"]} == set(config["statistics"]["metrics"])
    assert all("p_value_bh" in row and "effect_size_dz" in row for row in result["paired_comparisons"])
    assert (tmp_path / "out" / "sensitivity_results.json").exists()
    manifest = json.loads((tmp_path / "out" / "provenance_manifest.json").read_text())
    assert manifest["network_called"] is False and manifest["real_execution"] is False


def test_run_is_replayable_in_the_same_output_directory(tmp_path):
    config = small_config()
    local = tmp_path / "config.json"
    local.write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "repeatable"
    first = benchmark.run(local.resolve(), output, "BENCH-REPLAY")
    second = benchmark.run(local.resolve(), output, "BENCH-REPLAY")
    assert first["baseline_observation_sha256"] == second["baseline_observation_sha256"]
    assert first["summaries"] == second["summaries"]
    assert not list(output.glob(".benchmark-state-*"))
    assert not list(output.glob(".sensitivity-state-*"))


def test_unsafe_boundary_is_rejected(tmp_path):
    config = small_config(); config["execution_boundary"]["network_allowed"] = True
    path = tmp_path / "unsafe.json"; path.write_text(json.dumps(config))
    try:
        benchmark.run(path, tmp_path / "out", "UNSAFE")
    except ValueError as exc:
        assert "offline" in str(exc)
    else:
        raise AssertionError("unsafe benchmark boundary accepted")
