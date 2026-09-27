"""Generate deterministic offline simulation metrics and reuse channel_health."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import channel_health
from metrics_store import MetricsStore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "health_simulation_v1.json"
DEFAULT_METRICS_OUTPUT = ROOT / "data" / "channel_health_simulation_metrics_v1.json"
DEFAULT_HEALTH_OUTPUT = ROOT / "output" / "channel_health_simulation_snapshot_v1.json"
VALIDATION_TYPE = "offline simulation based health validation"


class HealthSimulationError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def build_metrics_dataset(config: Mapping[str, Any]) -> dict[str, Any]:
    scenarios = config["channel_scenarios"]
    if int(config["channels"]) != len(scenarios) or len(scenarios) < 8:
        raise HealthSimulationError("configured channel count must match at least 8 scenarios")
    default_sample_count = int(config["default_sample_count"])
    if default_sample_count <= 0:
        raise HealthSimulationError("default_sample_count must be positive")
    channels = []
    for scenario in scenarios:
        channels.append({
            "channel_id": str(scenario["channel_id"]),
            "sample_count": int(scenario.get("sample_count", default_sample_count)),
            "metrics": deepcopy(scenario["metrics"]),
            "simulation_condition": str(scenario["condition"]),
        })
    dataset = {
        "source_type": str(config["source_type"]),
        "validation_type": VALIDATION_TYPE,
        "metrics_version": str(config["metrics_version"]),
        "updated_at": str(config["updated_at"]),
        "simulation_version": str(config["simulation_version"]),
        "channels": channels,
        "generation_rules": deepcopy(config["metric_generation_rules"]),
        "limitations": deepcopy(config["limitations"]),
    }
    MetricsStore(dataset)
    return dataset


def simulation_policy(
    base_policy: Mapping[str, Any], config: Mapping[str, Any]
) -> dict[str, Any]:
    policy = deepcopy(dict(base_policy))
    overrides = config["health_policy_overrides"]
    for section, values in overrides.items():
        if not isinstance(values, dict) or section not in policy:
            raise HealthSimulationError(f"invalid health policy override: {section}")
        policy[section].update(values)
    channel_health.validate_policy(policy)
    return policy


def build_simulation_snapshot(
    dataset: Mapping[str, Any],
    config: Mapping[str, Any],
    base_policy: Mapping[str, Any],
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    policy = simulation_policy(base_policy, config)
    snapshot = channel_health.build_health_snapshot(
        MetricsStore(dict(dataset)), policy, thresholds
    )
    expected = {
        str(row["channel_id"]): str(row["expected_status"])
        for row in config["channel_scenarios"]
    }
    for row in snapshot["channels"]:
        row["expected_status"] = expected[row["channel_id"]]
        row["status_matches_expected"] = row["status"] == row["expected_status"]
    snapshot.update({
        "source_type": "offline_simulation",
        "validation_type": VALIDATION_TYPE,
        "simulation_version": config["simulation_version"],
        "confidence_sample_threshold": policy["confidence"]["sample_threshold"],
        "all_statuses_match_expected": all(
            row["status_matches_expected"] for row in snapshot["channels"]
        ),
    })
    snapshot["limitations"] = list(config["limitations"])
    return snapshot


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )


def generate(config_path: Path = DEFAULT_CONFIG_PATH) -> tuple[dict[str, Any], dict[str, Any]]:
    config = load_json(config_path)
    dataset = build_metrics_dataset(config)
    snapshot = build_simulation_snapshot(
        dataset, config,
        channel_health.load_json(channel_health.DEFAULT_POLICY_PATH),
        channel_health.load_json(channel_health.DEFAULT_THRESHOLDS_PATH),
    )
    return dataset, snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--metrics-output", type=Path, default=DEFAULT_METRICS_OUTPUT)
    parser.add_argument("--health-output", type=Path, default=DEFAULT_HEALTH_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        metrics, snapshot = generate(args.config)
        if not args.dry_run:
            write_json(args.metrics_output, metrics)
            write_json(args.health_output, snapshot)
        print(json.dumps({
            "status": "ok", "channel_count": len(metrics["channels"]),
            "validation_type": VALIDATION_TYPE, "dry_run": args.dry_run,
            "real_api_calls_performed": 0,
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps(
            {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
            ensure_ascii=False,
        ), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
