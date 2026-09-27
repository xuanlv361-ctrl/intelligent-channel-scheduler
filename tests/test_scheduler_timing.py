from datetime import datetime, timezone
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scheduler_timing import SchedulerTimingError, SchedulerTimingRecorder
from decision_logger import DecisionLogger
from scheduler import Scheduler


class Ticks:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


def test_session_records_only_explicit_scheduler_stages(tmp_path):
    recorder = SchedulerTimingRecorder(tmp_path / "timing.sqlite3")
    ticks = Ticks([10.0, 10.1, 10.12, 10.2])
    session = recorder.start_session(
        decision_id="D1", run_id="R1", monotonic=ticks
    )
    with session.measure("candidate_loading"):
        pass
    session.finish()
    result = recorder.summary()
    candidate = next(x for x in result["stages"] if x["stage"] == "candidate_loading")
    total = next(x for x in result["stages"] if x["stage"] == "scheduler_total")
    assert candidate["p50_ms"] == pytest.approx(20.0)
    assert total["p50_ms"] == pytest.approx(200.0)
    assert result["provider_latency_included"] is False


def test_percentiles_are_ordered_and_separate_by_stage(tmp_path):
    recorder = SchedulerTimingRecorder(tmp_path / "timing.sqlite3")
    for index, duration in enumerate([1, 2, 3, 10, 20]):
        recorder.record(
            decision_id=f"D{index}", run_id="R", stage="scheduler_total",
            duration_ms=duration,
        )
    result = recorder.summary()
    total = next(x for x in result["stages"] if x["stage"] == "scheduler_total")
    assert total["p50_ms"] <= total["p95_ms"] <= total["p99_ms"]
    assert total["sample_count"] == 5


def test_recording_is_idempotent_and_conflicts_fail_closed(tmp_path):
    recorder = SchedulerTimingRecorder(tmp_path / "timing.sqlite3")
    recorder.record(decision_id="D", run_id="R", stage="scheduler_total", duration_ms=1.5)
    recorder.record(decision_id="D", run_id="R", stage="scheduler_total", duration_ms=1.5)
    with pytest.raises(SchedulerTimingError, match="idempotency_conflict"):
        recorder.record(decision_id="D", run_id="R", stage="scheduler_total", duration_ms=2.0)


def test_invalid_stage_non_finite_and_invalid_window_are_rejected(tmp_path):
    recorder = SchedulerTimingRecorder(tmp_path / "timing.sqlite3")
    with pytest.raises(SchedulerTimingError, match="unknown"):
        recorder.record(decision_id="D", run_id="R", stage="provider_latency", duration_ms=1)
    with pytest.raises(SchedulerTimingError, match="duration"):
        recorder.record(decision_id="D", run_id="R", stage="scheduler_total", duration_ms=float("nan"))
    with pytest.raises(SchedulerTimingError, match="window"):
        recorder.summary(window_minutes=0)


def test_empty_summary_is_truthful(tmp_path):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    recorder = SchedulerTimingRecorder(tmp_path / "timing.sqlite3", clock=lambda: now)
    result = recorder.summary()
    assert result["status"] == "never_measured"
    assert result["sample_count"] == 0


def test_scheduler_runtime_emits_scheduler_only_stage_samples(tmp_path):
    recorder = SchedulerTimingRecorder(tmp_path / "timing.sqlite3")
    scheduler = Scheduler(
        logger=DecisionLogger(tmp_path / "decisions.jsonl"),
        runtime_log_enabled=False,
        timing_recorder=recorder,
    )
    result = scheduler.route({
        "request_id": "TIMING-1",
        "requested_model": "deepseek-v4-flash",
        "stream": False,
        "input_tokens": 10,
        "output_tokens": 10,
        "currency": "CNY",
        "strategy": "fastest_first",
        "mode": "simulation",
    })
    summary = recorder.summary()
    assert result["scheduler_timing"]["provider_latency_included"] is False
    assert summary["sample_count"] >= 4
    total = next(x for x in summary["stages"] if x["stage"] == "scheduler_total")
    assert total["sample_count"] == 1
