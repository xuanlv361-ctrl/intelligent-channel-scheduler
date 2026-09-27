import json
import math
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import generate_simulation_workload as workload  # noqa: E402
import run_weight_sensitivity as sensitivity  # noqa: E402


_FULL = None


def full_sensitivity():
    global _FULL
    if _FULL is None:
        _FULL = sensitivity.evaluate_weights()
    return _FULL


def small_inputs():
    requests, outcomes = workload.generate()
    selected = requests[:10]; ids = {r["request_id"] for r in selected}
    requests = [{k: str(v) for k, v in r.items()} for r in selected]
    outcomes = [{k: str(v) for k, v in r.items()} for r in outcomes if r["request_id"] in ids]
    return requests, outcomes


def test_weight_grid_has_66_unique_sets():
    grid = sensitivity.weight_grid()
    assert len(grid) == len({r["weight_set_id"] for r in grid}) == 66


def test_weight_sums_are_exact_within_tolerance():
    assert all(abs(r["latency"] + r["cost"] + r["failure_risk"] - 1.0) < 1e-9 for r in sensitivity.weight_grid())


def test_grid_contains_experimental_default_weights():
    assert any((r["latency"], r["cost"], r["failure_risk"]) == (0.4, 0.3, 0.3) for r in sensitivity.weight_grid())


def test_small_sensitivity_shape_and_markers():
    requests, outcomes = small_inputs()
    weights = sensitivity.weight_grid()[:2]
    overall, by_regime = sensitivity.evaluate_weights(requests, outcomes, weights)
    assert len(overall) == 2 and len(by_regime) == 12
    assert all(r["is_mock"] == r["is_mock_evaluation"] == "TRUE" and r["data_source"] == "offline_simulation_v3" for r in overall + by_regime)


def test_no_nan_or_infinity_in_small_run():
    requests, outcomes = small_inputs()
    overall, by_regime = sensitivity.evaluate_weights(requests, outcomes, sensitivity.weight_grid()[:2])
    numeric = ["success_rate", "average_latency_ms", "p95_latency_ms", "average_cost", "sla_violation_rate", "composite_regret", "robustness_score"]
    assert all(math.isfinite(float(r[k])) for r in overall + by_regime for k in numeric)


def test_full_outputs_have_66_and_330_rows():
    overall, by_regime = full_sensitivity()
    assert len(overall) == 66 and len(by_regime) == 396


def test_ranks_complete_per_regime():
    _, rows = full_sensitivity()
    for regime in {r["environment_regime"] for r in rows}:
        subset = [r for r in rows if r["environment_regime"] == regime]
        assert all(1 <= r["rank_min"] <= r["rank_max"] <= 66 for r in subset)
        assert all(r["tied_weight_count"] == r["rank_max"] - r["rank_min"] + 1 for r in subset)


def test_signature_ties_and_pareto_are_stable():
    first = full_sensitivity()
    second = sensitivity.evaluate_weights()
    assert [r["selection_signature"] for r in first[0]] == [r["selection_signature"] for r in second[0]]
    assert 1 <= len({r["selection_signature"] for r in first[0]}) <= 66
    assert any(r["pareto_optimal"] == "TRUE" for r in first[0])


def test_output_structure(tmp_path, monkeypatch):
    requests, outcomes = small_inputs()
    original = sensitivity.evaluate_weights
    monkeypatch.setattr(sensitivity, "evaluate_weights", lambda: original(requests, outcomes, sensitivity.weight_grid()[:2]))
    overall, rows = sensitivity.build(tmp_path)
    payload = json.loads((tmp_path / sensitivity.JSON_NAME).read_text(encoding="utf-8"))
    assert payload["weight_set_count"] == 2 and payload["is_mock_evaluation"] is True
    assert (tmp_path / sensitivity.SUMMARY_NAME).exists() and (tmp_path / sensitivity.BY_REGIME_NAME).exists()
