"""Validate manual UAT progress without creating or changing any result row."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from record_real_measurement_v2 import (
    DEFAULT_PLAN_PATH,
    DEFAULT_PAYLOAD_PATH,
    DEFAULT_RESULTS_PATH,
    METADATA_COMPARE_FIELDS,
    RESULTS_COLUMNS,
    read_csv,
    read_json,
)


ROOT = Path(__file__).resolve().parents[1]
AUTH_ERROR_CATEGORIES = {
    "authentication_error", "authorization_error", "permission_error",
    "auth_error", "forbidden",
}
COST_ERROR_CATEGORIES = {"cost_anomaly", "abnormal_cost"}
CHANNEL_CONFLICT_CATEGORIES = {"channel_mismatch", "evidence_conflict"}


def _path_exists(value: str, evidence_base: Path) -> bool:
    if not value:
        return False
    path = Path(value)
    return (path if path.is_absolute() else evidence_base / path).is_file()


def evaluate_stop_conditions(
    plan_rows: list[dict[str, str]], result_rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    order = {row["plan_id"]: index for index, row in enumerate(plan_rows)}
    valid = [row for row in result_rows if row.get("plan_id") in order]
    valid.sort(key=lambda row: order[row["plan_id"]])
    events: list[dict[str, str]] = []
    for row in valid:
        category = row.get("error_category", "")
        if category in AUTH_ERROR_CATEGORIES:
            events.append({
                "scope": "channel", "scope_id": row["channel_id"],
                "plan_id": row["plan_id"], "rule": "authentication_or_permission_error",
            })
        if category in COST_ERROR_CATEGORIES:
            events.append({
                "scope": "session", "scope_id": row["session_id"],
                "plan_id": row["plan_id"], "rule": "abnormal_cost",
            })
        if category in CHANNEL_CONFLICT_CATEGORIES or "evidence_conflict" in row.get("validation_status", ""):
            events.append({
                "scope": "record", "scope_id": row["plan_id"],
                "plan_id": row["plan_id"], "rule": "planned_channel_evidence_conflict",
            })
        if "protocol_incomplete" in row.get("validation_status", ""):
            events.append({
                "scope": "record", "scope_id": row["plan_id"],
                "plan_id": row["plan_id"], "rule": "stream_protocol_incomplete",
            })

    by_channel: dict[str, list[dict[str, str]]] = {}
    for row in valid:
        by_channel.setdefault(row["channel_id"], []).append(row)
    for channel_id, rows in by_channel.items():
        consecutive = 0
        for row in rows:
            status = row.get("http_status", "")
            is_rate_or_server_error = status == "429" or (
                status.isdigit() and 500 <= int(status) <= 599
            )
            consecutive = consecutive + 1 if is_rate_or_server_error else 0
            if consecutive == 3:
                events.append({
                    "scope": "channel", "scope_id": channel_id,
                    "plan_id": row["plan_id"], "rule": "three_consecutive_429_or_5xx",
                })
    unique: list[dict[str, str]] = []
    seen = set()
    for event in events:
        key = tuple(event.items())
        if key not in seen:
            unique.append(event)
            seen.add(key)
    return unique


def _breakdown(
    plan_rows: list[dict[str, str]],
    result_by_plan: dict[str, dict[str, str]],
    field: str,
) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for value in sorted({row[field] for row in plan_rows}):
        plans = [row for row in plan_rows if row[field] == value]
        results = [
            result_by_plan[row["plan_id"]]
            for row in plans if row["plan_id"] in result_by_plan
        ]
        output[value] = {
            "planned": len(plans),
            "recorded": len(results),
            "executed": sum(row.get("execution_attempted") == "TRUE" for row in results),
            "success": sum(row.get("result") == "success" for row in results),
            "failure": sum(row.get("result") == "failure" for row in results),
            "blocked_capability": sum(
                row.get("result") == "blocked_capability" for row in results
            ),
            "aborted_stop_rule": sum(
                row.get("result") == "aborted_stop_rule" for row in results
            ),
            "validated": sum(row.get("validation_status") == "validated" for row in results),
        }
    return output


def build_progress(
    plan_rows: list[dict[str, str]],
    result_rows: list[dict[str, str]],
    *,
    session_id: str | None = None,
    evidence_base: Path = ROOT,
    results_file_exists: bool = False,
    payload_rows: list[dict[str, Any]] | None = None,
    profile_rows: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    plan_by_id = {row["plan_id"]: row for row in plan_rows}
    sessions = {row["session_id"] for row in plan_rows}
    if session_id and session_id not in sessions:
        raise ValueError(f"unknown session_id: {session_id}")
    scope_plan = [
        row for row in plan_rows if session_id is None or row["session_id"] == session_id
    ]
    scope_ids = {row["plan_id"] for row in scope_plan}
    scoped_results = [row for row in result_rows if row.get("plan_id") in scope_ids]
    all_ids = [row.get("plan_id", "") for row in result_rows]
    duplicates = sum(count - 1 for count in Counter(all_ids).values() if count > 1)
    orphans = [row for row in result_rows if row.get("plan_id") not in plan_by_id]
    payload_by_id = {
        str(row["plan_id"]): row for row in (payload_rows or [])
    }
    profile_by_id = {
        row["request_profile_id"]: row for row in (profile_rows or [])
    }
    mismatches = 0
    for result in result_rows:
        plan = plan_by_id.get(result.get("plan_id"))
        if not plan:
            continue
        expected = {
            **{field: plan[field] for field in METADATA_COMPARE_FIELDS if field in plan},
            "planned_stream": plan["stream"],
        }
        payload = payload_by_id.get(result.get("plan_id", ""))
        profile = profile_by_id.get(plan["request_profile_id"])
        if payload:
            expected["planned_max_tokens"] = str(payload["max_tokens"])
        if profile:
            expected["planned_prompt_version"] = profile["prompt_version"]
        if any(result.get(field, "") != value for field, value in expected.items()):
            mismatches += 1
    result_by_plan: dict[str, dict[str, str]] = {}
    for row in scoped_results:
        result_by_plan.setdefault(row["plan_id"], row)
    executed = sum(row.get("execution_attempted") == "TRUE" for row in scoped_results)
    validated = sum(row.get("validation_status") == "validated" for row in scoped_results)
    missing_evidence = sum(
        not (
            _path_exists(row.get("evidence_test_path", ""), evidence_base)
            and _path_exists(row.get("evidence_log_path", ""), evidence_base)
        )
        for row in scoped_results
    )
    stop_events = evaluate_stop_conditions(plan_rows, result_rows)
    if session_id:
        stop_events = [
            event for event in stop_events
            if event["plan_id"] in scope_ids
        ]
    return {
        "overall_plan_total": len(plan_rows),
        "scope_session_id": session_id or "all",
        "current_session_plan_count": len(scope_plan),
        "recorded_count": len(scoped_results),
        "executed_count": executed,
        "not_executed_count": len(scope_plan) - executed,
        "success_count": sum(row.get("result") == "success" for row in scoped_results),
        "failure_count": sum(row.get("result") == "failure" for row in scoped_results),
        "blocked_capability_count": sum(
            row.get("result") == "blocked_capability" for row in scoped_results
        ),
        "aborted_stop_rule_count": sum(
            row.get("result") == "aborted_stop_rule" for row in scoped_results
        ),
        "validated_count": validated,
        "missing_evidence_count": missing_evidence,
        "duplicate_plan_id_count": duplicates,
        "result_not_in_plan_count": len(orphans),
        "plan_field_mismatch_count": mismatches,
        "channels": _breakdown(scope_plan, result_by_plan, "channel_id"),
        "profiles": _breakdown(scope_plan, result_by_plan, "request_profile_id"),
        "sessions": _breakdown(scope_plan, result_by_plan, "session_id"),
        "stop_condition_triggered": bool(stop_events),
        "stop_condition_events": stop_events,
        "results_file_exists": results_file_exists,
    }


def validate_result_schema(rows: list[dict[str, str]]) -> None:
    if rows and set(rows[0]) != set(RESULTS_COLUMNS):
        raise ValueError("result CSV schema mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-file", type=Path, default=DEFAULT_PLAN_PATH)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--payload-file", type=Path, default=DEFAULT_PAYLOAD_PATH)
    parser.add_argument(
        "--profiles-file", type=Path, default=ROOT / "data" / "request_profiles_v2.csv"
    )
    parser.add_argument("--evidence-base", type=Path, default=ROOT)
    parser.add_argument("--session-id")
    args = parser.parse_args(argv)
    try:
        plans, results = read_csv(args.plan_file), read_csv(args.results_file)
        validate_result_schema(results)
        progress = build_progress(
            plans, results, session_id=args.session_id,
            evidence_base=args.evidence_base,
            results_file_exists=args.results_file.exists(),
            payload_rows=read_json(args.payload_file),
            profile_rows=read_csv(args.profiles_file),
        )
        print(json.dumps(progress, ensure_ascii=False, indent=2))
        structural_errors = (
            "duplicate_plan_id_count", "result_not_in_plan_count",
            "plan_field_mismatch_count",
        )
        return 1 if any(progress[key] for key in structural_errors) else 0
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        print(json.dumps(
            {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
            ensure_ascii=False,
        ), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
