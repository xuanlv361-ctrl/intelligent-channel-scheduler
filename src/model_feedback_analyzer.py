"""Descriptive offline model feedback analysis; never updates routing weights."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from feedback_store import DEFAULT_STORE, aggregate_feedback, query_feedback


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "output" / "model_feedback_analysis_v1.json"


def analyze_model_performance(
    feedback_records: list[Mapping[str, Any]],
    *,
    minimum_sample_count: int = 5,
) -> list[dict[str, Any]]:
    if minimum_sample_count <= 0:
        raise ValueError("minimum_sample_count must be positive")
    results = []
    for row in aggregate_feedback(feedback_records):
        if row["sample_count"] < minimum_sample_count:
            recommendation = "insufficient_sample_warning"
        elif row["success_rate"] >= 0.9 and row["average_rating"] >= 4:
            recommendation = "positive_offline_feedback_signal"
        else:
            recommendation = "monitor_offline_feedback"
        results.append({**row, "recommendation": recommendation})
    return results


def build_analysis(path: Path = DEFAULT_STORE) -> dict[str, Any]:
    records = query_feedback(path=path)
    return {
        "analysis_type": "offline_feedback_analysis_only",
        "record_count": len(records),
        "results": analyze_model_performance(records),
        "automatic_model_weight_update": False,
        "real_api_calls_performed": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feedback", type=Path, default=DEFAULT_STORE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    output = build_analysis(args.feedback)
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "status": "ok", "record_count": output["record_count"],
        "dry_run": args.dry_run, "real_api_calls_performed": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
