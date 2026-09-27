"""Validate observational unified-routing progress without changing evidence."""

from __future__ import annotations
import argparse, json, sys
from collections import Counter
from pathlib import Path
from typing import Any
from record_unified_routing_measurement_v3 import DEFAULT_PLAN_PATH, DEFAULT_RESULTS_PATH, RESULTS_COLUMNS, read_csv

ROOT = Path(__file__).resolve().parents[1]

def _exists(value: str, base: Path) -> bool:
    path = Path(value)
    return bool(value) and (path if path.is_absolute() else base / path).is_file()

def _counts(plans: list[dict[str, str]], completed: set[str], field: str) -> dict[str, dict[str, int]]:
    return {value: {"planned": len(rows), "completed": sum(row["plan_id"] in completed for row in rows)}
            for value in sorted({row[field] for row in plans})
            for rows in [[row for row in plans if row[field] == value]]}

def build_progress(plans: list[dict[str, str]], results: list[dict[str, str]],
                   evidence_base: Path = ROOT, results_file_exists: bool = False) -> dict[str, Any]:
    plan_ids = {row["plan_id"] for row in plans}
    plan_by_id = {row["plan_id"]: row for row in plans}
    valid = [row for row in results if row.get("plan_id") in plan_ids]
    completed = {row["plan_id"] for row in valid}
    duplicate_plan = sum(n - 1 for n in Counter(row.get("plan_id", "") for row in results).values() if n > 1)
    duplicate_measurement = sum(n - 1 for n in Counter(row.get("measurement_id", "") for row in results).values() if n > 1)
    invalid_paths = sum(not _exists(row.get("apifox_evidence_path", ""), evidence_base) or
                        (bool(row.get("backend_log_evidence_path")) and not _exists(row["backend_log_evidence_path"], evidence_base))
                        for row in valid)
    routing = Counter((row.get("actual_channel_id") or row.get("actual_channel_name")) for row in valid
                      if row.get("actual_channel_id") or row.get("actual_channel_name"))
    structural = duplicate_plan + duplicate_measurement + len(results) - len(valid)
    missing_channel = sum(not row.get("actual_channel_id") and not row.get("actual_channel_name") for row in valid)
    report = {
        "completed": len(completed), "planned": len(plans), "pending": len(plans) - len(completed),
        "completed_by_session": _counts(plans, completed, "session_id"),
        "completed_by_profile": _counts(plans, completed, "request_profile_id"),
        "completed_by_round": _counts(plans, completed, "round_id"),
        "actual_observed_routing_counts_by_channel": dict(sorted(routing.items())),
        "success_count": sum(row.get("result") == "success" for row in valid),
        "failure_count": sum(row.get("result") == "failure" for row in valid),
        "missing_backend_channel_evidence": missing_channel + sum(
            bool(row.get("actual_channel_id") or row.get("actual_channel_name")) and
            not _exists(row.get("backend_log_evidence_path", ""), evidence_base) for row in valid),
        "missing_http_status": sum(not row.get("http_status") for row in valid),
        "missing_actual_model": sum(not row.get("actual_model") for row in valid),
        "missing_request_id": sum(not row.get("request_id") for row in valid),
        "missing_ttft_for_p04": sum((row.get("request_profile_id") or plan_by_id[row["plan_id"]].get("request_profile_id")) == "P04" and not row.get("ttft_ms") for row in valid),
        "incomplete_sse_records": sum((row.get("request_profile_id") or plan_by_id[row["plan_id"]].get("request_profile_id")) == "P04" and
            (row.get("sse_complete") != "TRUE" or row.get("done_received") != "TRUE") for row in valid),
        "duplicate_plan_id_count": duplicate_plan,
        "duplicate_measurement_id_count": duplicate_measurement,
        "result_not_in_plan_count": len(results) - len(valid),
        "invalid_evidence_paths": invalid_paths,
        "results_file_exists": results_file_exists,
        "equal_channel_counts_required": False,
    }
    report["readiness_for_unified_routing_analysis"] = bool(valid) and structural == 0 and invalid_paths == 0
    return report

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-file", type=Path, default=DEFAULT_PLAN_PATH)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--evidence-base", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        plans, results = read_csv(args.plan_file), read_csv(args.results_file)
        if results and list(results[0]) != RESULTS_COLUMNS: raise ValueError("result CSV schema mismatch")
        report = build_progress(plans, results, args.evidence_base, args.results_file.exists())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return int(any(report[key] for key in ("duplicate_plan_id_count", "duplicate_measurement_id_count", "result_not_in_plan_count")))
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr); return 1

if __name__ == "__main__": raise SystemExit(main())
