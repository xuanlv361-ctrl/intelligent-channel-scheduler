from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "first_week_source_records.csv"
DEFAULT_TARGET = ROOT / "data" / "request_records.csv"
FINGERPRINT_FIELDS = ("tested_at", "environment", "requested_model", "http_status", "prompt_tokens", "completion_tokens", "cost", "currency", "data_source")
SECRET_PATTERNS = (re.compile(r"sk-[A-Za-z0-9_-]{10,}", re.I), re.compile(r"(?:authorization\s*[:=]\s*)?bearer\s+\S+", re.I))


class ImportErrorDetail(ValueError):
    pass


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        rows = [{k: (v or "") for k, v in row.items() if k is not None} for row in reader]
    return headers, rows


def fingerprint(row: dict[str, str]) -> str:
    material = "\x1f".join((row.get(field) or "").strip() for field in FINGERPRINT_FIELDS)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def validate_source(headers: list[str], rows: list[dict[str, str]], target_headers: list[str]) -> None:
    if headers != target_headers:
        raise ImportErrorDetail("源 CSV 必须严格使用 request_records.csv 的 33 列及其顺序")
    for index, row in enumerate(rows, 2):
        text = " ".join(row.values())
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            raise ImportErrorDetail(f"第 {index} 行疑似包含 API Key 或完整鉴权头")
        if not row.get("record_id") or not row.get("tested_at") or not row.get("requested_model"):
            raise ImportErrorDetail(f"第 {index} 行缺少稳定来源编号、tested_at 或 requested_model")
        if set(row) != set(target_headers):
            raise ImportErrorDetail(f"第 {index} 行结构异常")


def plan_import(source: Path, target: Path) -> dict[str, object]:
    if not source.exists():
        raise ImportErrorDetail(f"找不到源文件：{source}")
    source_headers, source_rows = read_csv(source)
    if target.exists():
        target_headers, target_rows = read_csv(target)
    else:
        target_headers, target_rows = source_headers, []
    validate_source(source_headers, source_rows, target_headers)

    ids = {r.get("record_id", "") for r in target_rows}
    request_ids = {r.get("request_id", "") for r in target_rows if r.get("request_id")}
    fingerprints = {fingerprint(r) for r in target_rows}
    additions, skipped = [], 0
    for row in source_rows:
        request_id = row.get("request_id", "").strip()
        fp = fingerprint(row)
        if row["record_id"] in ids or (request_id and request_id in request_ids) or fp in fingerprints:
            skipped += 1
            continue
        additions.append(row)
        ids.add(row["record_id"])
        if request_id:
            request_ids.add(request_id)
        fingerprints.add(fp)
    warnings = sum(1 for r in source_rows if not r.get("actual_model") or not r.get("channel_id") or not r.get("request_id") or not r.get("ttft_ms"))
    return {"headers": target_headers, "existing": target_rows, "additions": additions, "new_count": len(additions), "skip_count": skipped, "warning_count": warnings, "record_ids": [r["record_id"] for r in source_rows]}


def execute(source: Path, target: Path, dry_run: bool = False) -> dict[str, object]:
    result = plan_import(source, target)
    if dry_run:
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        shutil.copy2(target, target.with_name(f"{target.stem}_before_first_week_import_{stamp}{target.suffix}"))
    with target.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=result["headers"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(result["existing"] + result["additions"])
    return result


def print_result(result: dict[str, object], dry_run: bool) -> None:
    print("第一周记录导入 DRY-RUN" if dry_run else "第一周记录导入完成")
    print(f"计划新增数量: {result['new_count']}")
    print(f"重复跳过数量: {result['skip_count']}")
    print(f"告警数量: {result['warning_count']}")
    print(f"计划生成或保留的 record_id: {', '.join(result['record_ids'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="幂等导入第一周六组规范化源记录")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = execute(args.input, args.target, args.dry_run)
        print_result(result, args.dry_run)
        return 0
    except (OSError, csv.Error, ImportErrorDetail) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
