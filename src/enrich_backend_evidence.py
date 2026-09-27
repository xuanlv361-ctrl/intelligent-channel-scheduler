"""后台证据补全

根据后台日志证据（JSON）补全 data/request_records.csv 中指定记录的
渠道信息、价格和费用字段，同时更新 data/model_channel_matrix.csv。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import shutil
from datetime import datetime
from typing import Any

# ── 项目根目录 ─────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

MAIN_RECORDS_CSV = os.path.join(PROJECT_ROOT, "data", "request_records.csv")
MATRIX_CSV = os.path.join(PROJECT_ROOT, "data", "model_channel_matrix.csv")
BACKUP_DIR = os.path.join(PROJECT_ROOT, "output")

# 主表 33 字段
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

# 渠道矩阵字段
MATRIX_FIELDS = [
    "matrix_id",
    "observed_at",
    "channel_id",
    "channel_name",
    "channel_type",
    "local_group",
    "upstream_group",
    "requested_model",
    "actual_model",
    "supports_text",
    "supports_non_stream",
    "supports_stream",
    "temperature_policy",
    "supports_cache",
    "pricing_type",
    "mapping_status",
    "availability_status",
    "evidence_http_status",
    "evidence_request_id",
    "data_source",
    "is_mock",
    "notes",
]

# 目标 record_id
TARGET_RECORD_ID = "R010"

# 目标 request_id
TARGET_REQUEST_ID = "202607210234117798612548268d9d6EwEg56lA"

# 证据文件
EVIDENCE_FILE = os.path.join(PROJECT_ROOT, "evidence", "R010_backend_log.json")


def read_csv(filepath: str, fields: list[str]) -> list[dict[str, str]]:
    """读取 CSV 返回 list[dict]"""
    if not os.path.isfile(filepath):
        return []
    with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return [row for row in reader]


def write_csv(filepath: str, rows: list[dict[str, str]], fields: list[str]) -> None:
    """写 CSV"""
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def backup_csv(filepath: str) -> str | None:
    """备份到 output/，返回备份路径"""
    if not os.path.isfile(filepath):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    basename = os.path.basename(filepath)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(
        BACKUP_DIR, f"{os.path.splitext(basename)[0]}_before_enrich_{timestamp}.csv"
    )
    shutil.copy2(filepath, backup_path)
    print(f"[INFO] 备份已创建: {backup_path}")
    return backup_path


def load_evidence() -> dict[str, Any]:
    """加载证据 JSON"""
    if not os.path.isfile(EVIDENCE_FILE):
        print(f"[ERROR] 证据文件不存在: {EVIDENCE_FILE}", file=sys.stderr)
        sys.exit(1)
    with open(EVIDENCE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def enrich_request_records(
    rows: list[dict[str, str]], evidence: dict[str, Any]
) -> tuple[list[dict[str, str]], int, str]:
    """补全 request_records 数据

    返回 (更新后的行列表, 是否找到并更新(0/1), 记录ID)
    """
    updated_count = 0
    found_rid = ""

    for row in rows:
        rid = (row.get("record_id") or "").strip()
        req_id = (row.get("request_id") or "").strip()

        # 定位：根据 request_id 匹配（不仅是 record_id）
        if req_id != TARGET_REQUEST_ID:
            continue

        found_rid = rid

        # ── 补全渠道/价格字段 ──
        row["channel_id"] = str(evidence["channel_id"])
        row["channel_name"] = str(evidence["channel_name"])
        row["local_group"] = str(evidence["local_group"])
        row["input_price_per_1m"] = str(evidence["input_price_per_1m"])
        row["output_price_per_1m"] = str(evidence["output_price_per_1m"])
        row["cache_read_price_per_1m"] = str(evidence["cache_read_price_per_1m"])
        row["cache_creation_price_per_1m"] = str(evidence["cache_creation_price_per_1m"])
        row["cost"] = f"{evidence['cost']:.10f}".rstrip("0").rstrip(".")
        row["currency"] = str(evidence["currency"])
        row["data_source"] = "live_api+backend_log"

        # ── 保留现有 result / error_category / tokens ──
        # 保持 success_with_warning 和 usage_field_mismatch 不动
        # completion_tokens 和 extended_output_tokens 也保持

        # ── notes 追加 ──
        existing_notes = (row.get("notes") or "").strip()
        append_parts = [
            "请求转换OpenAI Compatible -> Claude Messages",
            "计费模式为上游返回",
            "费用复核通过",
        ]
        append_text = "；".join(append_parts)
        if existing_notes:
            row["notes"] = f"{existing_notes}；{append_text}"
        else:
            row["notes"] = append_text

        updated_count += 1
        break

    return rows, updated_count, found_rid


def find_matrix_row(
    rows: list[dict[str, str]], evidence: dict[str, Any]
) -> tuple[int | None, str]:
    """查找已存在的矩阵行

    Returns (index, matrix_id):
      - index: 找到的行的索引，None 表示需要新增
      - matrix_id: 已有的或新生成的 matrix_id
    """
    channel_id = str(evidence["channel_id"])
    requested_model = str(evidence["requested_model"])
    ev_request_id = str(evidence["request_id"])

    for i, row in enumerate(rows):
        existing_req_id = (row.get("evidence_request_id") or "").strip()
        existing_channel = (row.get("channel_id") or "").strip()
        existing_model = (row.get("requested_model") or "").strip()

        # 幂等判断：相同 request_id 或 相同 channel_id + requested_model
        if existing_req_id == ev_request_id:
            return i, row.get("matrix_id", "").strip()
        if existing_channel == channel_id and existing_model == requested_model:
            return i, row.get("matrix_id", "").strip()

    return None, ""


def get_next_matrix_id(rows: list[dict[str, str]]) -> str:
    """生成下一个 matrix_id（如 MC004）"""
    max_num = 0
    for row in rows:
        mid = (row.get("matrix_id") or "").strip()
        m = re.match(r"MC(\d+)$", mid, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            if num > max_num:
                max_num = num
    return f"MC{max_num + 1:03d}"


def build_matrix_row(evidence: dict[str, Any], matrix_id: str) -> dict[str, str]:
    """构造一条渠道-模型矩阵行"""
    return {
        "matrix_id": matrix_id,
        "observed_at": str(evidence["tested_at"]),
        "channel_id": str(evidence["channel_id"]),
        "channel_name": str(evidence["channel_name"]),
        "channel_type": "DeepSeek",
        "local_group": str(evidence["local_group"]),
        "upstream_group": "pending_confirmation",
        "requested_model": str(evidence["requested_model"]),
        "actual_model": str(evidence["actual_model"]),
        "supports_text": "TRUE",
        "supports_non_stream": "TRUE",
        "supports_stream": "pending_confirmation",
        "temperature_policy": "pending_confirmation",
        "supports_cache": "pending_confirmation",
        "pricing_type": "per_token",
        "mapping_status": "match",
        "availability_status": "available",
        "evidence_http_status": str(evidence["http_status"]),
        "evidence_request_id": str(evidence["request_id"]),
        "data_source": "live_api+backend_log",
        "is_mock": "FALSE",
        "notes": "OpenAI Compatible请求转换为Claude Messages；本次非流式调用成功；缓存与流式能力待单独确认。",
    }


def enrich_matrix(
    rows: list[dict[str, str]], evidence: dict[str, Any]
) -> tuple[list[dict[str, str]], str, str]:
    """补全/新增渠道矩阵

    Returns (更新后的行列表, 操作类型, matrix_id)
    操作类型: 'updated' / 'inserted' / 'skipped'
    """
    idx, existing_mid = find_matrix_row(rows, evidence)

    if idx is not None:
        # 更新已有行
        mid = existing_mid or get_next_matrix_id(rows)
        new_row = build_matrix_row(evidence, mid)
        rows[idx] = new_row
        return rows, "updated", mid
    else:
        # 新增
        mid = get_next_matrix_id(rows)
        new_row = build_matrix_row(evidence, mid)
        rows.append(new_row)
        return rows, "inserted", mid


def enrich(dry_run: bool = False) -> dict[str, Any]:
    """执行补全"""
    # 加载证据
    evidence = load_evidence()

    # ── 1. 补全 request_records ──
    rec_rows = read_csv(MAIN_RECORDS_CSV, MAIN_FIELDS)
    rec_rows, rec_updated, found_rid = enrich_request_records(rec_rows, evidence)

    # ── 2. 补全 model_channel_matrix ──
    matrix_rows = read_csv(MATRIX_CSV, MATRIX_FIELDS)
    matrix_rows, matrix_op, matrix_id = enrich_matrix(matrix_rows, evidence)

    # ── 3. 写文件（非 dry-run） ──
    if not dry_run:
        if rec_updated:
            backup_csv(MAIN_RECORDS_CSV)
            write_csv(MAIN_RECORDS_CSV, rec_rows, MAIN_FIELDS)
            print(f"[INFO] request_records.csv 已更新: {found_rid}")
        else:
            print(f"[WARN] 未找到 request_id={TARGET_REQUEST_ID} 的记录")

        backup_csv(MATRIX_CSV)
        write_csv(MATRIX_CSV, matrix_rows, MATRIX_FIELDS)
        print(f"[INFO] model_channel_matrix.csv 已更新: {matrix_op} {matrix_id}")
    else:
        print("\n========== DRY-RUN 报告 ==========")
        if rec_updated:
            print(f"request_records: 将更新 {found_rid}")
        else:
            print(f"request_records: 未找到匹配记录")
        print(f"model_channel_matrix: 将{matrix_op} {matrix_id}")
        print("文件不会被实际写入。")
        print("==================================\n")

    return {
        "rec_updated": rec_updated,
        "found_rid": found_rid,
        "matrix_op": matrix_op,
        "matrix_id": matrix_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="后台证据补全")
    parser.add_argument("--dry-run", action="store_true", help="仅报告，不写文件")
    args = parser.parse_args(argv)

    result = enrich(dry_run=args.dry_run)

    if not args.dry_run:
        print("========== 补全完成 ==========")
        if result["rec_updated"]:
            print(f"请求记录:    {result['found_rid']} 已补全")
        print(f"渠道矩阵:    {result['matrix_op']} {result['matrix_id']}")
        print("==============================\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())