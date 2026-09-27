"""Build a deterministic 60-record UAT measurement plan; execute nothing."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "real_measurement_campaign_v2.json"
PROFILES_PATH = ROOT / "data" / "request_profiles_v2.csv"
V1_MEASUREMENTS_PATH = ROOT / "data" / "real_channel_measurements_v1.csv"
DEFAULT_OUTPUT_DIR = ROOT / "output"
CSV_NAME = "real_measurement_plan_v2.csv"
JSON_NAME = "real_measurement_plan_summary_v2.json"
PLAN_COLUMNS = [
    "plan_id", "campaign_version", "session_id", "session_sequence", "day_label",
    "time_window", "channel_id", "channel_name", "request_profile_id",
    "requested_model", "stream", "planned_order", "rotation_direction",
    "record_type", "source_type", "execution_status", "actual_tested_at",
    "actual_model", "http_status", "request_id", "actual_result", "evidence_path", "notes",
]
ACTUAL_FIELDS = ("actual_tested_at", "actual_model", "http_status", "request_id", "actual_result", "evidence_path")


class PlanValidationError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def build_plan(config: dict[str, Any], profiles: list[dict[str, str]]) -> list[dict[str, str]]:
    profile_by_id = {row["request_profile_id"]: row for row in profiles}
    enabled = config["profile_order"]
    if any(profile_by_id[profile]["enabled_in_pilot"] != "TRUE" for profile in enabled):
        raise PlanValidationError("profile_order contains a disabled profile")
    channels = {row["channel_id"]: row for row in config["channels"]}
    plans: list[dict[str, str]] = []
    sequence = 0
    for session in config["sessions"]:
        planned_order = 0
        for channel_id in session["channel_order"]:
            if channel_id not in channels:
                raise PlanValidationError(f"unknown channel in rotation: {channel_id}")
            for profile_id in enabled:
                sequence += 1
                planned_order += 1
                profile = profile_by_id[profile_id]
                plans.append({
                    "plan_id": f"PLAN-V2-{sequence:03d}",
                    "campaign_version": config["campaign_version"],
                    "session_id": session["session_id"],
                    "session_sequence": str(session["session_sequence"]),
                    "day_label": session["day_label"], "time_window": session["time_window"],
                    "channel_id": channel_id, "channel_name": channels[channel_id]["channel_name"],
                    "request_profile_id": profile_id, "requested_model": config["requested_model"],
                    "stream": profile["stream"], "planned_order": str(planned_order),
                    "rotation_direction": session["rotation_direction"],
                    "record_type": config["record_type"], "source_type": config["source_type"],
                    "execution_status": config["execution_status"],
                    "actual_tested_at": "", "actual_model": "", "http_status": "",
                    "request_id": "", "actual_result": "", "evidence_path": "",
                    "notes": f"尚未执行；按{session['session_id']}轮换顺序人工采集；使用{profile_id}非敏感测试模板。",
                })
    return plans


def validate_plan(plans: list[dict[str, str]], profiles: list[dict[str, str]]) -> None:
    errors = []
    if len(plans) != 60:
        errors.append(f"plan count must be 60, got {len(plans)}")
    sessions = Counter(row["session_id"] for row in plans)
    channels = Counter(row["channel_id"] for row in plans)
    profile_counts = Counter(row["request_profile_id"] for row in plans)
    if len(sessions) != 3 or set(sessions.values()) != {20}:
        errors.append("each of 3 sessions must contain 20 plans")
    if len(channels) != 5 or set(channels.values()) != {12}:
        errors.append("each of 5 channels must contain 12 plans")
    if set(profile_counts) != {"P01", "P02", "P03", "P04"} or set(profile_counts.values()) != {15}:
        errors.append("P01-P04 must each contain 15 plans")
    ids = [row["plan_id"] for row in plans]
    keys = [(row["session_id"], row["channel_id"], row["request_profile_id"]) for row in plans]
    if len(ids) != len(set(ids)):
        errors.append("plan_id must be unique")
    if len(keys) != len(set(keys)):
        errors.append("session+channel+profile must be unique")
    if any(row["execution_status"] != "not_executed" for row in plans):
        errors.append("all plans must be not_executed")
    if any(any(row[field] != "" for field in ACTUAL_FIELDS) for row in plans):
        errors.append("actual evidence fields must remain empty")
    if any(row["record_type"] != "measurement_plan" or row["source_type"] != "planned_uat" for row in plans):
        errors.append("plan provenance is invalid")
    if any("pending_confirmation" in row[field] for row in plans for field in ACTUAL_FIELDS):
        errors.append("planned actual fields must not use pending_confirmation")
    if any(row["enabled_in_pilot"] == "FALSE" and row["request_profile_id"] in profile_counts for row in profiles):
        errors.append("disabled profiles entered the pilot")
    serialized = json.dumps(plans, ensure_ascii=False).lower()
    if any(term in serialized for term in ("api_key", "authorization", "secret_key")):
        errors.append("plan contains sensitive credential material")
    if errors:
        raise PlanValidationError("; ".join(errors))


def build_summary(config: dict[str, Any], profiles: list[dict[str, str]], plans: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "campaign_version": config["campaign_version"], "record_type": "measurement_plan",
        "execution_status": "not_executed", "total_planned_measurements": len(plans),
        "session_count": len(config["sessions"]), "channel_count": len(config["channels"]),
        "enabled_profile_count": sum(row["enabled_in_pilot"] == "TRUE" for row in profiles),
        "disabled_profile_count": sum(row["enabled_in_pilot"] == "FALSE" for row in profiles),
        "plans_per_session": dict(Counter(row["session_id"] for row in plans)),
        "plans_per_channel": dict(sorted(Counter(row["channel_id"] for row in plans).items(), key=lambda item: int(item[0]))),
        "plans_per_profile": dict(sorted(Counter(row["request_profile_id"] for row in plans).items())),
        "time_windows": {row["session_id"]: row["time_window"] for row in config["sessions"]},
        "rotation_rules": {
            row["session_id"]: {"channel_order": row["channel_order"], "profile_order_within_channel": config["profile_order"]}
            for row in config["sessions"]
        },
        "real_api_calls_performed": 0,
        "limitations": [
            "当前只生成计划，尚未执行。", "执行前需要确认UAT预算。",
            "需要确认测试工具是否支持指定渠道。", "P05、P06尚未进入Pilot。",
            "流式测试需要记录TTFT与SSE结束状态。",
            "普通API无法确认渠道时不能归因给指定渠道。",
            "实际结果必须根据调用证据填写。",
        ],
        "next_manual_step": "确认预算、工具定向渠道能力和停止条件后，按planned_order逐条人工执行并保存截图与后台日志证据。",
    }


def write_outputs(output_dir: Path, plans: list[dict[str, str]], summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / CSV_NAME).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_COLUMNS, lineterminator="\n")
        writer.writeheader(); writer.writerows(plans)
    with (output_dir / JSON_NAME).open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2); handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    try:
        config, profiles = load_json(CONFIG_PATH), read_csv(PROFILES_PATH)
        plans = build_plan(config, profiles)
        validate_plan(plans, profiles)
        summary = build_summary(config, profiles, plans)
        if not args.dry_run:
            write_outputs(args.output_dir, plans, summary)
        print(json.dumps({"status": "ok", "dry_run": args.dry_run, "planned": len(plans), "real_api_calls_performed": 0}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
