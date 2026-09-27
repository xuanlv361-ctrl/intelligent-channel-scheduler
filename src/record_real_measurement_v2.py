"""Record one user-confirmed manual UAT result; never performs a network call."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN_PATH = ROOT / "output" / "real_measurement_plan_v2.csv"
DEFAULT_PAYLOAD_PATH = ROOT / "output" / "real_measurement_payloads_v2.json"
DEFAULT_RESULTS_PATH = ROOT / "data" / "real_channel_measurements_v2.csv"
DEFAULT_BACKUP_DIR = ROOT / "output" / "backups"
RESULTS = {"success", "failure", "blocked_capability", "aborted_stop_rule"}
BOOLEAN_TEXT = {"TRUE", "FALSE"}
SENSITIVE_TERMS = (
    "api_key", "apikey", "authorization", "bearer ", "secret_key", "access_token"
)
PLAN_COPY_FIELDS = (
    "plan_id", "campaign_version", "session_id", "planned_order", "channel_id",
    "channel_name", "request_profile_id", "requested_model",
)
RESULTS_COLUMNS = [
    "measurement_id", *PLAN_COPY_FIELDS, "planned_stream",
    "planned_prompt_version", "planned_max_tokens", "measured_at", "result",
    "latency_ms", "ttft_ms", "input_tokens", "output_tokens", "total_tokens",
    "cost_cny", "http_status", "actual_model", "request_id", "finish_reason",
    "sse_complete", "done_received", "error_category", "evidence_test_path",
    "evidence_log_path", "notes", "execution_attempted", "source_type",
    "is_mock", "validation_status", "missing_evidence_fields",
]
METADATA_COMPARE_FIELDS = (
    *PLAN_COPY_FIELDS, "planned_stream", "planned_prompt_version",
    "planned_max_tokens",
)


class MeasurementRecordError(ValueError):
    pass


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def find_unique(rows: list[dict[str, Any]], field: str, value: str) -> dict[str, Any]:
    matches = [row for row in rows if str(row.get(field, "")) == value]
    if len(matches) != 1:
        label = "unknown" if not matches else "duplicate"
        raise MeasurementRecordError(f"{label} {field}: {value}")
    return matches[0]


def deterministic_measurement_id(plan_id: str) -> str:
    digest = hashlib.sha256(f"manual-uat-v2:{plan_id}".encode("utf-8")).hexdigest()[:16]
    return f"MEAS-V2-{digest.upper()}"


def _optional_number(value: Any, field: str, *, integer: bool = False) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        parsed = int(str(value)) if integer else float(str(value))
    except (TypeError, ValueError) as exc:
        raise MeasurementRecordError(f"{field} must be numeric") from exc
    if parsed < 0:
        raise MeasurementRecordError(f"{field} must be non-negative")
    return str(parsed)


def _optional_boolean(value: Any, field: str) -> str:
    if value is None or str(value).strip() == "":
        return ""
    normalized = str(value).strip().upper()
    if normalized not in BOOLEAN_TEXT:
        raise MeasurementRecordError(f"{field} must be TRUE or FALSE")
    return normalized


def _resolve_evidence(value: str, base: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else base / path


def _planned_metadata(
    plan: Mapping[str, str], payload: Mapping[str, Any], prompt_version: str
) -> dict[str, str]:
    if payload.get("plan_id") != plan.get("plan_id"):
        raise MeasurementRecordError("plan and payload plan_id mismatch")
    for field in ("session_id", "channel_id", "channel_name", "request_profile_id", "requested_model"):
        if str(payload.get(field, "")) != str(plan.get(field, "")):
            raise MeasurementRecordError(f"plan and payload metadata mismatch: {field}")
    payload_stream = "TRUE" if payload.get("stream") is True else "FALSE"
    if payload_stream != plan.get("stream"):
        raise MeasurementRecordError("plan and payload stream mismatch")
    return {
        **{field: str(plan[field]) for field in PLAN_COPY_FIELDS},
        "planned_stream": payload_stream,
        "planned_prompt_version": prompt_version,
        "planned_max_tokens": str(payload["max_tokens"]),
    }


def validate_actual_fields(
    planned: Mapping[str, str],
    actual: Mapping[str, Any],
    *,
    evidence_base: Path,
) -> dict[str, str]:
    result = str(actual.get("result") or "")
    if result not in RESULTS:
        raise MeasurementRecordError(f"unsupported result: {result}")
    values = {
        "measured_at": str(actual.get("measured_at") or ""),
        "result": result,
        "latency_ms": _optional_number(actual.get("latency_ms"), "latency_ms"),
        "ttft_ms": _optional_number(actual.get("ttft_ms"), "ttft_ms"),
        "input_tokens": _optional_number(actual.get("input_tokens"), "input_tokens", integer=True),
        "output_tokens": _optional_number(actual.get("output_tokens"), "output_tokens", integer=True),
        "total_tokens": _optional_number(actual.get("total_tokens"), "total_tokens", integer=True),
        "cost_cny": _optional_number(actual.get("cost_cny"), "cost_cny"),
        "http_status": str(actual.get("http_status") or ""),
        "actual_model": str(actual.get("actual_model") or ""),
        "request_id": str(actual.get("request_id") or ""),
        "finish_reason": str(actual.get("finish_reason") or ""),
        "sse_complete": _optional_boolean(actual.get("sse_complete"), "sse_complete"),
        "done_received": _optional_boolean(actual.get("done_received"), "done_received"),
        "error_category": str(actual.get("error_category") or ""),
        "evidence_test_path": str(actual.get("evidence_test_path") or ""),
        "evidence_log_path": str(actual.get("evidence_log_path") or ""),
        "notes": str(actual.get("notes") or ""),
    }
    if not values["measured_at"]:
        raise MeasurementRecordError("measured_at is required; it must be the observed time")
    is_stream = planned["planned_stream"] == "TRUE"
    if not is_stream and any((values["ttft_ms"], values["sse_complete"], values["done_received"])):
        raise MeasurementRecordError("non-stream plans cannot record TTFT or SSE fields")
    if is_stream and result == "success":
        if not values["ttft_ms"]:
            raise MeasurementRecordError("streaming success requires observed ttft_ms")
        if values["sse_complete"] == "" or values["done_received"] == "":
            raise MeasurementRecordError("streaming success requires SSE completeness observations")
    if result == "success" and (not values["latency_ms"] or float(values["latency_ms"]) <= 0):
        raise MeasurementRecordError("success requires positive latency_ms")
    if result == "failure" and not (values["error_category"] or values["notes"]):
        raise MeasurementRecordError("failure requires error_category or notes")
    serialized = json.dumps(values, ensure_ascii=False).lower()
    if any(term in serialized for term in SENSITIVE_TERMS):
        raise MeasurementRecordError("record fields contain prohibited credential material")

    flags: list[str] = []
    if values["total_tokens"] and values["input_tokens"] and values["output_tokens"]:
        expected = int(values["input_tokens"]) + int(values["output_tokens"])
        if int(values["total_tokens"]) != expected:
            flags.append("usage_mismatch")
    if values["actual_model"] and values["actual_model"] != planned["requested_model"]:
        flags.append("model_mismatch")
    if is_stream and (
        values["sse_complete"] == "FALSE" or values["done_received"] == "FALSE"
    ):
        flags.append("protocol_incomplete")
    if values["error_category"] in {"channel_mismatch", "evidence_conflict"}:
        flags.append("evidence_conflict")

    missing = []
    for field in ("evidence_test_path", "evidence_log_path"):
        value = values[field]
        if not value or not _resolve_evidence(value, evidence_base).is_file():
            missing.append(field)
    if missing:
        flags.append("missing_evidence")
    values["missing_evidence_fields"] = "|".join(missing)
    values["validation_status"] = "|".join(dict.fromkeys(flags)) if flags else "validated"
    values["execution_attempted"] = "FALSE" if result == "blocked_capability" else "TRUE"
    values["source_type"] = "measured_manual_uat"
    values["is_mock"] = "FALSE"
    return values


def build_record(
    plan: Mapping[str, str],
    payload: Mapping[str, Any],
    prompt_version: str,
    actual: Mapping[str, Any],
    *,
    evidence_base: Path,
) -> dict[str, str]:
    planned = _planned_metadata(plan, payload, prompt_version)
    observed = validate_actual_fields(planned, actual, evidence_base=evidence_base)
    return {
        "measurement_id": deterministic_measurement_id(planned["plan_id"]),
        **planned,
        **observed,
    }


def validate_existing_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    if set(rows[0]) != set(RESULTS_COLUMNS):
        raise MeasurementRecordError("existing result file schema mismatch")
    plan_ids = [row["plan_id"] for row in rows]
    measurement_ids = [row["measurement_id"] for row in rows]
    if len(plan_ids) != len(set(plan_ids)) or len(measurement_ids) != len(set(measurement_ids)):
        raise MeasurementRecordError("existing result file contains duplicate identifiers")


def backup_for_amend(path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    candidate = backup_dir / f"{path.stem}_before_amend_{timestamp}{path.suffix}"
    suffix = 1
    while candidate.exists():
        candidate = backup_dir / f"{path.stem}_before_amend_{timestamp}_{suffix:02d}{path.suffix}"
        suffix += 1
    shutil.copyfile(path, candidate)
    return candidate


def atomic_write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULTS_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def record_result(
    *,
    plan_path: Path,
    payload_path: Path,
    profiles_path: Path,
    results_path: Path,
    backup_dir: Path,
    plan_id: str,
    actual: Mapping[str, Any],
    amend: bool = False,
    dry_run: bool = False,
    evidence_base: Path = ROOT,
) -> dict[str, Any]:
    plan = find_unique(read_csv(plan_path), "plan_id", plan_id)
    payloads = read_json(payload_path)
    payload = find_unique(payloads, "plan_id", plan_id)
    profiles = read_csv(profiles_path)
    profile = find_unique(profiles, "request_profile_id", plan["request_profile_id"])
    record = build_record(
        plan, payload, profile["prompt_version"], actual, evidence_base=evidence_base
    )
    existing = read_csv(results_path)
    validate_existing_rows(existing)
    matches = [index for index, row in enumerate(existing) if row["plan_id"] == plan_id]
    if matches and not amend:
        raise MeasurementRecordError(f"plan_id already recorded: {plan_id}")
    if amend and not matches:
        raise MeasurementRecordError(f"cannot amend unrecorded plan_id: {plan_id}")
    updated = list(existing)
    if matches:
        updated[matches[0]] = record
    else:
        updated.append(record)
    order = {row["plan_id"]: index for index, row in enumerate(read_csv(plan_path))}
    updated.sort(key=lambda row: order[row["plan_id"]])
    backup_path = None
    if not dry_run:
        if amend:
            backup_path = backup_for_amend(results_path, backup_dir)
        atomic_write_csv(results_path, updated)
    return {
        "status": record["validation_status"],
        "written": not dry_run,
        "dry_run": dry_run,
        "plan_id": plan_id,
        "measurement_id": record["measurement_id"],
        "backup_path": str(backup_path) if backup_path else None,
        "record": record,
    }


def display_plan(plan: Mapping[str, str], payload: Mapping[str, Any]) -> str:
    shown = {
        "plan_id": plan["plan_id"], "session_id": plan["session_id"],
        "planned_order": plan["planned_order"], "channel_id": plan["channel_id"],
        "channel_name": plan["channel_name"],
        "request_profile_id": plan["request_profile_id"],
        "requested_model": plan["requested_model"], "stream": payload["stream"],
        "prompt": payload["messages"][0]["content"], "max_tokens": payload["max_tokens"],
    }
    return json.dumps(shown, ensure_ascii=False, indent=2)


def _ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _interactive_actual() -> dict[str, str]:
    return {
        "measured_at": _ask("measured_at"),
        "result": _ask("result (success/failure/blocked_capability/aborted_stop_rule)"),
        "latency_ms": _ask("latency_ms"), "ttft_ms": _ask("ttft_ms"),
        "input_tokens": _ask("input_tokens"), "output_tokens": _ask("output_tokens"),
        "total_tokens": _ask("total_tokens"), "cost_cny": _ask("cost_cny"),
        "http_status": _ask("http_status"), "actual_model": _ask("actual_model"),
        "request_id": _ask("request_id"), "finish_reason": _ask("finish_reason"),
        "sse_complete": _ask("sse_complete (TRUE/FALSE)"),
        "done_received": _ask("done_received (TRUE/FALSE)"),
        "error_category": _ask("error_category"),
        "evidence_test_path": _ask("evidence_test_path"),
        "evidence_log_path": _ask("evidence_log_path"), "notes": _ask("notes"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--measured-at")
    parser.add_argument("--result", choices=sorted(RESULTS))
    for name in (
        "latency-ms", "ttft-ms", "input-tokens", "output-tokens", "total-tokens",
        "cost-cny", "http-status", "actual-model", "request-id", "finish-reason",
        "sse-complete", "done-received", "error-category", "notes",
    ):
        parser.add_argument(f"--{name}")
    parser.add_argument("--evidence-test", dest="evidence_test_path")
    parser.add_argument("--evidence-log", dest="evidence_log_path")
    parser.add_argument("--amend", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-save", action="store_true")
    parser.add_argument("--plan-file", type=Path, default=DEFAULT_PLAN_PATH)
    parser.add_argument("--payload-file", type=Path, default=DEFAULT_PAYLOAD_PATH)
    parser.add_argument("--profiles-file", type=Path, default=ROOT / "data" / "request_profiles_v2.csv")
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--evidence-base", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        plan = find_unique(read_csv(args.plan_file), "plan_id", args.plan_id)
        payload = find_unique(read_json(args.payload_file), "plan_id", args.plan_id)
        print(display_plan(plan, payload))
        supplied = args.result is not None
        if supplied:
            actual = {
                key: value for key, value in vars(args).items()
                if key in {
                    "measured_at", "result", "latency_ms", "ttft_ms", "input_tokens",
                    "output_tokens", "total_tokens", "cost_cny", "http_status",
                    "actual_model", "request_id", "finish_reason", "sse_complete",
                    "done_received", "error_category", "evidence_test_path",
                    "evidence_log_path", "notes",
                }
            }
        else:
            actual = _interactive_actual()
        if not args.dry_run and not args.confirm_save:
            if _ask("确认以上内容来自真实人工UAT证据后保存？输入YES") != "YES":
                raise MeasurementRecordError("save cancelled; explicit user confirmation required")
        output = record_result(
            plan_path=args.plan_file, payload_path=args.payload_file,
            profiles_path=args.profiles_file, results_path=args.results_file,
            backup_dir=args.backup_dir, plan_id=args.plan_id, actual=actual,
            amend=args.amend, dry_run=args.dry_run, evidence_base=args.evidence_base,
        )
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps(
            {"status": "error", "error_type": type(exc).__name__, "error": str(exc)},
            ensure_ascii=False,
        ), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
