"""Record one human-confirmed Apifox result; this module never uses a network."""

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
DEFAULT_PLAN_PATH = ROOT / "output" / "unified_routing_plan_v3.csv"
DEFAULT_RESULTS_PATH = ROOT / "data" / "unified_routing_measurements_v3.csv"
DEFAULT_BACKUP_DIR = ROOT / "output" / "backups"
RESULTS = {"success", "failure"}
RESULTS_COLUMNS = [
    "measurement_id", "plan_id", "campaign_version", "session_id", "round_id",
    "request_profile_id", "requested_model", "actual_model", "measured_at",
    "stream_requested", "stream_observed", "max_tokens", "result", "http_status",
    "request_id", "total_latency_ms", "ttft_ms", "input_tokens", "output_tokens",
    "total_tokens", "cost_cny", "finish_reason", "sse_complete", "done_received",
    "error_category", "actual_channel_id", "actual_channel_name",
    "actual_channel_evidence_status", "apifox_evidence_path",
    "backend_log_evidence_path", "source_type", "is_mock", "notes",
]
SENSITIVE = ("api_key", "apikey", "authorization", "bearer ", "cookie", "secret")


class MeasurementRecordError(ValueError):
    pass


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def find_unique(rows: list[dict[str, str]], field: str, value: str) -> dict[str, str]:
    matches = [row for row in rows if row.get(field) == value]
    if len(matches) != 1:
        raise MeasurementRecordError(f"{'unknown' if not matches else 'duplicate'} {field}: {value}")
    return matches[0]


def deterministic_measurement_id(plan_id: str) -> str:
    digest = hashlib.sha256(f"unified-routing-v3:{plan_id}".encode()).hexdigest()[:16]
    return f"MEAS-UR-V3-{digest.upper()}"


def _number(value: Any, field: str, integer: bool = False) -> str:
    if value is None or str(value).strip() == "":
        return ""
    try:
        parsed = int(str(value)) if integer else float(str(value))
    except ValueError as exc:
        raise MeasurementRecordError(f"{field} must be numeric") from exc
    if parsed < 0:
        raise MeasurementRecordError(f"{field} must be non-negative")
    return str(parsed)


def _boolean(value: Any, field: str) -> str:
    if value is None or str(value).strip() == "":
        return ""
    text = str(value).upper()
    if text not in {"TRUE", "FALSE"}:
        raise MeasurementRecordError(f"{field} must be TRUE or FALSE")
    return text


def _evidence_exists(value: str, base: Path) -> bool:
    path = Path(value)
    return bool(value) and (path if path.is_absolute() else base / path).is_file()


def build_record(plan: Mapping[str, str], actual: Mapping[str, Any], evidence_base: Path) -> dict[str, str]:
    result = str(actual.get("result") or "")
    if result not in RESULTS:
        raise MeasurementRecordError("result must be success or failure")
    stream_requested = plan["stream"]
    record = {
        "measurement_id": str(actual.get("measurement_id") or deterministic_measurement_id(plan["plan_id"])),
        "plan_id": plan["plan_id"], "campaign_version": plan["campaign_version"],
        "session_id": plan["session_id"], "round_id": plan["round_id"],
        "request_profile_id": plan["request_profile_id"],
        "requested_model": plan["requested_model"],
        "actual_model": str(actual.get("actual_model") or ""),
        "measured_at": str(actual.get("measured_at") or ""),
        "stream_requested": stream_requested,
        "stream_observed": _boolean(actual.get("stream_observed"), "stream_observed"),
        "max_tokens": plan["max_tokens"], "result": result,
        "http_status": str(actual.get("http_status") or ""),
        "request_id": str(actual.get("request_id") or ""),
        "total_latency_ms": _number(actual.get("total_latency_ms"), "total_latency_ms"),
        "ttft_ms": _number(actual.get("ttft_ms"), "ttft_ms"),
        "input_tokens": _number(actual.get("input_tokens"), "input_tokens", True),
        "output_tokens": _number(actual.get("output_tokens"), "output_tokens", True),
        "total_tokens": _number(actual.get("total_tokens"), "total_tokens", True),
        "cost_cny": _number(actual.get("cost_cny"), "cost_cny"),
        "finish_reason": str(actual.get("finish_reason") or ""),
        "sse_complete": _boolean(actual.get("sse_complete"), "sse_complete"),
        "done_received": _boolean(actual.get("done_received"), "done_received"),
        "error_category": str(actual.get("error_category") or ""),
        "actual_channel_id": str(actual.get("actual_channel_id") or ""),
        "actual_channel_name": str(actual.get("actual_channel_name") or ""),
        "actual_channel_evidence_status": str(actual.get("actual_channel_evidence_status") or ""),
        "apifox_evidence_path": str(actual.get("apifox_evidence_path") or ""),
        "backend_log_evidence_path": str(actual.get("backend_log_evidence_path") or ""),
        "source_type": "measured_unified_uat", "is_mock": "FALSE",
        "notes": str(actual.get("notes") or ""),
    }
    if not record["measured_at"]:
        raise MeasurementRecordError("measured_at is required")
    if not _evidence_exists(record["apifox_evidence_path"], evidence_base):
        raise MeasurementRecordError("valid apifox_evidence_path is required")
    has_channel = bool(record["actual_channel_id"] or record["actual_channel_name"])
    if has_channel:
        if not _evidence_exists(record["backend_log_evidence_path"], evidence_base):
            raise MeasurementRecordError("actual channel requires matching backend log evidence")
        if record["actual_channel_evidence_status"] != "backend_log_confirmed":
            raise MeasurementRecordError("actual channel evidence status must be backend_log_confirmed")
    elif record["actual_channel_evidence_status"] == "backend_log_confirmed":
        raise MeasurementRecordError("confirmed channel evidence cannot have blank channel")
    if stream_requested == "FALSE" and record["ttft_ms"]:
        raise MeasurementRecordError("non-stream requests cannot record TTFT")
    if stream_requested == "FALSE" and any(record[key] for key in ("sse_complete", "done_received")):
        raise MeasurementRecordError("non-stream requests cannot record SSE fields")
    if record["total_tokens"] and record["input_tokens"] and record["output_tokens"]:
        if int(record["total_tokens"]) != int(record["input_tokens"]) + int(record["output_tokens"]):
            raise MeasurementRecordError("total_tokens must equal input_tokens + output_tokens")
    if any(term in json.dumps(record, ensure_ascii=False).lower() for term in SENSITIVE):
        raise MeasurementRecordError("record contains credential material")
    return record


def validate_existing(rows: list[dict[str, str]]) -> None:
    if rows and list(rows[0]) != RESULTS_COLUMNS:
        raise MeasurementRecordError("existing result file schema mismatch")
    for key in ("plan_id", "measurement_id"):
        values = [row[key] for row in rows]
        if len(values) != len(set(values)):
            raise MeasurementRecordError(f"duplicate {key} in existing results")


def atomic_write(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RESULTS_COLUMNS, lineterminator="\n")
            writer.writeheader(); writer.writerows(rows); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def backup(path: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f%z")
    target = directory / f"{path.stem}_before_amend_{stamp}{path.suffix}"
    shutil.copyfile(path, target)
    return target


def record_result(*, plan_path: Path, results_path: Path, backup_dir: Path, plan_id: str,
                  actual: Mapping[str, Any], evidence_base: Path = ROOT,
                  amend: bool = False, dry_run: bool = False) -> dict[str, Any]:
    plans = read_csv(plan_path)
    plan = find_unique(plans, "plan_id", plan_id)
    existing = read_csv(results_path)
    validate_existing(existing)
    record = build_record(plan, actual, evidence_base)
    if any(row["measurement_id"] == record["measurement_id"] and row["plan_id"] != plan_id for row in existing):
        raise MeasurementRecordError("duplicate measurement_id")
    indices = [i for i, row in enumerate(existing) if row["plan_id"] == plan_id]
    if indices and not amend:
        raise MeasurementRecordError(f"duplicate plan_id: {plan_id}")
    if amend and not indices:
        raise MeasurementRecordError(f"cannot amend unrecorded plan_id: {plan_id}")
    updated = list(existing)
    if indices: updated[indices[0]] = record
    else: updated.append(record)
    order = {row["plan_id"]: i for i, row in enumerate(plans)}
    updated.sort(key=lambda row: order[row["plan_id"]])
    backup_path = None
    if not dry_run:
        if amend: backup_path = backup(results_path, backup_dir)
        atomic_write(results_path, updated)
    return {"written": not dry_run, "dry_run": dry_run, "record": record,
            "backup_path": str(backup_path) if backup_path else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", required=True); parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--amend", action="store_true"); parser.add_argument("--confirm-save", action="store_true")
    parser.add_argument("--actual-json", type=Path)
    parser.add_argument("--plan-file", type=Path, default=DEFAULT_PLAN_PATH)
    parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_PATH)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--evidence-base", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        plan = find_unique(read_csv(args.plan_file), "plan_id", args.plan_id)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        if args.actual_json is None:
            if args.dry_run:
                print(json.dumps({"dry_run": True, "written": False, "message": "plan preview only"}))
                return 0
            raise MeasurementRecordError("--actual-json is required for a confirmed result")
        actual = json.loads(args.actual_json.read_text(encoding="utf-8-sig"))
        if not args.dry_run and not args.confirm_save:
            raise MeasurementRecordError("--confirm-save is required to write genuine evidence")
        print(json.dumps(record_result(plan_path=args.plan_file, results_path=args.results_file,
              backup_dir=args.backup_dir, plan_id=args.plan_id, actual=actual,
              evidence_base=args.evidence_base, amend=args.amend, dry_run=args.dry_run),
              ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
