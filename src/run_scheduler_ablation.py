"""Run authoritative repository A0-A14 through the real Scheduler.route path."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from run_full_scheduler_benchmark import (
    apply_bh, execute_variant, observation_digest, paired_stats, sha256,
)
from scheduler import OFFLINE_COMPONENT_HOOKS

DEFAULT_CONFIG = ROOT / "config" / "ablation_a0_a14_v1.json"


def load_definitions(config_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    definition = json.loads(config_path.read_text(encoding="utf-8"))
    benchmark_path = ROOT / definition["benchmark_config"]
    benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
    boundary = {"offline_only": True, "mock_only": True,
                "network_allowed": False, "real_execution_allowed": False}
    if definition["execution_boundary"] != boundary or benchmark["execution_boundary"] != boundary:
        raise ValueError("A0-A14 requires the offline Mock-only execution boundary")
    variants = definition["variants"]
    if [row["id"] for row in variants] != [f"A{i}" for i in range(15)]:
        raise ValueError("variants must be exactly A0 through A14")
    hooks = [row["hook"] for row in variants[1:]]
    if len(hooks) != len(set(hooks)) or set(hooks) != set(OFFLINE_COMPONENT_HOOKS):
        raise ValueError("A1-A14 must map one-to-one to production offline hooks")
    if [row["id"] for row in variants if row["safety_bypass"]] != ["A13", "A14"]:
        raise ValueError("only A13 and A14 may be safety bypass probes")
    return definition, benchmark


def run(config_path: Path, output_dir: Path, run_id: str) -> dict[str, Any]:
    definition, benchmark = load_definitions(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    strategy = benchmark["dataset"]["strategies"][0]
    observations: dict[str, list[dict[str, Any]]] = {}
    summaries = []
    for variant in definition["variants"]:
        with tempfile.TemporaryDirectory(
                prefix=f'.ablation-state-{variant["id"]}-', dir=output_dir) as state:
            rows, calls = execute_variant(
                benchmark, Path(state), strategy=strategy,
                variant_id=variant["id"], disabled_hook=variant["hook"])
        observations[variant["id"]] = rows
        changed_sets = {tuple(row["changed_hooks"]) for row in rows}
        expected = () if variant["hook"] is None else (variant["hook"],)
        change_proved = changed_sets == {expected}
        exercise_count = (sum(calls.values()) if variant["hook"] is None
                          else calls[variant["hook"]])
        summaries.append({
            "variant_id": variant["id"], "label": variant["label"],
            "disabled_production_hook": variant["hook"],
            "safety_bypass": variant["safety_bypass"],
            "configured_change_proved": change_proved,
            "branch_exercise_count": exercise_count,
            "exercise_status": "exercised" if exercise_count > 0 else "unexercised",
            "observation_count": len(rows),
            "network_called": any(row["network_called"] for row in rows),
            "observation_sha256": observation_digest(rows),
        })
    comparisons = []
    baseline = observations["A0"]
    for variant in definition["variants"][1:]:
        for metric in definition["statistics"]["metrics"]:
            row = paired_stats(baseline, observations[variant["id"]], metric)
            row["variant_id"] = variant["id"]
            comparisons.append(row)
    apply_bh(comparisons, float(definition["statistics"]["alpha"]))
    baseline_digest = observation_digest(baseline)
    result = {
        "schema_version": "full-scheduler-ablation-result-v2", "run_id": run_id,
        "definition_version": definition["definition_version"],
        "definition_status": definition["definition_status"],
        "execution_boundary": definition["execution_boundary"],
        "scheduler_route_executed": True,
        "shared_benchmark_executor": "run_full_scheduler_benchmark.execute_variant",
        "a0_benchmark_baseline_sha256": baseline_digest,
        "a0_parity_contract": definition["baseline"]["parity_requirement"],
        "variants": summaries, "paired_comparisons": comparisons,
        "sensitivity_included": False,
        "sensitivity_reference": "full Scheduler benchmark sensitivity_results.json",
        "supersedes": definition["supersedes"],
        "network_called": False, "real_execution": False,
    }
    (output_dir / "ablation_results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "paired_observations.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for variant in definition["variants"]:
            for row in observations[variant["id"]]:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    with (output_dir / "ablation_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summaries[0])); writer.writeheader(); writer.writerows(summaries)
    lines = ["# Full Scheduler A0-A14 Ablation v2", "", f"Run: `{run_id}`", "",
             "A0 and every Ax execute the shared full benchmark `Scheduler.route` path. Prior synthetic-loop results are retained as a superseded prototype.", "",
             "| ID | Disabled production hook | Changed proved | Branch count | Status | Network |",
             "|---|---|---:|---:|---|---:|"]
    for row in summaries:
        lines.append(f"| {row['variant_id']} | {row['disabled_production_hook'] or 'none'} | {str(row['configured_change_proved']).lower()} | {row['branch_exercise_count']} | {row['exercise_status']} | {str(row['network_called']).lower()} |")
    lines += ["", "Sensitivity is intentionally external to ablation and remains exploratory.", ""]
    (output_dir / "ablation_results.md").write_text("\n".join(lines), encoding="utf-8")
    artifact_names = ["ablation_results.json", "paired_observations.jsonl",
                      "ablation_summary.csv", "ablation_results.md"]
    source_names = ["src/scheduler.py", "src/run_full_scheduler_benchmark.py",
                    "src/run_scheduler_ablation.py"]
    benchmark_path = ROOT / definition["benchmark_config"]
    manifest = {
        "schema_version": "full-scheduler-ablation-provenance-v2", "run_id": run_id,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version, "platform": platform.platform(),
        "inputs": {str(config_path.relative_to(ROOT)).replace('\\', '/'): sha256(config_path),
                   definition["benchmark_config"]: sha256(benchmark_path)},
        "sources": {name: sha256(ROOT / name) for name in source_names},
        "artifacts": {name: sha256(output_dir / name) for name in artifact_names},
        "network_called": False, "real_execution": False,
    }
    (output_dir / "provenance_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    result = run(args.config.resolve(), args.output_dir.resolve(), args.run_id)
    print(json.dumps({"run_id": args.run_id, "variants": len(result["variants"]),
                      "scheduler_route_executed": True, "network_called": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
