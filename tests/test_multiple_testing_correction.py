import csv
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import apply_multiple_testing_correction as correction  # noqa: E402


def test_bh_known_three_value_example():
    assert correction.benjamini_hochberg([0.01, 0.02, 0.03]) == pytest.approx([0.03, 0.03, 0.03])


def test_bh_is_monotone_in_sorted_order_and_bounded():
    raw = [0.8, 0.001, 0.07, 1.0, 0.0, 0.2]
    adjusted = correction.benjamini_hochberg(raw)
    assert all(0 <= value <= 1 for value in adjusted)
    ordered = sorted(zip(raw, adjusted))
    assert [value for _, value in ordered] == sorted(value for _, value in ordered)


def test_bh_rejects_invalid_p_values():
    with pytest.raises(ValueError):
        correction.benjamini_hochberg([-0.01, 0.5])
    with pytest.raises(ValueError):
        correction.benjamini_hochberg([0.5, 1.01])


def test_output_structure_sorting_and_one_family_scope():
    payload = json.loads((ROOT / "output" / correction.JSON_NAME).read_text(encoding="utf-8"))
    rows = payload["results"]
    required = {"strategy_a", "strategy_b", "metric", "mean_difference", "raw_p_value",
                "adjusted_p_value", "significant_before_fdr", "significant_after_fdr",
                "fdr_method", "fdr_alpha", "sample_count", "bootstrap_count"}
    assert payload["test_family_size"] == len(rows) == 84
    assert payload["correction_scope"] == "all_84_pairwise_strategy_metric_tests_together"
    assert required <= set(rows[0])
    assert [r["raw_p_value"] for r in rows] == sorted(r["raw_p_value"] for r in rows)
    assert all(0 <= r["adjusted_p_value"] <= 1 for r in rows)
    assert all(r["fdr_method"] == "Benjamini-Hochberg" and r["fdr_alpha"] == 0.05 for r in rows)


def test_repeated_run_is_identical_and_input_is_not_modified(tmp_path):
    input_path = ROOT / "output" / "pairwise_strategy_comparison_v4.csv"
    before = hashlib.sha256(input_path.read_bytes()).hexdigest()
    first = correction.build(tmp_path / "first", input_path)
    second = correction.build(tmp_path / "second", input_path)
    assert first == second
    assert (tmp_path / "first" / correction.CSV_NAME).read_bytes() == (tmp_path / "second" / correction.CSV_NAME).read_bytes()
    assert (tmp_path / "first" / correction.JSON_NAME).read_bytes() == (tmp_path / "second" / correction.JSON_NAME).read_bytes()
    assert hashlib.sha256(input_path.read_bytes()).hexdigest() == before


def test_csv_has_84_rows_and_matches_json():
    with (ROOT / "output" / correction.CSV_NAME).open(encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    payload = json.loads((ROOT / "output" / correction.JSON_NAME).read_text(encoding="utf-8"))
    assert len(csv_rows) == len(payload["results"]) == 84
