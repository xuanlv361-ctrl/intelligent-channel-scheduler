"""Build a deterministic, shadow-only real-channel UAT measurement catalog."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "real_channel_measurements_v1.csv"
DEFAULT_CATALOG_OUTPUT = ROOT / "data" / "channel_catalog_real_v1.csv"
DEFAULT_SUMMARY_OUTPUT = ROOT / "output" / "real_channel_measurement_summary_v1.json"
CATALOG_VERSION = "real-v1.0.0"
EXPECTED_CHANNELS = {"19", "27", "45", "48", "50"}
EXPECTED_IDS = {f"RM{i:03d}" for i in range(1, 26)}
ROUTING_BLOCK_REASON = (
    "actual_model_unconfirmed;http_status_unconfirmed;"
    "sample_size_below_routing_threshold"
)
CATALOG_COLUMNS = [
    "catalog_version", "candidate_id", "channel_id", "channel_name",
    "requested_model", "actual_model", "source_type", "measurement_count",
    "observed_success_count", "observed_failure_count", "observed_success_rate",
    "latency_mean_ms", "latency_p50_ms", "latency_min_ms", "latency_max_ms",
    "input_price_per_1m", "output_price_per_1m", "cache_read_price_per_1m",
    "cache_creation_price_per_1m", "currency", "supports_text",
    "supports_non_stream", "supports_stream", "mapping_status", "confidence_level",
    "shadow_eligible", "routing_eligible", "routing_block_reason",
    "metrics_observed_from", "metrics_observed_to", "data_source", "is_mock", "notes",
]
EXPECTED_LATENCIES = {
    "19": (732.0, 710.0, 570.0, 850.0),
    "27": (2264.0, 2120.0, 1910.0, 3030.0),
    "45": (2568.0, 1340.0, 1090.0, 7370.0),
    "48": (826.0, 870.0, 680.0, 970.0),
    "50": (1712.0, 1590.0, 1540.0, 2140.0),
}


class ValidationError(ValueError):
    """Input evidence violates the required measurement contract."""


def read_measurements(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _number(row: dict[str, str], field: str, *, allow_empty: bool = False) -> float | None:
    value = row.get(field, "")
    if value == "" and allow_empty:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{row.get('measurement_id', '<unknown>')}: invalid {field}") from exc


def validate_measurements(rows: list[dict[str, str]]) -> None:
    errors: list[str] = []
    if len(rows) != 25:
        errors.append(f"measurement_count must be 25, got {len(rows)}")
    ids = [row.get("measurement_id", "") for row in rows]
    if any(not value for value in ids):
        errors.append("measurement_id must be non-empty")
    if len(ids) != len(set(ids)):
        errors.append("measurement_id must be unique")
    if set(ids) != EXPECTED_IDS:
        errors.append("measurement_id must cover RM001-RM025")

    keys = [(row.get("test_round", ""), row.get("channel_id", "")) for row in rows]
    if len(keys) != len(set(keys)):
        errors.append("test_round+channel_id must be unique")

    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    rounds: dict[str, int] = defaultdict(int)
    for row in rows:
        mid = row.get("measurement_id", "<unknown>")
        channel_id = row.get("channel_id", "")
        if not channel_id:
            errors.append(f"{mid}: channel_id must be non-empty")
        groups[channel_id].append(row)
        try:
            test_round = int(row.get("test_round", ""))
            if test_round not in range(1, 6):
                errors.append(f"{mid}: test_round must be 1-5")
            rounds[str(test_round)] += 1
        except ValueError:
            errors.append(f"{mid}: test_round must be an integer")
        if row.get("requested_model") != "deepseek-v4-flash":
            errors.append(f"{mid}: requested_model mismatch")
        if row.get("test_result") not in {"pass", "fail"}:
            errors.append(f"{mid}: invalid test_result")
        if row.get("source_type") != "measured":
            errors.append(f"{mid}: source_type must be measured")
        if row.get("is_mock") != "FALSE":
            errors.append(f"{mid}: is_mock must be FALSE")
        data_source = row.get("data_source", "")
        if "channel_direct_test" not in data_source and "backend_log" not in data_source:
            errors.append(f"{mid}: invalid data_source")
        if row.get("actual_model") != "pending_confirmation":
            errors.append(f"{mid}: actual_model must remain pending_confirmation")
        if row.get("http_status") != "pending_confirmation":
            errors.append(f"{mid}: http_status must remain pending_confirmation")
        if row.get("request_id"):
            errors.append(f"{mid}: request_id must remain empty")
        try:
            latency = _number(row, "latency_ms", allow_empty=True)
            if row.get("test_result") == "pass" and (latency is None or latency <= 0):
                errors.append(f"{mid}: successful measurement requires positive latency_ms")
            for field in ("prompt_tokens", "completion_tokens"):
                value = _number(row, field)
                if value is not None and value < 0:
                    errors.append(f"{mid}: {field} must be non-negative")
            cost = _number(row, "calculated_cost", allow_empty=True)
            if cost is not None and cost < 0:
                errors.append(f"{mid}: calculated_cost must be non-negative")
        except ValidationError as exc:
            errors.append(str(exc))

    if set(groups) != EXPECTED_CHANNELS:
        errors.append(f"channels must be {sorted(EXPECTED_CHANNELS, key=int)}")
    for channel_id in sorted(EXPECTED_CHANNELS, key=int):
        channel_rows = groups.get(channel_id, [])
        if len(channel_rows) != 5:
            errors.append(f"channel {channel_id} must have 5 measurements")
        if {row.get("test_round") for row in channel_rows} != {str(i) for i in range(1, 6)}:
            errors.append(f"channel {channel_id} must cover rounds 1-5")
    for test_round in range(1, 6):
        if rounds.get(str(test_round), 0) != 5:
            errors.append(f"round {test_round} must have 5 measurements")

    rm022 = next((row for row in rows if row.get("measurement_id") == "RM022"), None)
    required_rm022 = {
        "channel_id": "45", "latency_ms": "7370", "completion_tokens": "10",
        "calculated_cost": "0.000025", "error_category": "latency_outlier",
    }
    if rm022 is None or any(rm022.get(key) != value for key, value in required_rm022.items()):
        errors.append("RM022 long-tail evidence is incomplete or altered")
    if errors:
        raise ValidationError("; ".join(errors))


def group_measurements(rows: Iterable[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["channel_id"]].append(row)
    return dict(groups)


def percentile_nearest_rank(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one value")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def _derive_token_prices(rows: list[dict[str, str]]) -> tuple[float, float]:
    equations = []
    for row in rows:
        if row["test_result"] == "pass" and row.get("calculated_cost"):
            equations.append((float(row["prompt_tokens"]), float(row["completion_tokens"]), float(row["calculated_cost"]) * 1_000_000))
    pairs = [(a, b) for i, a in enumerate(equations) for b in equations[i + 1:] if a[0] * b[1] != b[0] * a[1]]
    if not pairs:
        raise ValidationError("input/output token prices cannot be derived from evidence")
    first, second = pairs[0]
    determinant = first[0] * second[1] - second[0] * first[1]
    input_price = (first[2] * second[1] - second[2] * first[1]) / determinant
    output_price = (first[0] * second[2] - second[0] * first[2]) / determinant
    if any(abs(prompt * input_price + completion * output_price - total) > 1e-9 for prompt, completion, total in equations):
        raise ValidationError("calculated_cost is inconsistent with a single token-price pair")
    return input_price, output_price


def _fmt(value: float, places: int) -> str:
    return f"{value:.{places}f}"


def build_channel_record(channel_rows: list[dict[str, str]], prices: tuple[float, float]) -> dict[str, str]:
    channel_rows = sorted(channel_rows, key=lambda row: int(row["test_round"]))
    successful = [row for row in channel_rows if row["test_result"] == "pass"]
    latencies = [float(row["latency_ms"]) for row in successful]
    actual_models = {row["actual_model"] for row in channel_rows}
    if len(actual_models) != 1:
        raise ValidationError(f"channel {channel_rows[0]['channel_id']}: inconsistent actual_model")
    success_count = len(successful)
    measurement_count = len(channel_rows)
    return {
        "catalog_version": CATALOG_VERSION,
        "candidate_id": f"REAL-CHANNEL-{channel_rows[0]['channel_id']}",
        "channel_id": channel_rows[0]["channel_id"],
        "channel_name": channel_rows[0]["channel_name"],
        "requested_model": channel_rows[0]["requested_model"],
        "actual_model": next(iter(actual_models)),
        "source_type": "measured",
        "measurement_count": str(measurement_count),
        "observed_success_count": str(success_count),
        "observed_failure_count": str(measurement_count - success_count),
        "observed_success_rate": _fmt(success_count / measurement_count, 6),
        "latency_mean_ms": _fmt(statistics.mean(latencies), 3),
        "latency_p50_ms": _fmt(percentile_nearest_rank(latencies, 0.5), 3),
        "latency_min_ms": _fmt(min(latencies), 3),
        "latency_max_ms": _fmt(max(latencies), 3),
        "input_price_per_1m": _fmt(prices[0], 9),
        "output_price_per_1m": _fmt(prices[1], 9),
        "cache_read_price_per_1m": "pending_confirmation",
        "cache_creation_price_per_1m": "pending_confirmation",
        "currency": channel_rows[0]["currency"],
        "supports_text": "TRUE", "supports_non_stream": "TRUE",
        "supports_stream": "pending_confirmation", "mapping_status": "pending_confirmation",
        "confidence_level": "low", "shadow_eligible": "TRUE", "routing_eligible": "FALSE",
        "routing_block_reason": ROUTING_BLOCK_REASON,
        "metrics_observed_from": min(row["tested_at"] for row in channel_rows),
        "metrics_observed_to": max(row["tested_at"] for row in channel_rows),
        "data_source": "channel_direct_test+backend_log", "is_mock": "FALSE",
        "notes": (
            "仅表示本次UAT非流式短请求的5条观测；observed_success_rate=1.0不等于长期成功率100%；"
            "actual_model、HTTP状态和Request ID待确认；不可用于生产性能排名或真实自动路由。"
        ),
    }


def build_catalog(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    prices = _derive_token_prices(rows)
    groups = group_measurements(rows)
    catalog = [build_channel_record(groups[channel_id], prices) for channel_id in sorted(groups, key=int)]
    actual = {
        row["channel_id"]: tuple(float(row[field]) for field in ("latency_mean_ms", "latency_p50_ms", "latency_min_ms", "latency_max_ms"))
        for row in catalog
    }
    if actual != EXPECTED_LATENCIES:
        raise ValidationError(f"latency results differ from expected: {actual}")
    return catalog


def build_summary(catalog: list[dict[str, str]], rows: list[dict[str, str]], source_path: Path) -> dict[str, Any]:
    return {
        "catalog_version": CATALOG_VERSION,
        "source_file": "data/real_channel_measurements_v1.csv",
        "source_file_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest().upper(),
        "measurement_count": len(rows), "channel_count": len(catalog),
        "round_count": len({row["test_round"] for row in rows}),
        "observed_success_count": sum(row["test_result"] == "pass" for row in rows),
        "observed_failure_count": sum(row["test_result"] == "fail" for row in rows),
        "actual_model_status": "pending_confirmation",
        "http_status_status": "pending_confirmation",
        "request_id_status": "missing",
        "routing_readiness": "shadow_only",
        "channel_summaries": [
            {key: row[key] for key in (
                "candidate_id", "channel_id", "channel_name", "measurement_count",
                "observed_success_count", "observed_failure_count", "observed_success_rate",
                "latency_mean_ms", "latency_p50_ms", "latency_min_ms", "latency_max_ms",
                "shadow_eligible", "routing_eligible", "routing_block_reason",
            )}
            for row in catalog
        ],
        "limitations": [
            "每渠道仅5个样本。", "仅覆盖当前UAT测试时段。", "actual_model未确认。",
            "HTTP状态未直接确认。", "Request ID缺失。", "未测试流式能力。",
            "不能外推长期稳定性。", "不能据此直接接管真实流量。",
        ],
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CATALOG_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--catalog-output", type=Path, default=DEFAULT_CATALOG_OUTPUT)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        input_hash = hashlib.sha256(args.input.read_bytes()).hexdigest()
        rows = read_measurements(args.input)
        validate_measurements(rows)
        catalog = build_catalog(rows)
        summary = build_summary(catalog, rows, args.input)
        if hashlib.sha256(args.input.read_bytes()).hexdigest() != input_hash:
            raise ValidationError("input file changed during build")
        if not args.dry_run:
            write_csv(args.catalog_output, catalog)
            write_json(args.summary_output, summary)
        print(json.dumps({"status": "ok", "dry_run": args.dry_run, "measurement_count": len(rows), "channel_count": len(catalog), "routing_readiness": "shadow_only"}, ensure_ascii=False))
        return 0
    except (OSError, ValidationError, KeyError, ValueError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
