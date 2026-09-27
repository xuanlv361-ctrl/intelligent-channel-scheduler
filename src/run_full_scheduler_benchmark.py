"""Deterministic offline benchmark that executes the complete Scheduler.route path."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import statistics
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from backend.capability_evidence_service import CapabilityEvidenceService
from backend.circuit_breaker_service import CircuitBreakerService
from backend.exploration_governance_service import ExplorationGovernanceService
from backend.scheduler_attribution_service import SchedulerAttributionService
from backend.sticky_routing_service import StickyRoutingService
from candidate_resolver import CandidateResolver, ResolvedCandidates
from channel_adapter import MockChannelAdapter
from decision_logger import DecisionLogger
from scheduler import OFFLINE_COMPONENT_HOOKS, Scheduler

DEFAULT_CONFIG = ROOT / "config" / "full_scheduler_benchmark_v1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_unit(*parts: object) -> float:
    raw = "|".join(map(str, parts)).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class DeterministicMetricProvider:
    def __init__(self) -> None:
        self.calls = 0

    def apply(self, candidates, **_scope):
        self.calls += 1
        rows = []
        for row in candidates:
            item = dict(row)
            item["dynamic_metrics_state"] = "ready"
            item["dynamic_metric_snapshot_id"] = "BENCH-METRIC-V1"
            item["dynamic_metrics_block_reason"] = ""
            if self.calls % 6 == 0 and item["candidate_id"] == "MOCK-DS-FAST":
                item["is_mock"] = "FALSE"
                item["source_type"] = "offline_guard_probe"
            rows.append(item)
        return rows, {"provider": "deterministic_benchmark_snapshot",
                      "applied_count": len(rows), "snapshot_ids": ["BENCH-METRIC-V1"],
                      "raw_evidence_scanned": False}


class AllPassStickyEligibility:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, *, candidates, **_kwargs):
        self.calls += 1
        from sticky_routing import GATE_ORDER
        return {"gate_results": {name: True for name in GATE_ORDER},
                "eligible_channel_ids": [row["candidate_id"] for row in candidates],
                "evidence": {"metric_snapshot_id": "BENCH-METRIC-V1",
                             "confidence_snapshot_id": "BENCH-CONF-V1",
                             "evidence_ids": ["BENCH-EVIDENCE-V1"]},
                "network_called": False}


class BenchmarkResolver:
    """Count and delegate to the production mode-specific resolver."""
    def __init__(self) -> None:
        self.delegate = CandidateResolver()
        self.calls = 0

    def resolve(self, mode: str) -> ResolvedCandidates:
        self.calls += 1
        resolved = self.delegate.resolve(mode)
        return ResolvedCandidates(
            mode=resolved.mode, catalog_path=resolved.catalog_path,
            catalog_version=resolved.catalog_version,
            catalog_sha256=resolved.catalog_sha256,
            source_type=resolved.source_type,
            candidates=[dict(row) for row in resolved.candidates],
            raw_candidate_count=resolved.raw_candidate_count)


class DeterministicMockAdapter(MockChannelAdapter):
    def __init__(self, seed: int, repetition: int) -> None:
        super().__init__()
        self.seed, self.repetition, self.calls = seed, repetition, 0

    def send(self, request, candidate):
        self.calls += 1
        base = super().send(request, candidate)
        failed = stable_unit(self.seed, self.repetition, request.request_id,
                             candidate["candidate_id"]) < 0.22
        return {**base,
                "status": "timeout" if failed else "success",
                "error_category": "upstream_timeout" if failed else None,
                "latency_ms": float(candidate.get("latency_ms") or 0),
                "is_mock": True, "network_called": False}


class CountingAttributionStore:
    def __init__(self, path: Path) -> None:
        self.delegate = SchedulerAttributionService(path)
        self.calls = 0

    def record_scheduler_decision(self, decision):
        self.calls += 1
        allowed = {
            key: decision.get(key) for key in (
                "decision_id", "run_id", "request_id", "requested_model",
                "selected_target_id", "selected_channel_id",
                "decision_policy_version", "metric_snapshot_ids",
                "confidence_snapshot_ids", "downstream_request_correlation_id",
                "execution_status", "executed_candidate_id", "executed_channel_id")
        }
        return self.delegate.record_scheduler_decision(allowed)


def _sticky_policy(workdir: Path) -> Path:
    policy = json.loads((ROOT / "config" / "sticky_routing_policy_v1.json").read_text(encoding="utf-8"))
    policy.update({"enabled": True, "ttl_seconds": 600,
                   "maximum_total_duration_seconds": 3600})
    path = workdir / "sticky_policy.json"
    path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    return path


def _services(workdir: Path, seed: int, repetition: int):
    workdir.mkdir(parents=True, exist_ok=True)
    clock = FixedClock()
    metric = DeterministicMetricProvider()
    sticky_gate = AllPassStickyEligibility()
    sticky = StickyRoutingService(
        workdir / "state.sqlite3", _sticky_policy(workdir),
        secret=hashlib.sha256(b"full-scheduler-benchmark-test-key").hexdigest(),
        key_id="benchmark-key-v1",
        clock=clock)
    circuit_policy = json.loads((ROOT / "config" / "circuit_breaker_policy_v1.json").read_text(encoding="utf-8"))
    circuit_policy["failure_threshold"] = 3
    circuit = CircuitBreakerService(workdir / "circuit.sqlite3", circuit_policy, clock=clock)
    capability = CapabilityEvidenceService(
        workdir / "capability.sqlite3", ROOT / "config" / "capability_evidence_policy_v1.json",
        clock=clock)
    capability.record_evidence({
        "evidence_id": "BENCH-CAP-TEXT", "evidence_type": "contract_test",
        "observed_at": "2026-07-31T12:00:00Z", "environment_id": "offline_benchmark",
        "subject_id": "deepseek-v4-flash", "subject_version": "v1",
        "source": "local", "requirement": "text", "state": "supported"})
    capability.record_evidence({
        "evidence_id": "BENCH-CAP-STREAM", "evidence_type": "contract_test",
        "observed_at": "2026-07-31T12:00:00Z", "environment_id": "offline_benchmark",
        "subject_id": "deepseek-v4-flash", "subject_version": "v1",
        "source": "local", "requirement": "stream", "state": "supported"})
    exploration = ExplorationGovernanceService(
        workdir / "exploration.sqlite3", ROOT / "config" / "exploration_policy_v1.json",
        clock=clock, environ={})
    attribution = CountingAttributionStore(workdir / "attribution.sqlite3")
    adapter = DeterministicMockAdapter(seed, repetition)
    return metric, sticky_gate, sticky, circuit, capability, exploration, attribution, adapter


def request_payload(config: Mapping[str, Any], *, strategy: str, seed: int,
                    repetition: int, index: int) -> dict[str, Any]:
    dataset = config["dataset"]
    return {
        "request_id": f"BENCH-S{seed}-R{repetition}-Q{index:03d}",
        "requested_model": dataset["requested_model"], "stream": index % 4 == 0,
        "input_tokens": 400 + index * 37, "output_tokens": 120 + index * 11,
        "currency": "CNY", "strategy": strategy, "mode": dataset["mode"],
        "sticky_affinity_key": f"opaque-benchmark-group-{index % 3}",
        "capability_scope": {"required_modalities": ["text"], "stream": index % 4 == 0},
        "metadata": {"environment_id": dataset["environment_id"],
                     "request_profile_id": dataset["request_profile_id"],
                     "capability_subject_version": "v1",
                     "run_id": f"FULL-SCHEDULER-S{seed}-R{repetition}-{strategy}"},
    }


def _metrics(decision: Mapping[str, Any]) -> dict[str, float]:
    attempts = decision.get("attempts") or []
    success = decision.get("execution_status") == "success"
    executed = decision.get("executed_candidate_id") or decision.get("recommended_candidate")
    ranking = {row["candidate_id"]: row for row in decision.get("candidate_ranking", [])}
    selected = ranking.get(executed) or {}
    latency = float(selected.get("latency_ms") or 0.0)
    cost = float(selected.get("estimated_cost") or 0.0)
    reliability = 1.0 if success else 0.0
    quality = reliability - latency / 6000.0 - cost * 10.0 - .03 * max(0, len(attempts) - 1)
    return {"quality": quality, "reliability": reliability,
            "latency_ms": latency, "cost_cny": cost}


def execute_variant(config: Mapping[str, Any], workdir: Path, *, strategy: str,
                    variant_id: str, disabled_hook: str | None = None,
                    request_limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    dataset = config["dataset"]
    limit = request_limit or int(dataset["request_count"])
    observations: list[dict[str, Any]] = []
    aggregate = {name: 0 for name in OFFLINE_COMPONENT_HOOKS}
    for seed in dataset["seeds"]:
        for repetition in range(int(dataset["repetitions_per_seed"])):
            run_dir = workdir / f"S{seed}-R{repetition}"
            metric, sticky_gate, sticky, circuit, capability, exploration, attribution, adapter = _services(run_dir, seed, repetition)
            scheduler = Scheduler(
                resolver=BenchmarkResolver(), adapter=adapter,
                logger=DecisionLogger(run_dir / "decisions.jsonl"), random_seed=seed,
                id_generator=lambda req, index, s=seed, r=repetition, v=variant_id:
                    f"{v}-S{s}-R{r}-D{index:03d}",
                metric_provider=metric, sticky_service=sticky,
                sticky_eligibility_provider=sticky_gate,
                circuit_breaker_service=circuit,
                capability_evidence_service=capability,
                exploration_service=exploration, attribution_store=attribution,
                offline_component_overrides=({disabled_hook: False} if disabled_hook else {}),
                offline_ablation_id=variant_id)
            for index in range(limit):
                decision = scheduler.route(request_payload(
                    config, strategy=strategy, seed=seed,
                    repetition=repetition, index=index))
                trace = decision["offline_component_trace"]
                for name, count in trace["branch_counts"].items():
                    aggregate[name] += count
                values = _metrics(decision)
                observations.append({
                    "variant_id": variant_id, "strategy": strategy,
                    "pair_id": f"S{seed}-R{repetition}-Q{index:03d}",
                    "seed": seed, "repetition": repetition, "request_index": index,
                    **values, "selected_target_id": decision.get("selected_target_id"),
                    "executed_candidate_id": decision.get("executed_candidate_id"),
                    "execution_status": decision["execution_status"],
                    "attempt_count": len(decision.get("attempts") or []),
                    "changed_hooks": trace["changed_hooks"],
                    "network_called": decision["network_called"],
                    "authoritative_actual_channel": decision["authoritative_actual_channel"],
                })
    return observations, aggregate


def paired_stats(base: list[dict[str, Any]], other: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    index = {row["pair_id"]: float(row[metric]) for row in base}
    values = [float(row[metric]) - index[row["pair_id"]] for row in other]
    mean = statistics.fmean(values); sd = statistics.stdev(values) if len(values) > 1 else 0.0
    half = 1.96 * sd / math.sqrt(len(values)) if values else 0.0
    nonzero = [v for v in values if abs(v) > 1e-15]
    if nonzero:
        positive = sum(v > 0 for v in nonzero); k = min(positive, len(nonzero) - positive)
        p_value = min(1.0, 2 * sum(math.comb(len(nonzero), i) for i in range(k + 1)) / 2**len(nonzero))
    else:
        p_value = 1.0
    return {"metric": metric, "n_pairs": len(values), "mean_difference": mean,
            "ci95_lower": mean - half, "ci95_upper": mean + half,
            "effect_size_dz": mean / sd if sd else 0.0, "p_value": p_value}


def apply_bh(rows: list[dict[str, Any]], alpha: float) -> None:
    ordered = sorted(rows, key=lambda row: row["p_value"]); running = 1.0; m = len(rows)
    for rank in range(m, 0, -1):
        row = ordered[rank - 1]; running = min(running, row["p_value"] * m / rank)
        row["p_value_bh"] = running; row["bh_significant"] = running <= alpha


def observation_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    stable = [{k: v for k, v in row.items() if k not in {"variant_id", "changed_hooks"}} for row in rows]
    raw = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def run(config_path: Path, output_dir: Path, run_id: str) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config["execution_boundary"] != {"offline_only": True, "mock_only": True,
                                        "network_allowed": False, "real_execution_allowed": False}:
        raise ValueError("full Scheduler benchmark must remain offline and Mock-only")
    output_dir.mkdir(parents=True, exist_ok=True)
    all_rows = []; calls = {}; by_strategy = {}
    for strategy in config["dataset"]["strategies"]:
        with tempfile.TemporaryDirectory(
                prefix=f".benchmark-state-{strategy}-", dir=output_dir) as state:
            rows, component_calls = execute_variant(
                config, Path(state), strategy=strategy, variant_id="A0")
        all_rows.extend(rows); by_strategy[strategy] = rows; calls[strategy] = component_calls
    base_name = config["dataset"]["strategies"][0]; comparisons = []
    for strategy in config["dataset"]["strategies"][1:]:
        for metric in config["statistics"]["metrics"]:
            row = paired_stats(by_strategy[base_name], by_strategy[strategy], metric)
            row.update({"strategy": strategy, "baseline_strategy": base_name})
            comparisons.append(row)
    apply_bh(comparisons, float(config["statistics"]["alpha"]))
    summaries = [{"strategy": strategy, "observations": len(rows),
                  **{f"mean_{m}": statistics.fmean(float(r[m]) for r in rows)
                     for m in config["statistics"]["metrics"]}}
                 for strategy, rows in by_strategy.items()]
    sensitivity = []
    for count in config["sensitivity"]["request_counts"]:
        with tempfile.TemporaryDirectory(
                prefix=f".sensitivity-state-{count}-", dir=output_dir) as state:
            rows, _ = execute_variant(config, Path(state), strategy=base_name,
                                      variant_id="SENSITIVITY", request_limit=int(count))
        sensitivity.append({"request_count_per_repetition": count,
                            "observations": len(rows),
                            "mean_quality": statistics.fmean(r["quality"] for r in rows)})
    result = {"schema_version": "full-scheduler-benchmark-result-v1", "run_id": run_id,
              "definition_status": config["definition_status"],
              "execution_boundary": config["execution_boundary"],
              "scheduler_route_executed": True, "summaries": summaries,
              "paired_comparisons": comparisons, "component_call_counts": calls,
              "baseline_strategy": base_name,
              "baseline_observation_sha256": observation_digest(by_strategy[base_name]),
              "sensitivity_artifact": "sensitivity_results.json",
              "network_called": False, "real_execution": False}
    (output_dir / "benchmark_results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (output_dir / "sensitivity_results.json").write_text(json.dumps({
        "status": "exploratory_separate_from_confirmatory_benchmark", "rows": sensitivity}, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "observations.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for row in all_rows: handle.write(json.dumps(row, sort_keys=True) + "\n")
    with (output_dir / "benchmark_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0])); writer.writeheader(); writer.writerows(summaries)
    lines = ["# Full Scheduler Benchmark v1", "", f"Run: `{run_id}`", "",
             "This offline Mock benchmark executed `Scheduler.route`; it is not UAT or real-channel evidence.", "",
             "| Strategy | Observations | Quality | Reliability | Latency ms | Cost CNY |",
             "|---|---:|---:|---:|---:|---:|"]
    for row in summaries:
        lines.append(f"| {row['strategy']} | {row['observations']} | {row['mean_quality']:.6f} | {row['mean_reliability']:.6f} | {row['mean_latency_ms']:.3f} | {row['mean_cost_cny']:.8f} |")
    lines += ["", "Sensitivity is separate and excluded from confirmatory multiple-testing correction.", ""]
    (output_dir / "benchmark_results.md").write_text("\n".join(lines), encoding="utf-8")
    artifact_names = ["benchmark_results.json", "sensitivity_results.json", "observations.jsonl",
                      "benchmark_summary.csv", "benchmark_results.md"]
    sources = ["src/scheduler.py", "src/run_full_scheduler_benchmark.py",
               "backend/sticky_routing_service.py", "backend/circuit_breaker_service.py",
               "backend/capability_evidence_service.py", "backend/exploration_governance_service.py",
               "backend/scheduler_attribution_service.py"]
    try:
        config_label = str(config_path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        config_label = str(config_path)
    manifest = {"schema_version": "full-scheduler-provenance-v1", "run_id": run_id,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "python": sys.version, "platform": platform.platform(),
                "inputs": {config_label: sha256(config_path)},
                "sources": {name: sha256(ROOT / name) for name in sources},
                "artifacts": {name: sha256(output_dir / name) for name in artifact_names},
                "network_called": False, "real_execution": False}
    (output_dir / "provenance_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    result = run(args.config.resolve(), args.output_dir.resolve(), args.run_id)
    print(json.dumps({"run_id": args.run_id, "scheduler_route_executed": True,
                      "observations": sum(r["observations"] for r in result["summaries"]),
                      "network_called": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
