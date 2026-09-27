"""Generate a deterministic offline Mock workload and shared potential outcomes."""

from __future__ import annotations

import argparse
import csv
import json
import random
from copy import deepcopy
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "simulation_experiment_v2.json"
CANDIDATES_PATH = ROOT / "data" / "mock_channel_catalog_v3.csv"
DEFAULT_OUTPUT_DIR = ROOT / "data"
REQUESTS_NAME = "simulation_requests_v3.csv"
OUTCOMES_NAME = "simulation_potential_outcomes_v3.csv"
REQUEST_COLUMNS = [
    "request_id", "request_index", "requested_model", "stream_required", "input_tokens",
    "output_tokens", "currency", "decision_time", "budget_cny", "timeout_ms",
    "environment_regime", "simulation_seed", "simulation_config_version", "experiment_id",
    "is_mock", "is_mock_evaluation", "data_source",
]
OUTCOME_COLUMNS = [
    "request_id", "candidate_id", "availability", "realized_success",
    "realized_latency_ms", "realized_cost", "sla_violated", "latent_success_probability",
    "environment_regime",
    "simulation_seed", "simulation_config_version", "experiment_id", "is_mock",
    "is_mock_evaluation", "data_source",
]


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def effective_regime(regime: dict[str, Any], candidate_id: str) -> dict[str, Any]:
    result = dict(regime)
    override = regime.get("candidate_overrides", {}).get(candidate_id, {})
    for field in ("latency_multiplier", "price_multiplier"):
        result[field] = float(regime[field]) * float(override.get(field, 1.0))
    result["success_rate_delta"] = float(regime["success_rate_delta"]) + float(override.get("success_rate_delta", 0.0))
    result["availability_probability"] = float(override.get("availability_probability", regime["availability_probability"]))
    return result


def build_candidate_snapshot(base: list[dict[str, str]], regime: dict[str, Any]) -> list[dict[str, str]]:
    """Return decision-time features only; no realized future fields are accepted."""
    rows = deepcopy(base)
    for row in rows:
        row["success_rate"] = row.get("success_rate", row["success_probability_prior"])
        effective = effective_regime(regime, row["candidate_id"])
        row["latency_ms"] = str(round(float(row["latency_ms"]) * effective["latency_multiplier"], 6))
        row["input_price_per_1m"] = str(round(float(row["input_price_per_1m"]) * effective["price_multiplier"], 6))
        row["output_price_per_1m"] = str(round(float(row["output_price_per_1m"]) * effective["price_multiplier"], 6))
        observed = min(1.0, max(0.0, float(row["success_rate"])))
        row["observed_success_rate"] = str(round(observed, 9))
        # Kept as a compatibility alias for the v1 decision engine. It is an
        # observed feature, never the simulator's hidden success probability.
        row["success_rate"] = row["observed_success_rate"]
        row["is_mock"] = "TRUE"
        row["data_source"] = "offline_simulation_v3"
    return rows


def draw_latent_success_probabilities(config: dict[str, Any], rng: random.Random) -> dict[str, float]:
    """Draw hidden Mock truth from configured distributions, never candidate observations."""
    parameters = config["latent_success_model"]["candidate_beta_parameters"]
    return {
        candidate_id: rng.betavariate(float(values["alpha"]), float(values["beta"]))
        for candidate_id, values in sorted(parameters.items())
    }


def generate(config: dict[str, Any] | None = None, base: list[dict[str, str]] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    config = config or load_json(CONFIG_PATH)
    base = base or load_csv(CANDIDATES_PATH)
    rng = random.Random(int(config["simulation_seed"]))
    latent_probabilities = draw_latent_success_probabilities(config, rng)
    requests: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    index = 0
    for regime_name, regime in config["regimes"].items():
        regime_count = int(config.get("regime_request_counts", {}).get(regime_name, config.get("requests_per_regime", 0)))
        for _ in range(regime_count):
            request_id = f"SIM-{index + 1:05d}"
            input_tokens = rng.randint(*regime["input_tokens"])
            output_tokens = rng.randint(*regime["output_tokens"])
            request = {
                "request_id": request_id, "request_index": index,
                "requested_model": "deepseek-v4-flash",
                "stream_required": "TRUE" if rng.random() < regime["stream_probability"] else "FALSE",
                "input_tokens": input_tokens, "output_tokens": output_tokens, "currency": "CNY",
                "decision_time": "2026-07-21 12:00:00",
                "budget_cny": round(rng.uniform(*regime["budget_cny"]), 9),
                "timeout_ms": rng.randint(*regime["timeout_ms"]), "environment_regime": regime_name,
                "simulation_seed": config["simulation_seed"],
                "simulation_config_version": config["simulation_config_version"],
                "experiment_id": config["experiment_id"], "is_mock": "TRUE",
                "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3",
            }
            requests.append(request)
            for candidate in base:
                effective = effective_regime(regime, candidate["candidate_id"])
                available = rng.random() < effective["availability_probability"]
                success_probability = min(1.0, max(0.0, latent_probabilities[candidate["candidate_id"]] + effective["success_rate_delta"]))
                success = available and rng.random() < success_probability
                latency = max(1.0, float(candidate["latency_ms"]) * effective["latency_multiplier"] * rng.uniform(0.8, 1.2))
                cost = (
                    input_tokens * float(candidate["input_price_per_1m"])
                    + output_tokens * float(candidate["output_price_per_1m"])
                ) / 1_000_000 * effective["price_multiplier"]
                outcomes.append({
                    "request_id": request_id, "candidate_id": candidate["candidate_id"],
                    "availability": "TRUE" if available else "FALSE",
                    "realized_success": "TRUE" if success else "FALSE",
                    "realized_latency_ms": round(latency, 6), "realized_cost": round(max(0.0, cost), 12),
                    "sla_violated": "TRUE" if (not success or latency > regime["sla_latency_ms"]) else "FALSE",
                    "latent_success_probability": round(success_probability, 9),
                    "environment_regime": regime_name, "simulation_seed": config["simulation_seed"],
                    "simulation_config_version": config["simulation_config_version"],
                    "experiment_id": config["experiment_id"], "is_mock": "TRUE",
                    "is_mock_evaluation": "TRUE", "data_source": "offline_simulation_v3",
                })
            index += 1
    assert len(requests) == int(config["total_requests"])
    assert len(outcomes) == len(requests) * len(base)
    assert len(base) == int(config["candidate_count"])
    return requests, outcomes


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def build(output_dir: Path = DEFAULT_OUTPUT_DIR, dry_run: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requests, outcomes = generate()
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_csv(output_dir / REQUESTS_NAME, REQUEST_COLUMNS, requests)
        write_csv(output_dir / OUTCOMES_NAME, OUTCOME_COLUMNS, outcomes)
    return requests, outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    requests, outcomes = build(args.output_dir, args.dry_run)
    print(f"requests={len(requests)} potential_outcomes={len(outcomes)}" + (" dry_run=TRUE" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
