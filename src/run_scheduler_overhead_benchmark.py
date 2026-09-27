"""Measure cold per-request construction versus the reusable Scheduler runtime."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for value in (ROOT, ROOT / "src"):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from decision_logger import DecisionLogger
from scheduler import Scheduler
from scheduler_timing import SchedulerTimingRecorder


def _request(index: int, prefix: str) -> dict:
    return {
        "request_id": f"{prefix}-{index:04d}", "requested_model": "deepseek-v4-flash",
        "stream": False, "input_tokens": 100, "output_tokens": 50, "currency": "CNY",
        "strategy": "fastest_first", "mode": "simulation",
    }


def _run(output_dir: Path, *, name: str, samples: int, reuse_scheduler: bool) -> dict:
    directory = output_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    recorder = SchedulerTimingRecorder(directory / "timing.sqlite3")
    logger = DecisionLogger(directory / "decisions.jsonl")
    shared = Scheduler(logger=logger, runtime_log_enabled=False, timing_recorder=recorder) if reuse_scheduler else None
    for index in range(samples):
        scheduler = shared or Scheduler(logger=logger, runtime_log_enabled=False, timing_recorder=recorder)
        result = scheduler.route(_request(index, name.upper()))
        if result.get("network_called"):
            raise RuntimeError("scheduler overhead benchmark attempted network access")
    return recorder.summary(window_minutes=10080)


def run(output_dir: Path, samples: int) -> dict:
    if samples < 20:
        raise ValueError("minimum_20_samples_required")
    output_dir.mkdir(parents=True, exist_ok=True)
    before = _run(output_dir, name="cold_per_request", samples=samples, reuse_scheduler=False)
    after = _run(output_dir, name="reused_runtime", samples=samples, reuse_scheduler=True)
    before_total = next(x for x in before["stages"] if x["stage"] == "scheduler_total")
    after_total = next(x for x in after["stages"] if x["stage"] == "scheduler_total")
    comparison = {}
    for key in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
        old, new = float(before_total[key]), float(after_total[key])
        comparison[key] = {"before": old, "after": new, "change_percent": ((new - old) / old * 100) if old else None}
    result = {
        "schema_version": "scheduler_overhead_benchmark_v1",
        "environment": {"python": sys.version, "platform": platform.platform()},
        "dataset": "fixed_local_simulation_requests_v1", "configuration_version": "decision-policy-v2.0.0",
        "sample_count_per_variant": samples,
        "before": {"implementation": "construct_scheduler_per_request", **before},
        "after": {"implementation": "reuse_initialized_scheduler_runtime", **after},
        "scheduler_total_comparison": comparison,
        "provider_latency_included": False, "network_calls": 0, "completion_calls": 0,
        "interpretation": "The comparison isolates reusable local runtime construction; it does not measure provider latency.",
    }
    (output_dir / "scheduler_overhead_comparison.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Scheduler 开销对比", "", f"每组样本：{samples}",
        "", "| 指标 | 每请求重建 | 复用运行时 | 变化 |", "|---|---:|---:|---:|",
    ]
    for key, value in comparison.items():
        change = "n/a" if value["change_percent"] is None else f"{value['change_percent']:.2f}%"
        lines.append(f"| {key} | {value['before']:.4f} ms | {value['after']:.4f} ms | {change} |")
    lines += ["", "范围只包含 Scheduler 计算和本地持久化；供应商网络、生成与客户端渲染均排除。", ""]
    (output_dir / "scheduler_overhead_comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=100)
    args = parser.parse_args()
    result = run(args.output_dir.resolve(), args.samples)
    print(json.dumps({"status": "ready", "samples_per_variant": result["sample_count_per_variant"], "network_calls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
