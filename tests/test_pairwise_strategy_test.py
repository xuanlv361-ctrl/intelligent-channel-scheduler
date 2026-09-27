import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import run_pairwise_strategy_test as pairwise  # noqa: E402


def test_bootstrap_is_reproducible_and_interval_valid():
    values = [-0.2, -0.1, 0.0, 0.1] * 5
    assert pairwise.paired_bootstrap(values, 1000, 7) == pairwise.paired_bootstrap(values, 1000, 7)
    mean, lower, upper, samples = pairwise.paired_bootstrap(values, 1000, 7)
    assert lower <= mean <= upper and len(samples) == 1000


def test_pairwise_output_has_21_pairs_four_metrics_and_10000_bootstraps():
    payload = json.loads((ROOT / "output" / pairwise.JSON_NAME).read_text(encoding="utf-8"))
    rows = payload["results"]
    assert payload["strategy_pair_count"] == 21 and len(rows) == 84
    assert len({(r["strategy_a"], r["strategy_b"]) for r in rows}) == 21
    assert {r["metric"] for r in rows} == {"composite_regret", "success_rate", "average_latency_ms", "average_cost"}
    assert all(r["bootstrap_iterations"] >= 10000 and r["request_pair_count"] == 100000 for r in rows)
    assert all(r["ci95_lower"] <= r["mean_difference"] <= r["ci95_upper"] for r in rows)
    assert all(0 <= r["raw_p_value"] <= 1 for r in rows)
    assert all(r["is_mock"] == r["is_mock_evaluation"] == "TRUE" and r["data_source"] == "offline_simulation_v3" for r in rows)


def test_csv_and_json_results_match():
    with (ROOT / "output" / pairwise.CSV_NAME).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 84
