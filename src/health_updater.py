"""Offline dynamic health updater using windows, EWMA, and existing health logic."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import channel_health
from channel_observation import ChannelObservation
from health_window import HealthWindow
from time_decay import update_ewma


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WINDOW_POLICY = ROOT / "config" / "health_window_policy_v1.json"
DEFAULT_DECAY_POLICY = ROOT / "config" / "health_decay_policy_v1.json"
DEFAULT_SCENARIO = ROOT / "data" / "channel_health_update_simulation_v1.json"
DEFAULT_OUTPUT = ROOT / "output" / "channel_health_dynamic_snapshot_v1.json"
VALIDATION_TYPE = "offline simulation based dynamic health validation"


def load_json(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


@dataclass(slots=True)
class _ChannelState:
    window: HealthWindow
    ewma_success: float | None = None
    ewma_latency_ms: float | None = None
    ewma_cost: float | None = None
    last_result: dict[str, Any] | None = None


class HealthUpdater:
    def __init__(
        self,
        *,
        window_policy: Mapping[str, Any],
        decay_policy: Mapping[str, Any],
        health_policy: Mapping[str, Any],
        thresholds: Mapping[str, Any],
    ):
        self.window_size = int(window_policy["window_size"])
        self.max_age_seconds = window_policy.get("max_age_seconds")
        self.alpha = float(decay_policy["alpha"])
        self.health_policy = dict(health_policy)
        self.thresholds = dict(thresholds)
        channel_health.validate_policy(self.health_policy)
        channel_health.validate_thresholds(self.thresholds)
        self._states: dict[str, _ChannelState] = {}

    def _state(self, channel_id: str) -> _ChannelState:
        if channel_id not in self._states:
            self._states[channel_id] = _ChannelState(HealthWindow(
                window_size=self.window_size,
                max_age_seconds=self.max_age_seconds,
            ))
        return self._states[channel_id]

    def update(self, observation: ChannelObservation) -> dict[str, Any]:
        state = self._state(observation.channel_id)
        previous = state.last_result
        previous_latency = state.ewma_latency_ms
        state.window.append(observation)
        state.ewma_success = update_ewma(
            state.ewma_success, 1.0 if observation.success else 0.0,
            self.alpha, lower_bound=0.0, upper_bound=1.0,
        )
        state.ewma_latency_ms = update_ewma(
            state.ewma_latency_ms, observation.latency_ms,
            self.alpha, lower_bound=0.0,
        )
        state.ewma_cost = update_ewma(
            state.ewma_cost, observation.cost,
            self.alpha, lower_bound=0.0,
        )
        window_metrics = state.window.metrics()
        health_input = {
            "channel_id": observation.channel_id,
            "sample_count": window_metrics["sample_count"],
            "metrics": {
                "success_rate": state.ewma_success,
                "avg_latency_ms": state.ewma_latency_ms,
                "p95_latency_ms": window_metrics["p95_latency_ms"],
                "avg_cost": state.ewma_cost,
            },
        }
        scores = channel_health.calculate_health_score(
            health_input, self.health_policy
        )
        status = channel_health.classify_status(
            scores["health_score"], self.thresholds
        )
        reasons: list[str] = []
        if not observation.success:
            reasons.append("recent failure detected")
        if (
            previous_latency is not None
            and observation.latency_ms > previous_latency * 1.25
        ):
            reasons.append("recent latency increase")
        if previous is not None:
            if scores["health_score"] < previous["health_score"] - 1e-12:
                reasons.append("health score decreased")
            elif scores["health_score"] > previous["health_score"] + 1e-12:
                reasons.append("health score improved")
            if status != previous["status"]:
                reasons.append(
                    f"status changed from {previous['status']} to {status}"
                )
        else:
            reasons.append("channel state initialized")
        result = {
            "channel_id": observation.channel_id,
            "timestamp": observation.timestamp,
            "health_score": round(scores["health_score"], 6),
            "confidence": round(scores["confidence"], 6),
            "status": status,
            "window_metrics": {
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in window_metrics.items()
            },
            "decay_metrics": {
                "success_rate": round(state.ewma_success, 6),
                "avg_latency_ms": round(state.ewma_latency_ms, 6),
                "avg_cost": round(state.ewma_cost, 6),
                "alpha": self.alpha,
            },
            "update_reason": reasons or ["new observation incorporated"],
            "validation_type": VALIDATION_TYPE,
        }
        state.last_result = result
        return result

    def current(self, channel_id: str) -> dict[str, Any] | None:
        state = self._states.get(channel_id)
        return dict(state.last_result) if state and state.last_result else None


def build_updater() -> HealthUpdater:
    return HealthUpdater(
        window_policy=load_json(DEFAULT_WINDOW_POLICY),
        decay_policy=load_json(DEFAULT_DECAY_POLICY),
        health_policy=channel_health.load_json(channel_health.DEFAULT_POLICY_PATH),
        thresholds=channel_health.load_json(channel_health.DEFAULT_THRESHOLDS_PATH),
    )


def run_simulation(
    scenario: Mapping[str, Any],
    updater: HealthUpdater | None = None,
) -> dict[str, Any]:
    updater = updater or build_updater()
    timestamp = datetime.fromisoformat(
        str(scenario["start_timestamp"]).replace("Z", "+00:00")
    )
    interval = timedelta(seconds=float(scenario["interval_seconds"]))
    channel_id = str(scenario["channel_id"])
    checkpoints: dict[str, dict[str, Any]] = {}
    phase_reasons: dict[str, list[str]] = {}
    observation_count = 0
    for phase in scenario["phases"]:
        reasons: list[str] = []
        template = phase["observation"]
        for _ in range(int(phase["observation_count"])):
            observation = ChannelObservation.from_dict({
                "channel_id": channel_id,
                "timestamp": timestamp.isoformat(),
                "success": template["success"],
                "latency_ms": template["latency_ms"],
                "cost": template["cost"],
                "error_type": template["error_type"],
            })
            result = updater.update(observation)
            for reason in result["update_reason"]:
                if reason not in reasons:
                    reasons.append(reason)
            timestamp += interval
            observation_count += 1
        phase_id = str(phase["phase_id"])
        checkpoints[phase_id] = result
        if phase_id == "failure_burst":
            reasons.insert(0, "failure burst detected")
        phase_reasons[phase_id] = list(dict.fromkeys(reasons))
    expected = list(scenario["expected_transition"])
    actual = [
        checkpoints[str(phase["phase_id"])]["status"]
        for phase in scenario["phases"]
    ]
    return {
        "health_version": "dynamic-v1.0.0",
        "simulation_version": scenario["simulation_version"],
        "source_type": "offline_simulation",
        "validation_type": VALIDATION_TYPE,
        "observation_count": observation_count,
        "channels": [{
            "channel_id": channel_id,
            "before": checkpoints["baseline"],
            "after": checkpoints["failure_burst"],
            "recovered": checkpoints["recovery"],
            "reason": phase_reasons["failure_burst"],
            "expected_transition": expected,
            "actual_transition": actual,
            "transition_matches_expected": actual == expected,
        }],
        "limitations": list(scenario["limitations"]),
        "real_api_calls_performed": 0,
    }


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        snapshot = run_simulation(load_json(args.scenario))
        if not args.dry_run:
            write_json(args.output, snapshot)
        print(json.dumps({
            "status": "ok", "validation_type": VALIDATION_TYPE,
            "observation_count": snapshot["observation_count"],
            "dry_run": args.dry_run, "real_api_calls_performed": 0,
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
