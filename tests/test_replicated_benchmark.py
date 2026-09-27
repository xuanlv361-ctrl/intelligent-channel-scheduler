import csv
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rows(name):
    with (ROOT / "output" / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_replicated_counts_seeds_and_common_random_numbers():
    payload = json.loads((ROOT / "output" / "replicated_strategy_results_v4.json").read_text(encoding="utf-8"))
    assert len(payload["simulation_seeds"]) == len(set(payload["simulation_seeds"])) == 20
    assert payload["requests_per_seed"] == 5000 and payload["total_requests"] == 100000
    assert payload["total_strategy_decisions"] == 700000
    assert payload["common_random_numbers_within_seed"] is True
    assert all(a["request_count"] == 5000 and a["potential_outcome_count"] == 40000 and a["decision_count"] == 35000 for a in payload["seed_audit"])


def test_replicated_intervals_and_mock_markers_are_valid():
    for row in rows("replicated_strategy_summary_v4.csv") + rows("replicated_strategy_by_regime_v4.csv") + rows("paired_strategy_comparison_v4_intermediate.csv"):
        assert math.isfinite(float(row["ci95_lower"])) and math.isfinite(float(row["ci95_upper"]))
        assert float(row["ci95_lower"]) <= float(row["mean"]) <= float(row["ci95_upper"])
        assert row["is_mock"] == row["is_mock_evaluation"] == "TRUE"


def test_fixed_replicated_outputs_have_stable_seed_audit():
    payload = json.loads((ROOT / "output" / "replicated_strategy_results_v4.json").read_text(encoding="utf-8"))
    assert len({(a["simulation_seed"], a["shared_potential_outcomes_sha256"]) for a in payload["seed_audit"]}) == 20
    assert all(len(a["shared_potential_outcomes_sha256"]) == 64 for a in payload["seed_audit"])
