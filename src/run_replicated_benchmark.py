"""Run 20 deterministic offline Mock replications with request-level pairing."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from copy import deepcopy
from itertools import combinations
from pathlib import Path

import generate_simulation_workload as workload
import run_strategy_benchmark as benchmark
import strategy_engine

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "replicated_experiment_v3.json"
OUTPUT_DIR = ROOT / "output"
SUMMARY_NAME = "replicated_strategy_summary_v4.csv"
BY_REGIME_NAME = "replicated_strategy_by_regime_v4.csv"
PAIRED_NAME = "paired_strategy_comparison_v4_intermediate.csv"
JSON_NAME = "replicated_strategy_results_v4.json"
METRICS = ("success_rate", "average_latency_ms", "p95_latency_ms", "average_cost",
           "sla_violation_rate", "latency_regret", "cost_regret", "composite_regret")
T_CRITICAL_95_DF19 = 2.093024054


class OnlineStats:
    def __init__(self):
        self.n = 0; self.mean = 0.0; self.m2 = 0.0; self.minimum = math.inf; self.maximum = -math.inf

    def add(self, value):
        value = float(value); self.n += 1
        delta = value - self.mean; self.mean += delta / self.n; self.m2 += delta * (value - self.mean)
        self.minimum = min(self.minimum, value); self.maximum = max(self.maximum, value)

    def row(self, critical=1.96):
        sd = math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0
        half = critical * sd / math.sqrt(self.n) if self.n else 0.0
        return {"sample_count": self.n, "mean": self.mean, "standard_deviation": sd,
                "ci95_lower": self.mean - half, "ci95_upper": self.mean + half,
                "minimum": self.minimum if self.n else 0.0, "maximum": self.maximum if self.n else 0.0}


def _as_strings(rows):
    return [{key: str(value) for key, value in row.items()} for row in rows]


def _write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def run(config=None):
    replicated = config or strategy_engine.load_json(CONFIG_PATH)
    base_config = strategy_engine.load_json(workload.CONFIG_PATH)
    catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    strategies = catalog["supported_strategies"]
    seed_metrics = defaultdict(list)
    pair_stats = defaultdict(OnlineStats)
    seed_audit = []
    seed_pair_metrics = []
    selection_counts = defaultdict(lambda: defaultdict(int))
    total_decisions = 0
    for seed in replicated["simulation_seeds"]:
        simulation = deepcopy(base_config)
        simulation["simulation_seed"] = seed
        simulation["experiment_id"] = f"{replicated['experiment_id']}-S{seed}"
        requests, outcomes = workload.generate(simulation)
        requests_s, outcomes_s = _as_strings(requests), _as_strings(outcomes)
        decisions, summaries = benchmark.run_benchmark(requests_s, outcomes_s, strategies)
        total_decisions += len(decisions)
        for summary in summaries:
            for metric in METRICS:
                seed_metrics[(summary["strategy_name"], summary["environment_regime"], metric)].append(float(summary[metric]))
        by_request = defaultdict(dict)
        for decision in decisions:
            by_request[decision["request_id"]][decision["strategy_name"]] = decision
            selected = decision["selected_candidate"] or "UNROUTABLE"
            selection_counts[(decision["strategy_name"], "overall")][selected] += 1
            selection_counts[(decision["strategy_name"], decision["environment_regime"])][selected] += 1
        local_pair = defaultdict(OnlineStats)
        for request_id, mapped in by_request.items():
            for left, right in combinations(strategies, 2):
                a, b = mapped[left], mapped[right]
                values_a = {"success": a["realized_success"] == "TRUE", "latency_ms": float(a["realized_latency_ms"] or 0),
                            "cost": float(a["realized_cost"] or 0), "sla_violation": a["sla_violated"] == "TRUE",
                            "composite_regret": float(a["composite_regret_value"])}
                values_b = {"success": b["realized_success"] == "TRUE", "latency_ms": float(b["realized_latency_ms"] or 0),
                            "cost": float(b["realized_cost"] or 0), "sla_violation": b["sla_violated"] == "TRUE",
                            "composite_regret": float(b["composite_regret_value"])}
                for metric in values_a:
                    difference = float(values_a[metric]) - float(values_b[metric])
                    pair_stats[(left, right, metric)].add(difference)
                    local_pair[(left, right, metric)].add(difference)
        metric_names = {"success": "success_rate", "latency_ms": "average_latency_ms", "cost": "average_cost",
                        "composite_regret": "composite_regret"}
        for (left, right, metric), stats in sorted(local_pair.items()):
            if metric in metric_names:
                seed_pair_metrics.append({"simulation_seed": seed, "strategy_a": left, "strategy_b": right,
                                          "metric": metric_names[metric], "mean_request_paired_difference": stats.mean,
                                          "request_count": stats.n})
        sample_ids = {r["request_id"] for r in requests[:int(replicated["audit_sample_requests_per_seed"])]}
        seed_audit.append({"simulation_seed": seed, "request_count": len(requests), "potential_outcome_count": len(outcomes),
                           "decision_count": len(decisions), "shared_potential_outcomes_sha256": _outcome_digest(outcomes),
                           "audit_sample": [r for r in outcomes if r["request_id"] in sample_ids],
                           "is_mock_evaluation": True, "data_source": "offline_simulation_v3"})
    summary_rows, regime_rows = [], []
    for (strategy, regime, metric), values in sorted(seed_metrics.items()):
        stats = OnlineStats()
        for value in values: stats.add(value)
        row = {"strategy_name": strategy, "environment_regime": regime, "metric": metric,
               **stats.row(T_CRITICAL_95_DF19), "seed_count": len(values), "is_mock": "TRUE",
               "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3",
               "candidate_selection_distribution": json.dumps({k: v / sum(selection_counts[(strategy, regime)].values())
                   for k, v in sorted(selection_counts[(strategy, regime)].items())}, separators=(",", ":"))}
        (summary_rows if regime == "overall" else regime_rows).append(row)
    paired_rows = []
    for (left, right, metric), stats in sorted(pair_stats.items()):
        paired_rows.append({"strategy_a": left, "strategy_b": right, "metric": metric,
                            "difference_definition": "strategy_a_minus_strategy_b", **stats.row(1.96),
                            "paired_unit": "request_within_simulation_seed", "is_mock": "TRUE",
                            "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3"})
    assert len(set(replicated["simulation_seeds"])) == 20
    assert total_decisions == int(replicated["expected_total_decisions"])
    return summary_rows, regime_rows, paired_rows, seed_pair_metrics, seed_audit, total_decisions


def _outcome_digest(outcomes):
    import hashlib
    canonical = json.dumps(outcomes, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def build(output_dir=OUTPUT_DIR, dry_run=False):
    config = strategy_engine.load_json(CONFIG_PATH)
    summary, regimes, pairs, seed_pair_metrics, audits, total = run(config)
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write(output_dir / SUMMARY_NAME, summary); _write(output_dir / BY_REGIME_NAME, regimes); _write(output_dir / PAIRED_NAME, pairs)
        payload = {"evaluation_type": "offline_mock_replicated_strategy_benchmark", "is_mock": True, "is_mock_evaluation": True,
                   "data_source": "offline_simulation_v3", "simulation_seeds": config["simulation_seeds"],
                   "seed_count": len(config["simulation_seeds"]), "requests_per_seed": config["requests_per_seed"],
                   "total_requests": len(config["simulation_seeds"]) * config["requests_per_seed"],
                   "total_strategy_decisions": total, "common_random_numbers_within_seed": True,
                   "full_decisions_saved": False, "summary": summary, "by_regime": regimes,
                   "paired_comparisons": pairs, "seed_pair_metrics": seed_pair_metrics, "seed_audit": audits}
        (output_dir / JSON_NAME).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary, regimes, pairs, seed_pair_metrics, audits, total


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR); args = parser.parse_args()
    summary, regimes, pairs, seed_pair_metrics, audits, total = build(args.output_dir, args.dry_run)
    print(f"seeds={len(audits)} decisions={total} summary_rows={len(summary)} paired_rows={len(pairs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
