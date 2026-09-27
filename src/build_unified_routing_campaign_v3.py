"""Build the deterministic observational unified-routing v3 campaign."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "unified_routing_campaign_v3.json"
PROFILES_PATH = ROOT / "data" / "request_profiles_v2.csv"
PLAN_PATH = ROOT / "output" / "unified_routing_plan_v3.csv"
PAYLOAD_PATH = ROOT / "output" / "unified_routing_payloads_v3.json"
READINESS_PATH = ROOT / "output" / "unified_routing_readiness_v3.json"
PLAN_COLUMNS = [
    "plan_id", "campaign_version", "experiment_type", "session_id",
    "session_sequence", "day_label", "time_window", "round_id",
    "request_profile_id", "requested_model", "stream", "max_tokens",
    "planned_order", "planned_channel_id", "target_channel_control",
    "execution_status", "actual_executed_at", "actual_channel_id",
    "actual_channel_name", "source_type", "evidence_path", "notes",
]
SENSITIVE = ("api_key", "apikey", "authorization", "cookie", "bearer ", "secret")


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_profiles(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    profiles = {row["request_profile_id"]: row for row in rows if row["request_profile_id"] in {"P01", "P02", "P03", "P04"}}
    if set(profiles) != {"P01", "P02", "P03", "P04"}:
        raise ValueError("request_profiles_v2.csv must contain P01-P04")
    return profiles


def build(config: dict[str, Any], profiles: dict[str, dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    plans: list[dict[str, str]] = []
    payloads: list[dict[str, Any]] = []
    sequence = 0
    for session in config["sessions"]:
        for round_number in range(1, 6):
            for profile_id in session["profile_order"]:
                sequence += 1
                profile = profiles[profile_id]
                stream = profile["stream"].strip().upper() == "TRUE"
                plan_id = f"UR-V3-{sequence:03d}"
                plan = {
                    "plan_id": plan_id,
                    "campaign_version": config["campaign_version"],
                    "experiment_type": config["experiment_type"],
                    "session_id": session["session_id"],
                    "session_sequence": str(session["session_sequence"]),
                    "day_label": session["day_label"],
                    "time_window": session["time_window"],
                    "round_id": f"R{round_number:02d}",
                    "request_profile_id": profile_id,
                    "requested_model": config["requested_model"],
                    "stream": "TRUE" if stream else "FALSE",
                    "max_tokens": profile["requested_max_tokens"],
                    "planned_order": str(sequence),
                    "planned_channel_id": "",
                    "target_channel_control": "unavailable",
                    "execution_status": "not_executed",
                    "actual_executed_at": "",
                    "actual_channel_id": "",
                    "actual_channel_name": "",
                    "source_type": config["source_type"],
                    "evidence_path": "",
                    "notes": "",
                }
                plans.append(plan)
                payloads.append({
                    "plan_id": plan_id, "session_id": session["session_id"],
                    "round_id": plan["round_id"], "request_profile_id": profile_id,
                    "requested_model": config["requested_model"],
                    "endpoint": config["endpoint_url"],
                    "messages": [{"role": "user", "content": profile["prompt_template"]}],
                    "stream": stream, "max_tokens": int(profile["requested_max_tokens"]),
                    "planned_channel_id": None, "execution_status": "not_executed",
                    "source_type": config["source_type"], "is_mock": False,
                })
    validate(plans, payloads)
    return plans, payloads


def validate(plans: list[dict[str, str]], payloads: list[dict[str, Any]]) -> None:
    ids = [row["plan_id"] for row in plans]
    if len(plans) != 60 or len(payloads) != 60 or len(ids) != len(set(ids)):
        raise ValueError("campaign must contain 60 unique plans and payloads")
    if ids != [f"UR-V3-{index:03d}" for index in range(1, 61)]:
        raise ValueError("plan IDs are not deterministic")
    if set(Counter(row["session_id"] for row in plans).values()) != {20}:
        raise ValueError("each session must contain 20 requests")
    if set(Counter(row["request_profile_id"] for row in plans).values()) != {15}:
        raise ValueError("each profile must contain 15 requests")
    if any(set(Counter(row["round_id"] for row in plans if row["session_id"] == session).values()) != {4}
           for session in {row["session_id"] for row in plans}):
        raise ValueError("each session must contain five four-request rounds")
    if any(row["planned_channel_id"] or row["actual_channel_id"] or row["actual_executed_at"] for row in plans):
        raise ValueError("planned or actual result fields are populated")
    if any(payload["planned_channel_id"] is not None for payload in payloads):
        raise ValueError("payload planned_channel_id must be null")
    if any(payload["stream"] is not (payload["request_profile_id"] == "P04") for payload in payloads):
        raise ValueError("stream flags do not match profiles")
    serialized = json.dumps({"plans": plans, "payloads": payloads}, ensure_ascii=False).lower()
    if any(term in serialized for term in SENSITIVE):
        raise ValueError("generated output contains credential material")


def readiness(plans: list[dict[str, str]], payloads: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "campaign_version": "unified-routing-v3.0.0",
        "status": "ready_for_human_operated_apifox_execution",
        "execution_status": "not_executed",
        "plan_count": len(plans), "payload_count": len(payloads),
        "planned_channels_removed": all(not row["planned_channel_id"] for row in plans),
        "actual_results_populated": False, "real_api_called": False,
        "scientific_interpretation": "observational_unified_routing",
        "controlled_channel_comparison": False,
    }


def generate(config_path: Path = CONFIG_PATH, profiles_path: Path = PROFILES_PATH,
             plan_path: Path = PLAN_PATH, payload_path: Path = PAYLOAD_PATH,
             readiness_path: Path = READINESS_PATH) -> None:
    config, profiles = read_json(config_path), read_profiles(profiles_path)
    plans, payloads = build(config, profiles)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    with plan_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(plans)
    payload_path.write_text(json.dumps(payloads, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    readiness_path.write_text(json.dumps(readiness(plans, payloads), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    generate()
