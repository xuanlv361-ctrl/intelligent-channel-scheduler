"""Apply one-family Benjamini-Hochberg FDR correction to 84 Mock tests."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "output" / "pairwise_strategy_comparison_v4.csv"
CSV_NAME = "pairwise_strategy_comparison_fdr_v4.csv"
JSON_NAME = "pairwise_strategy_comparison_fdr_v4.json"
FDR_ALPHA = 0.05
FDR_METHOD = "Benjamini-Hochberg"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def benjamini_hochberg(p_values):
    """Return BH adjusted p-values in original input order."""
    if not p_values:
        return []
    values = [float(value) for value in p_values]
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("p-values must be within [0, 1]")
    count = len(values)
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    adjusted_sorted = [0.0] * count
    running = 1.0
    for position in range(count - 1, -1, -1):
        _, p_value = ordered[position]
        rank = position + 1
        running = min(running, p_value * count / rank)
        adjusted_sorted[position] = min(1.0, max(0.0, running))
    adjusted = [0.0] * count
    for position, (original_index, _) in enumerate(ordered):
        adjusted[original_index] = adjusted_sorted[position]
    return adjusted


def load_rows(input_path=INPUT_PATH):
    with Path(input_path).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"strategy_a", "strategy_b", "metric", "mean_difference", "raw_p_value",
                "request_pair_count", "bootstrap_iterations"}
    if not rows or not required <= set(rows[0]):
        raise ValueError(f"input must contain fields: {sorted(required)}")
    if len(rows) != 84:
        raise ValueError(f"expected one family of 84 tests, got {len(rows)}")
    return rows


def correct(rows, alpha=FDR_ALPHA):
    adjusted = benjamini_hochberg([row["raw_p_value"] for row in rows])
    output = []
    for row, adjusted_p in zip(rows, adjusted):
        raw_p = float(row["raw_p_value"])
        output.append({
            "strategy_a": row["strategy_a"], "strategy_b": row["strategy_b"], "metric": row["metric"],
            "mean_difference": float(row["mean_difference"]), "raw_p_value": raw_p,
            "adjusted_p_value": adjusted_p, "significant_before_fdr": raw_p <= alpha,
            "significant_after_fdr": adjusted_p <= alpha, "fdr_method": FDR_METHOD,
            "fdr_alpha": alpha, "sample_count": int(row["request_pair_count"]),
            "bootstrap_count": int(row["bootstrap_iterations"]), "is_mock": "TRUE",
            "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3",
        })
    return sorted(output, key=lambda row: (row["raw_p_value"], row["strategy_a"], row["strategy_b"], row["metric"]))


def build(output_dir=ROOT / "output", input_path=INPUT_PATH, dry_run=False):
    before_hash = sha256(input_path)
    rows = correct(load_rows(input_path))
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / CSV_NAME).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        payload = {"evaluation_type": "offline_mock_pairwise_multiple_testing_correction",
                   "is_mock": True, "is_mock_evaluation": True, "data_source": "offline_simulation_v3",
                   "fdr_method": FDR_METHOD, "fdr_alpha": FDR_ALPHA, "test_family_size": len(rows),
                   "correction_scope": "all_84_pairwise_strategy_metric_tests_together",
                   "significant_before_fdr": sum(row["significant_before_fdr"] for row in rows),
                   "significant_after_fdr": sum(row["significant_after_fdr"] for row in rows),
                   "input_sha256": before_hash, "results": rows}
        (output_dir / JSON_NAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if sha256(input_path) != before_hash:
        raise RuntimeError("input pairwise file changed during FDR correction")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--input", type=Path, default=INPUT_PATH)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output"); args = parser.parse_args()
    rows = build(args.output_dir, args.input, args.dry_run)
    print(f"tests={len(rows)} significant_before_fdr={sum(r['significant_before_fdr'] for r in rows)} "
          f"significant_after_fdr={sum(r['significant_after_fdr'] for r in rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
