from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "request_records.csv"
DEFAULT_TAXONOMY = ROOT / "data" / "error_taxonomy.csv"
DEFAULT_OUTPUT = ROOT / "data" / "metrics_snapshot_v1.csv"
SNAPSHOT_VERSION = "v1.0.0"
FIELDS = ["snapshot_version","updated_at","environment","requested_model","actual_model","channel_id","channel_name","local_group","sample_size","success_count","failure_count","excluded_count","success_rate","latency_avg_ms","latency_p50_ms","ttft_avg_ms","input_price_per_1m","output_price_per_1m","cache_read_price_per_1m","cache_creation_price_per_1m","currency","price_version","price_observed_at","data_source","is_mock","confidence_level","notes"]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        return [{k: (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]


def parse_date(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ValueError(f"无效 tested_at：{value}")


def number(value: str) -> float | None:
    try:
        return float(value) if value != "" else None
    except ValueError:
        return None


def display(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def build_snapshot(records: list[dict[str, str]], taxonomy: list[dict[str, str]]) -> list[dict[str, str]]:
    if not records:
        return []
    updated_at = max((parse_date(r["tested_at"]) for r in records)).strftime("%Y-%m-%d %H:%M:%S")
    faults = {r["error_category"]: r["channel_fault"] for r in taxonomy}
    grouped: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    excluded: dict[tuple[str, str], int] = defaultdict(int)
    for record in records:
        if not record.get("channel_id"):
            excluded[(record.get("environment", ""), record.get("requested_model", ""))] += 1
            continue
        key = tuple(record.get(f, "") for f in ("environment","requested_model","actual_model","channel_id","channel_name","local_group","currency","data_source","is_mock"))
        grouped[key].append(record)
    output = []
    for key in sorted(grouped):
        rows = grouped[key]
        environment, requested_model, actual_model, channel_id, channel_name, local_group, currency, data_source, is_mock = key
        successes, failures = 0, 0
        for row in rows:
            categories = [x for x in row.get("error_category", "").split(";") if x]
            channel_fault = any(faults.get(x) == "TRUE" for x in categories)
            status = int(row["http_status"]) if row.get("http_status", "").isdigit() else 0
            if 200 <= status < 300 and row.get("result") != "fail":
                successes += 1
            elif channel_fault:
                failures += 1
        denominator = successes + failures
        latencies = [number(r.get("latency_ms", "")) for r in rows]
        latencies = [v for v in latencies if v is not None]
        ttfts = [number(r.get("ttft_ms", "")) for r in rows if r.get("stream", "").lower() == "true"]
        ttfts = [v for v in ttfts if v is not None]
        def unique(field: str) -> str:
            values = sorted({r.get(field, "") for r in rows if r.get(field, "")})
            return values[0] if len(values) == 1 else ""
        count = len(rows)
        excluded_count = excluded.get((environment, requested_model), 0)
        notes = ["样本量不足，不可用于宣称真实渠道性能优劣。"]
        if excluded_count:
            notes.append(f"另有 {excluded_count} 条缺少 channel_id 的记录已排除，未归入真实渠道。")
        output.append(dict(zip(FIELDS, [SNAPSHOT_VERSION,updated_at,environment,requested_model,actual_model,channel_id,channel_name,local_group,str(count),str(successes),str(failures),str(excluded_count),display(successes/denominator) if denominator else "",display(statistics.mean(latencies)) if latencies else "",display(statistics.median(latencies)) if latencies else "",display(statistics.mean(ttfts)) if ttfts else "",unique("input_price_per_1m"),unique("output_price_per_1m"),unique("cache_read_price_per_1m"),unique("cache_creation_price_per_1m"),currency,"pending_confirmation",max(rows, key=lambda r: parse_date(r["tested_at"]))["tested_at"],data_source,is_mock,"low"," ".join(notes)])))
    return output


def write_snapshot(rows: list[dict[str, str]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成版本化渠道指标快照")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT); parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY); parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT); parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        rows = build_snapshot(read_rows(args.input), read_rows(args.taxonomy))
        if not args.dry_run: write_snapshot(rows, args.output)
        print(f"指标快照 {'DRY-RUN' if args.dry_run else '生成完成'}: {len(rows)} 行，版本 {SNAPSHOT_VERSION}")
        return 0
    except (OSError, ValueError, csv.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
