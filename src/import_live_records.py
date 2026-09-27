"""真实采集记录导入主数据集

从 output/live_request_records.csv 读取实时采集记录，
按 data/request_records.csv 的完整 33 字段结构转换并追加。
"""

from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path
from typing import Any

# ── 项目根目录 ─────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

LIVE_RECORDS_CSV = os.path.join(PROJECT_ROOT, "output", "live_request_records.csv")
MAIN_RECORDS_CSV = os.path.join(PROJECT_ROOT, "data", "request_records.csv")
BACKUP_FILE = os.path.join(PROJECT_ROOT, "output", "request_records_before_live_import.csv")

# 主表完整 33 字段
MAIN_FIELDS = [
    "record_id",
    "tested_at",
    "environment",
    "request_path",
    "requested_model",
    "actual_model",
    "channel_id",
    "channel_name",
    "local_group",
    "stream",
    "http_status",
    "latency_ms",
    "ttft_ms",
    "finish_reason",
    "stream_status",
    "raw_input_tokens",
    "cache_creation_tokens",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "extended_output_tokens",
    "input_price_per_1m",
    "output_price_per_1m",
    "cache_read_price_per_1m",
    "cache_creation_price_per_1m",
    "cost",
    "currency",
    "request_id",
    "result",
    "error_category",
    "data_source",
    "is_mock",
    "notes",
]

# 实时采集 CSV 字段顺序（live_request_records.csv 列顺序）
LIVE_FIELDS = [
    "tested_at",
    "environment",
    "request_path",
    "requested_model",
    "actual_model",
    "stream",
    "http_status",
    "latency_ms",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "extended_output_tokens",
    "request_id",
    "result",
    "error_category",
    "data_source",
    "is_mock",
    "notes",
]

# 主表提供但实时采集不应生成的字段 → 留空
EMPTY_FIELDS = {
    "channel_id": "",
    "channel_name": "",
    "local_group": "",
    "ttft_ms": "",
    "stream_status": "",
    "raw_input_tokens": "",
    "cache_creation_tokens": "",
    "input_price_per_1m": "",
    "output_price_per_1m": "",
    "cache_read_price_per_1m": "",
    "cache_creation_price_per_1m": "",
    "cost": "",
    "currency": "",
}

# 旧采集器遗留的错误分类 → 新标准分类
ERROR_CATEGORY_MAP: dict[str, str] = {
    "authentication_failed": "user_authentication_error",
    "bad_request": "request_parameter_error",
    "rate_limited": "rate_limit",
    "service_unavailable": "platform_routing_error",
}

# 非 2xx 响应时必须清空的字段（失败响应无测量值）
FAILURE_EMPTY_FIELDS = frozenset({
    "actual_model",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "extended_output_tokens",
})


def _sanitize_bool(val: Any) -> str:
    """将 bool / str / int 统一规范化为 'TRUE' / 'FALSE' 或原样"""
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, str):
        upper = val.strip().upper()
        if upper in ("TRUE", "FALSE"):
            return upper
        # 尝试转义
        if upper == "T":
            return "TRUE"
        if upper == "F":
            return "FALSE"
        return val
    # 数字
    if val == 0 or val == 1:
        return "TRUE" if val else "FALSE"
    return str(val) if val else ""


def _normalize_value(val: Any) -> str:
    """将各种类型统一为字符串"""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    return str(val)


def _is_http_success(http_status: str) -> bool:
    """判断 HTTP 状态码是否为 2xx 成功"""
    s = http_status.strip()
    return len(s) >= 3 and s[0] == "2"


def _normalize_error_category(raw: str) -> str:
    """将旧分类映射到新标准分类"""
    key = raw.strip()
    return ERROR_CATEGORY_MAP.get(key, key)


def read_main_records() -> list[dict[str, str]]:
    """读取主数据集，返回 list[dict]，字段均为字符串"""
    if not os.path.isfile(MAIN_RECORDS_CSV):
        return []
    with open(MAIN_RECORDS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [row for row in reader]


def read_live_records() -> list[dict[str, str]]:
    """读取实时采集 CSV"""
    if not os.path.isfile(LIVE_RECORDS_CSV):
        print(f"[ERROR] 实时采集文件不存在: {LIVE_RECORDS_CSV}", file=sys.stderr)
        sys.exit(1)
    with open(LIVE_RECORDS_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [row for row in reader]


def get_next_record_id(existing: list[dict[str, str]]) -> str:
    """根据现有记录生成下一个 record_id，如 R008"""
    max_num = 0
    for row in existing:
        rid = (row.get("record_id") or "").strip()
        m = re.match(r"R(\d+)$", rid, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            if num > max_num:
                max_num = num
    return f"R{max_num + 1:03d}"


def existing_request_ids(existing: list[dict[str, str]]) -> set[str]:
    """收集已有记录的 request_id（去重用）"""
    s: set[str] = set()
    for row in existing:
        rid = (row.get("request_id") or "").strip()
        if rid:
            s.add(rid)
    return s


def auto_detect_result(
    live_row: dict[str, str],
) -> tuple[str, str, str]:
    """根据实时行自动判定 result / error_category / notes

    返回 (result, error_category, notes)
    """
    raw_result = (live_row.get("result") or "").strip()
    raw_category = (live_row.get("error_category") or "").strip()

    # 非成功状态：保留原名
    if raw_result in ("fail", "error", "partial_pass", "success_with_warning"):
        return raw_result, raw_category, live_row.get("notes", "")

    # 成功状态下检测 usage 不一致
    if raw_result == "pass" or raw_result == "":
        ct_raw = (live_row.get("completion_tokens") or "").strip()
        eo_raw = (live_row.get("extended_output_tokens") or "").strip()

        # 两个字段均真实存在（非空）且数值不相等 → warning
        if ct_raw and eo_raw:
            try:
                ct_val = int(ct_raw)
                eo_val = int(eo_raw)
                if ct_val != eo_val:
                    notes = f"completion_tokens={ct_val}, extended_output_tokens={eo_val}"
                    if live_row.get("notes", "").strip():
                        notes = f"{live_row['notes'].strip()}; {notes}"
                    return "success_with_warning", "usage_field_mismatch", notes
            except ValueError:
                pass

    return raw_result or "pass", raw_category, live_row.get("notes", "")


def convert_to_main_format(
    live_row: dict[str, str],
    record_id: str,
) -> dict[str, str]:
    """将一条实时采集记录转换为 33 字段主表格式"""
    row: dict[str, str] = {}

    # 1. record_id
    row["record_id"] = record_id

    # 2. 实时字段（保持原样）
    for field in LIVE_FIELDS:
        val = live_row.get(field, "")
        # is_mock 统一规范化
        if field == "is_mock":
            row[field] = _sanitize_bool(val)
        else:
            row[field] = _normalize_value(val)

    # 3. 空字段
    for field, default in EMPTY_FIELDS.items():
        row[field] = default

    # 4. 智能检测分类
    result, ec, notes = auto_detect_result(live_row)
    row["result"] = result
    row["error_category"] = ec
    row["notes"] = notes

    # 5. 旧分类映射
    row["error_category"] = _normalize_error_category(row["error_category"])

    # 6. 非 2xx → 清空失败响应无测量值字段
    if not _is_http_success(row["http_status"]):
        for fld in FAILURE_EMPTY_FIELDS:
            row[fld] = ""

    return row


def create_backup(rows: list[dict[str, str]]) -> None:
    """备份主数据集到 output/"""
    os.makedirs(os.path.dirname(BACKUP_FILE), exist_ok=True)
    with open(BACKUP_FILE, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MAIN_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"[INFO] 备份已创建: {BACKUP_FILE}")


def write_main_records(rows: list[dict[str, str]]) -> None:
    """将完整主数据写回"""
    with open(MAIN_RECORDS_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MAIN_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"[INFO] 主数据集已更新: {MAIN_RECORDS_CSV}")


def import_live(dry_run: bool = False) -> dict[str, Any]:
    """执行导入，返回统计信息"""
    main_rows = read_main_records()
    live_rows = read_live_records()

    existing_rids = existing_request_ids(main_rows)
    next_id = get_next_record_id(main_rows)

    new_count = 0
    skip_count = 0
    warning_count = 0
    generated_ids: list[str] = []

    converted: list[dict[str, str]] = []

    for live_row in live_rows:
        request_id = (live_row.get("request_id") or "").strip()

        # 跳过空行（tested_at 和 request_id 均为空 → 旧测试污染）
        if not request_id and not (live_row.get("tested_at") or "").strip():
            skip_count += 1
            continue

        # 去重：request_id 已存在
        if request_id and request_id in existing_rids:
            skip_count += 1
            continue

        # 转换
        rid = next_id
        next_id_num = int(re.search(r"(\d+)", next_id).group(1))  # type: ignore[union-attr]
        next_id = f"R{next_id_num + 1:03d}"

        rec = convert_to_main_format(live_row, rid)
        generated_ids.append(rid)
        new_count += 1

        # 统计 warning
        if rec.get("result") == "success_with_warning":
            warning_count += 1

        converted.append(rec)

        # 记录已导入的 request_id，避免同批次重复
        if request_id:
            existing_rids.add(request_id)

    # 统计结果
    result = {
        "new_count": new_count,
        "skip_count": skip_count,
        "warning_count": warning_count,
        "generated_ids": generated_ids,
        "converted": converted,
        "main_rows": main_rows,
    }

    if not dry_run:
        # 备份
        create_backup(main_rows)
        # 追加
        main_rows.extend(converted)
        write_main_records(main_rows)
    else:
        print("\n========== DRY-RUN 报告 ==========")
        print(f"计划新增:     {new_count}")
        print(f"重复跳过:     {skip_count}")
        print(f"告警数量:     {warning_count}")
        print(f"生成的 ID:    {', '.join(generated_ids) if generated_ids else '无'}")
        print("==================================\n")

    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="将实时采集记录导入主数据集")
    parser.add_argument("--dry-run", action="store_true", help="仅报告，不写文件")
    args = parser.parse_args(argv)

    result = import_live(dry_run=args.dry_run)

    if not args.dry_run:
        print("========== 导入完成 ==========")
        print(f"新增数量:     {result['new_count']}")
        print(f"重复跳过:     {result['skip_count']}")
        print(f"告警数量:     {result['warning_count']}")
        print(f"生成的 ID:    {', '.join(result['generated_ids']) if result['generated_ids'] else '无'}")
        print("===============================\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())