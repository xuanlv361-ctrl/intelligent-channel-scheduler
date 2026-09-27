"""Request-paired, seed-clustered bootstrap comparisons for seven Mock strategies."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "output" / "replicated_strategy_results_v4.json"
CSV_NAME = "pairwise_strategy_comparison_v4.csv"
JSON_NAME = "pairwise_strategy_comparison_v4.json"
BOOTSTRAP_ITERATIONS = 10000
BOOTSTRAP_SEED = 2026072204
LOWER_IS_BETTER = {"composite_regret", "average_latency_ms", "average_cost"}


def percentile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position); upper = min(lower + 1, len(ordered) - 1); fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def paired_bootstrap(values, iterations=BOOTSTRAP_ITERATIONS, seed=BOOTSTRAP_SEED):
    """Bootstrap 20 independent seed clusters built from request-level paired differences."""
    if not values:
        raise ValueError("paired differences must not be empty")
    rng = random.Random(seed); count = len(values); means = []
    for _ in range(iterations):
        means.append(sum(values[rng.randrange(count)] for _ in range(count)) / count)
    return sum(values) / count, percentile(means, .025), percentile(means, .975), means


def two_sided_bootstrap_p_value(bootstrap_means):
    """Return a finite-simulation two-sided p-value with a plus-one correction."""
    iterations = len(bootstrap_means)
    if not iterations:
        raise ValueError("bootstrap means must not be empty")
    non_positive = sum(value <= 0 for value in bootstrap_means)
    non_negative = sum(value >= 0 for value in bootstrap_means)
    return min(1.0, 2.0 * (min(non_positive, non_negative) + 1) / (iterations + 1))


def evaluate(input_path=INPUT_PATH, iterations=BOOTSTRAP_ITERATIONS, seed=BOOTSTRAP_SEED):
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    grouped = defaultdict(list)
    for row in payload["seed_pair_metrics"]:
        grouped[(row["strategy_a"], row["strategy_b"], row["metric"])].append(float(row["mean_request_paired_difference"]))
    rows = []
    for (left, right, metric), values in sorted(grouped.items()):
        stable = int(hashlib.sha256(f"{left}|{right}|{metric}".encode()).hexdigest()[:12], 16)
        mean, lower, upper, bootstrap_means = paired_bootstrap(values, iterations, seed + stable)
        raw_p_value = two_sided_bootstrap_p_value(bootstrap_means)
        lower_better = metric in LOWER_IS_BETTER
        if lower > 0:
            winner = right if lower_better else left
        elif upper < 0:
            winner = left if lower_better else right
        else:
            winner = "no_statistically_significant_winner"
        if lower_better:
            a_win_rate = sum(value < 0 for value in bootstrap_means) / iterations
        else:
            a_win_rate = sum(value > 0 for value in bootstrap_means) / iterations
        rows.append({"strategy_a": left, "strategy_b": right, "metric": metric,
                     "difference_definition": "strategy_a_minus_strategy_b",
                     "mean_difference": mean, "ci95_lower": lower, "ci95_upper": upper,
                     "raw_p_value": raw_p_value,
                     "win_rate": a_win_rate, "better_strategy": winner,
                     "request_pair_count": 100000, "seed_cluster_count": len(values),
                     "bootstrap_iterations": iterations, "bootstrap_seed": seed,
                     "bootstrap_unit": "simulation_seed_cluster_after_request_level_pairing",
                     "is_mock": "TRUE", "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3"})
    return rows


def build(output_dir=ROOT / "output", dry_run=False):
    rows = evaluate()
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        with (output_dir / CSV_NAME).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        payload = {"evaluation_type": "offline_mock_request_paired_seed_cluster_bootstrap",
                   "is_mock": True, "is_mock_evaluation": True, "data_source": "offline_simulation_v3",
                   "strategy_pair_count": 21, "metric_count": 4, "comparison_count": len(rows),
                   "bootstrap_iterations": BOOTSTRAP_ITERATIONS, "bootstrap_seed": BOOTSTRAP_SEED,
                   "multiple_comparison_adjustment": "none; interpret metric-wise 95% intervals with multiplicity caution",
                   "results": rows}
        (output_dir / JSON_NAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output"); args = parser.parse_args()
    rows = build(args.output_dir, args.dry_run)
    winners = {r["better_strategy"] for r in rows if r["better_strategy"] != "no_statistically_significant_winner"}
    print(f"strategy_pairs=21 comparisons={len(rows)} bootstrap_iterations={BOOTSTRAP_ITERATIONS} significant_winner_strategies={len(winners)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
