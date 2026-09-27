import json
import math
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import generate_simulation_workload as workload  # noqa: E402
import run_strategy_benchmark as benchmark  # noqa: E402


_FULL = None


def full_benchmark():
    global _FULL
    if _FULL is None:
        _FULL = benchmark.run_benchmark()
    return _FULL


def small_workload():
    requests, outcomes = workload.generate()
    selected = requests[:20]; ids = {r["request_id"] for r in selected}
    return [{k: str(v) for k, v in r.items()} for r in selected], [{k: str(v) for k, v in r.items()} for r in outcomes if r["request_id"] in ids]


def test_full_benchmark_counts_from_generated_files():
    decisions, summaries = full_benchmark()
    catalog = benchmark.strategy_engine.load_json(benchmark.strategy_engine.CATALOG_PATH)
    strategy_count = len(catalog["supported_strategies"])
    request_count = len(benchmark.load_or_generate()[0])
    regime_count = len({row["environment_regime"] for row in summaries})
    assert len(decisions) == request_count * strategy_count
    assert len(summaries) == strategy_count * regime_count


def test_each_strategy_has_5000_decisions():
    decisions, _ = full_benchmark()
    assert set(Counter(r["strategy_name"] for r in decisions).values()) == {5000}


def test_round_robin_really_rotates():
    requests, outcomes = small_workload()
    decisions, _ = benchmark.run_benchmark(requests, outcomes, ["round_robin"])
    assert len({r["selected_candidate"] for r in decisions[:3]}) == 3


def test_random_baseline_reproducible():
    requests, outcomes = small_workload()
    assert benchmark.run_benchmark(requests, outcomes, ["random_baseline"])[0] == benchmark.run_benchmark(requests, outcomes, ["random_baseline"])[0]


def test_decisions_have_complete_versions_and_mock_markers():
    requests, outcomes = small_workload()
    decisions, _ = benchmark.run_benchmark(requests, outcomes, ["fastest_first"])
    catalog = benchmark.strategy_engine.load_json(benchmark.strategy_engine.CATALOG_PATH)
    policy = benchmark.strategy_engine.load_json(benchmark.strategy_engine.POLICY_PATH)
    assert all(r["catalog_version"] == catalog["catalog_version"] and
               r["policy_version"] == policy["policy_version"] for r in decisions)
    assert all(r["is_mock"] == r["is_mock_evaluation"] == "TRUE" and r["data_source"] == "offline_simulation_v3" for r in decisions)


def test_summary_has_overall_and_five_regimes():
    _, summaries = full_benchmark()
    for strategy in {r["strategy_name"] for r in summaries}:
        assert {r["environment_regime"] for r in summaries if r["strategy_name"] == strategy} == {"overall", "normal", "peak_load", "price_spike", "channel_degradation", "cold_start", "candidate_failure"}


def test_no_nan_infinity_or_negative_cost():
    decisions, summaries = full_benchmark()
    assert all(not r["realized_cost"] or float(r["realized_cost"]) >= 0 for r in decisions)
    numeric = ["success_rate", "average_latency_ms", "p95_latency_ms", "average_cost", "cost_per_success", "sla_violation_rate", "selection_entropy", "latency_regret", "cost_regret", "composite_regret"]
    assert all(math.isfinite(float(r[k])) and float(r[k]) >= 0 for r in summaries for k in numeric)


def test_future_outcomes_not_passed_to_strategy(monkeypatch):
    requests, outcomes = small_workload()
    original = benchmark.strategy_engine.strategy_decision
    def guarded(**kwargs):
        assert all("realized_success" not in c and "realized_latency_ms" not in c for c in kwargs["candidates"])
        assert all("latent_success_probability" not in c for c in kwargs["candidates"])
        return original(**kwargs)
    monkeypatch.setattr(benchmark.strategy_engine, "strategy_decision", guarded)
    benchmark.run_benchmark(requests, outcomes, ["fastest_first"])


def test_outputs_have_expected_structures(tmp_path, monkeypatch):
    requests, outcomes = small_workload()
    monkeypatch.setattr(benchmark, "load_or_generate", lambda: (requests, outcomes))
    monkeypatch.setattr(benchmark.strategy_engine, "load_json", benchmark.strategy_engine.load_json)
    decisions, summaries = benchmark.build(tmp_path)
    assert (tmp_path / benchmark.DECISIONS_NAME).exists() and (tmp_path / benchmark.SUMMARY_NAME).exists()
    payload = json.loads((tmp_path / benchmark.JSON_NAME).read_text(encoding="utf-8"))
    assert payload["decision_count"] == len(decisions) and payload["is_mock_evaluation"] is True
