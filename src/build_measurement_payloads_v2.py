"""Build deterministic UAT request payload definitions without sending requests."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLAN_PATH = ROOT / "output" / "real_measurement_plan_v2.csv"
DEFAULT_PROFILES_PATH = ROOT / "data" / "request_profiles_v2.csv"
DEFAULT_CAPABILITIES_PATH = ROOT / "config" / "measurement_execution_capabilities_v2.json"
DEFAULT_PAYLOAD_PATH = ROOT / "output" / "real_measurement_payloads_v2.json"
DEFAULT_READINESS_PATH = ROOT / "output" / "measurement_execution_readiness_v2.json"
ACTUAL_FIELDS = {"actual_model", "http_status", "request_id", "actual_result", "measured_at"}
SECRET_TERMS = ("api_key", "apikey", "authorization", "secret_key", "access_token")


class PayloadValidationError(ValueError):
    pass


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def parse_bool(value: str) -> bool:
    normalized = value.strip().upper()
    if normalized not in {"TRUE", "FALSE"}:
        raise PayloadValidationError(f"invalid boolean: {value!r}")
    return normalized == "TRUE"


def load_profiles(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    profiles = {row["request_profile_id"]: row for row in rows}
    required = {f"P{i:02d}" for i in range(1, 5)}
    if not required <= profiles.keys():
        raise PayloadValidationError("P01-P04 profiles are required")
    return profiles


def build_payloads(
    plans: list[dict[str, str]], profiles: dict[str, dict[str, str]]
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for plan in plans:
        profile = profiles.get(plan["request_profile_id"])
        if profile is None:
            raise PayloadValidationError(f"unknown profile: {plan['request_profile_id']}")
        stream = parse_bool(profile["stream"])
        if parse_bool(plan["stream"]) != stream:
            raise PayloadValidationError(f"plan/profile stream mismatch: {plan['plan_id']}")
        payload: dict[str, Any] = {
            "plan_id": plan["plan_id"],
            "session_id": plan["session_id"],
            "channel_id": plan["channel_id"],
            "channel_name": plan["channel_name"],
            "request_profile_id": plan["request_profile_id"],
            "requested_model": plan["requested_model"],
            "messages": [{"role": "user", "content": profile["prompt_template"]}],
            "stream": stream,
            "max_tokens": int(profile["requested_max_tokens"]),
            "execution_status": "not_executed",
            "source_type": "planned_uat",
            "is_mock": False,
        }
        temperature = profile["requested_temperature"].strip()
        if temperature:
            payload["temperature"] = float(temperature)
        payloads.append(payload)
    validate_payloads(plans, payloads)
    return payloads


def validate_payloads(
    plans: list[dict[str, str]], payloads: list[dict[str, Any]]
) -> None:
    errors: list[str] = []
    if len(plans) != 60 or len(payloads) != 60:
        errors.append("exactly 60 plans and 60 payloads are required")
    plan_ids = [row["plan_id"] for row in plans]
    payload_ids = [row["plan_id"] for row in payloads]
    if len(payload_ids) != len(set(payload_ids)):
        errors.append("payload plan_id must be unique")
    if plan_ids != payload_ids:
        errors.append("payloads must correspond one-to-one and in order with plans")
    for row in payloads:
        expected_stream = row["request_profile_id"] == "P04"
        if row["stream"] is not expected_stream:
            errors.append(f"invalid stream value: {row['plan_id']}")
        if row["execution_status"] != "not_executed" or row["source_type"] != "planned_uat":
            errors.append(f"invalid planned provenance: {row['plan_id']}")
        if row["is_mock"] is not False:
            errors.append(f"is_mock must be false: {row['plan_id']}")
        if ACTUAL_FIELDS & row.keys():
            errors.append(f"actual fields are forbidden: {row['plan_id']}")
    serialized = json.dumps(payloads, ensure_ascii=False).lower()
    if any(term in serialized for term in SECRET_TERMS):
        errors.append("payload output contains credential material")
    if errors:
        raise PayloadValidationError("; ".join(errors))


def _profile_readiness(
    profile_id: str, capabilities: dict[str, Any]
) -> dict[str, Any]:
    caps = capabilities["capabilities"]
    required = ["supports_target_channel", "supports_custom_prompt", "supports_max_tokens"]
    if profile_id == "P04":
        required.append("supports_stream")
    def state(name: str) -> Any:
        item = caps.get(name)
        if isinstance(item, dict):
            if item.get("confirmation_status") != "confirmed":
                return "pending_confirmation"
            return item.get("value")
        return item

    pending = [name for name in required if state(name) == "pending_confirmation"]
    blocked = [name for name in required if state(name) is False]
    if blocked:
        status = "blocked_capability"
    elif pending:
        status = "pending_confirmation"
    else:
        status = "ready"
    optional_observation = [
        name for name in ("exposes_http_status", "exposes_request_id", "exposes_actual_model")
        if state(name) == "pending_confirmation"
    ]
    if profile_id == "P04" and state("exposes_ttft") == "pending_confirmation":
        optional_observation.append("exposes_ttft")
    return {
        "request_profile_id": profile_id,
        "readiness_status": status,
        "required_capabilities": required,
        "pending_required_capabilities": pending,
        "blocked_required_capabilities": blocked,
        "pending_observation_capabilities": optional_observation,
    }


def build_readiness(capabilities: dict[str, Any]) -> dict[str, Any]:
    profiles = [_profile_readiness(f"P{i:02d}", capabilities) for i in range(1, 5)]
    ready_profile_count = sum(row["readiness_status"] == "ready" for row in profiles)
    ready_profiles = {
        row["request_profile_id"] for row in profiles if row["readiness_status"] == "ready"
    }
    plan_profile_counts = {f"P{i:02d}": 15 for i in range(1, 5)}
    ready_plan_count = sum(plan_profile_counts[profile] for profile in ready_profiles)
    return {
        "config_version": capabilities["config_version"],
        "execution_tool": capabilities["execution_tool"],
        "overall_status": (
            "ready" if all(row["readiness_status"] == "ready" for row in profiles)
            else "blocked_capability" if any(row["readiness_status"] == "blocked_capability" for row in profiles)
            else "pending_confirmation"
        ),
        "profiles": profiles,
        "ready_profile_count": ready_profile_count,
        "ready_plan_count": ready_plan_count,
        "blocked_plan_count": sum(
            plan_profile_counts[row["request_profile_id"]]
            for row in profiles if row["readiness_status"] == "blocked_capability"
        ),
        "pending_plan_count": 60 - ready_plan_count - sum(
            plan_profile_counts[row["request_profile_id"]]
            for row in profiles if row["readiness_status"] == "blocked_capability"
        ),
        "scheduler_real_execution_authorized": capabilities.get(
            "scheduler_real_execution_authorized", False
        ),
        "automatic_routing_authorized": capabilities.get(
            "automatic_routing_authorized", False
        ),
        "note": "就绪仅表示用户可人工执行计划内UAT；不授权Scheduler真实执行或自动路由。",
        "real_api_calls_performed": 0,
    }


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    data = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary.write_text(data, encoding="utf-8", newline="\n")
    temporary.replace(path)


def generate(
    plan_path: Path = DEFAULT_PLAN_PATH,
    profiles_path: Path = DEFAULT_PROFILES_PATH,
    capabilities_path: Path = DEFAULT_CAPABILITIES_PATH,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    plans = read_csv(plan_path)
    profiles = load_profiles(read_csv(profiles_path))
    payloads = build_payloads(plans, profiles)
    readiness = build_readiness(read_json(capabilities_path))
    return payloads, readiness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN_PATH)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES_PATH)
    parser.add_argument("--capabilities", type=Path, default=DEFAULT_CAPABILITIES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_PAYLOAD_PATH)
    parser.add_argument("--readiness-output", type=Path, default=DEFAULT_READINESS_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--readiness-only", action="store_true",
        help="write only the readiness audit; leave the protected payload file untouched",
    )
    args = parser.parse_args(argv)
    try:
        payloads, readiness = generate(args.plan, args.profiles, args.capabilities)
        if not args.dry_run:
            if not args.readiness_only:
                write_json_atomic(args.output, payloads)
            write_json_atomic(args.readiness_output, readiness)
        print(json.dumps({
            "status": "ok", "dry_run": args.dry_run, "payload_count": len(payloads),
            "overall_readiness": readiness["overall_status"], "real_api_calls_performed": 0,
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
