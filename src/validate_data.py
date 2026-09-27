from __future__ import annotations

import csv
import re
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

REQUEST_FILE = DATA_DIR / "request_records.csv"
DICTIONARY_FILE = DATA_DIR / "metric_dictionary.csv"
MATRIX_FILE = DATA_DIR / "model_channel_matrix.csv"
SNAPSHOT_FILE = DATA_DIR / "metrics_snapshot_v1.csv"
SNAPSHOT_DICTIONARY_FILE = DATA_DIR / "metrics_snapshot_dictionary.csv"
TAXONOMY_FILE = DATA_DIR / "error_taxonomy.csv"
FIRST_WEEK_FILE = DATA_DIR / "first_week_source_records.csv"

messages: list[tuple[str, str, str, str]] = []


def add_message(level: str, table: str, record_id: str, message: str) -> None:
    messages.append((level, table, record_id, message))


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        raise FileNotFoundError(f"找不到文件：{path}")

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)

        headers = [
            header.strip()
            for header in (reader.fieldnames or [])
            if header and header.strip()
        ]

        rows = []
        for raw_row in reader:
            row = {
                key.strip(): value.strip() if isinstance(value, str) else value
                for key, value in raw_row.items()
                if key and key.strip()
            }
            rows.append(row)

    return headers, rows


def is_blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def to_int(value: object) -> int | None:
    if is_blank(value):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def to_float(value: object) -> float | None:
    if is_blank(value):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_bool(value: object) -> bool | None:
    if is_blank(value):
        return None

    normalized = str(value).strip().lower()

    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def valid_datetime(value: str) -> bool:
    formats = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
    ]

    for date_format in formats:
        try:
            datetime.strptime(value, date_format)
            return True
        except ValueError:
            continue

    return False


def contains_secret(value: str) -> bool:
    patterns = [
        r"sk-[A-Za-z0-9_-]{10,}",
        r"Authorization\s*:\s*Bearer\s+\S+",
    ]
    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def validate_request_records(
    headers: list[str], rows: list[dict[str, str]]
) -> None:
    table = "request_records"

    required_headers = {
        "record_id",
        "tested_at",
        "environment",
        "request_path",
        "requested_model",
        "stream",
        "http_status",
        "result",
        "data_source",
        "is_mock",
    }

    missing_headers = required_headers - set(headers)
    for header in sorted(missing_headers):
        add_message("ERROR", table, "HEADER", f"缺少字段：{header}")

    required_values = [
        "record_id",
        "tested_at",
        "environment",
        "request_path",
        "requested_model",
        "stream",
        "http_status",
        "result",
        "data_source",
        "is_mock",
    ]

    integer_fields = [
        "channel_id",
        "http_status",
        "latency_ms",
        "ttft_ms",
        "raw_input_tokens",
        "cache_creation_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "extended_output_tokens",
    ]

    decimal_fields = [
        "input_price_per_1m",
        "output_price_per_1m",
        "cache_read_price_per_1m",
        "cache_creation_price_per_1m",
        "cost",
    ]

    allowed_results = {
        "pass",
        "partial_pass",
        "fail",
        "success_with_warning",
    }

    seen_record_ids: set[str] = set()
    seen_request_ids: set[str] = set()

    for row_number, row in enumerate(rows, start=2):
        record_id = row.get("record_id") or f"ROW-{row_number}"

        for field in required_values:
            if is_blank(row.get(field)):
                add_message(
                    "ERROR",
                    table,
                    record_id,
                    f"必填字段为空：{field}",
                )

        if record_id in seen_record_ids:
            add_message("ERROR", table, record_id, "record_id重复")
        seen_record_ids.add(record_id)

        tested_at = row.get("tested_at", "")
        if tested_at and not valid_datetime(tested_at):
            add_message(
                "ERROR",
                table,
                record_id,
                f"测试时间格式不正确：{tested_at}",
            )

        request_path = row.get("request_path", "")
        if request_path and not request_path.startswith("/"):
            add_message(
                "ERROR",
                table,
                record_id,
                "request_path必须以/开头",
            )

        stream = parse_bool(row.get("stream"))
        if stream is None:
            add_message(
                "ERROR",
                table,
                record_id,
                "stream只能填写true或false",
            )

        is_mock = parse_bool(row.get("is_mock"))
        if is_mock is None:
            add_message(
                "ERROR",
                table,
                record_id,
                "is_mock只能填写true或false",
            )

        for field in integer_fields:
            value = row.get(field)
            if not is_blank(value):
                parsed = to_int(value)
                if parsed is None:
                    add_message(
                        "ERROR",
                        table,
                        record_id,
                        f"{field}不是有效整数：{value}",
                    )
                elif parsed < 0:
                    add_message(
                        "ERROR",
                        table,
                        record_id,
                        f"{field}不能为负数",
                    )

        for field in decimal_fields:
            value = row.get(field)
            if not is_blank(value):
                parsed = to_float(value)
                if parsed is None:
                    add_message(
                        "ERROR",
                        table,
                        record_id,
                        f"{field}不是有效数字：{value}",
                    )
                elif parsed < 0:
                    add_message(
                        "ERROR",
                        table,
                        record_id,
                        f"{field}不能为负数",
                    )

        http_status = to_int(row.get("http_status"))
        if http_status is not None and not 100 <= http_status <= 599:
            add_message(
                "ERROR",
                table,
                record_id,
                f"HTTP状态码超出范围：{http_status}",
            )

        prompt_tokens = to_int(row.get("prompt_tokens"))
        completion_tokens = to_int(row.get("completion_tokens"))
        total_tokens = to_int(row.get("total_tokens"))

        if None not in (prompt_tokens, completion_tokens, total_tokens):
            expected_total = prompt_tokens + completion_tokens
            if expected_total != total_tokens:
                add_message(
                    "ERROR",
                    table,
                    record_id,
                    f"Token总数错误：{prompt_tokens}+"
                    f"{completion_tokens}!={total_tokens}",
                )

        extended_output = to_int(row.get("extended_output_tokens"))
        if (
            extended_output is not None
            and completion_tokens is not None
            and extended_output != completion_tokens
        ):
            add_message(
                "WARNING",
                table,
                record_id,
                f"扩展output_tokens={extended_output}，"
                f"但completion_tokens={completion_tokens}",
            )

        if stream is True and is_blank(row.get("ttft_ms")):
            add_message(
                "WARNING",
                table,
                record_id,
                "流式请求未记录ttft_ms",
            )

        if stream is False and not is_blank(row.get("ttft_ms")):
            add_message(
                "WARNING",
                table,
                record_id,
                "非流式请求不应填写ttft_ms",
            )

        requested_model = row.get("requested_model", "")
        actual_model = row.get("actual_model", "")

        if http_status is not None and 200 <= http_status < 300:
            if is_blank(actual_model):
                add_message(
                    "WARNING",
                    table,
                    record_id,
                    "成功请求缺少actual_model",
                )
            elif requested_model != actual_model:
                add_message(
                    "WARNING",
                    table,
                    record_id,
                    f"模型不一致：{requested_model} -> {actual_model}",
                )

        if 200 <= (http_status or 0) < 300:
            if is_blank(row.get("channel_id")):
                add_message(
                    "WARNING",
                    table,
                    record_id,
                    "成功请求缺少channel_id",
                )
            if is_blank(row.get("request_id")):
                add_message(
                    "WARNING",
                    table,
                    record_id,
                    "成功请求缺少request_id",
                )

        request_id = row.get("request_id", "")
        if request_id:
            if request_id in seen_request_ids:
                add_message(
                    "ERROR",
                    table,
                    record_id,
                    "request_id重复",
                )
            seen_request_ids.add(request_id)

        cost = to_float(row.get("cost"))
        currency = row.get("currency", "")

        if cost is not None and not currency:
            add_message(
                "ERROR",
                table,
                record_id,
                "存在费用但未填写currency",
            )

        if currency and currency not in {"CNY", "USD"}:
            add_message(
                "ERROR",
                table,
                record_id,
                f"不支持的币种：{currency}",
            )

        input_price = to_float(row.get("input_price_per_1m"))
        output_price = to_float(row.get("output_price_per_1m"))

        if (
            cost is not None
            and input_price is not None
            and output_price is not None
            and prompt_tokens is not None
            and completion_tokens is not None
        ):
            expected_cost = (
                prompt_tokens * input_price
                + completion_tokens * output_price
            ) / 1_000_000

            if abs(expected_cost - cost) > 0.00001:
                add_message(
                    "WARNING",
                    table,
                    record_id,
                    f"费用计算可能不一致：记录={cost:.6f}，"
                    f"按输入输出单价计算={expected_cost:.6f}",
                )

        result = row.get("result", "")
        if result and result not in allowed_results:
            add_message(
                "ERROR",
                table,
                record_id,
                f"未知result：{result}",
            )

        if result == "pass" and row.get("error_category"):
            add_message(
                "WARNING",
                table,
                record_id,
                "result=pass但存在error_category",
            )

        combined_text = " ".join(str(value) for value in row.values())
        if contains_secret(combined_text):
            add_message(
                "ERROR",
                table,
                record_id,
                "记录中疑似包含API Key或完整鉴权头",
            )


def validate_metric_dictionary(
    headers: list[str], rows: list[dict[str, str]], request_headers: list[str]
) -> None:
    table = "metric_dictionary"

    expected_headers = {
        "field_name",
        "chinese_name",
        "data_type",
        "unit",
        "required",
        "data_source",
        "nullable_condition",
        "validation_rule",
        "example",
    }

    missing_headers = expected_headers - set(headers)
    for header in sorted(missing_headers):
        add_message("ERROR", table, "HEADER", f"缺少字段：{header}")

    seen_fields: set[str] = set()

    for row_number, row in enumerate(rows, start=2):
        field_name = row.get("field_name") or f"ROW-{row_number}"

        if field_name in seen_fields:
            add_message(
                "ERROR",
                table,
                field_name,
                "field_name重复",
            )
        seen_fields.add(field_name)

        for field in expected_headers:
            if is_blank(row.get(field)):
                add_message(
                    "ERROR",
                    table,
                    field_name,
                    f"字典字段为空：{field}",
                )

    for request_field in request_headers:
        if request_field not in seen_fields:
            add_message(
                "ERROR",
                table,
                request_field,
                "request_records中的字段未在指标字典中定义",
            )


def validate_model_channel_matrix(
    headers: list[str], rows: list[dict[str, str]]
) -> None:
    table = "model_channel_matrix"

    required_headers = {
        "matrix_id",
        "observed_at",
        "channel_id",
        "channel_name",
        "channel_type",
        "local_group",
        "upstream_group",
        "requested_model",
        "supports_text",
        "availability_status",
        "evidence_http_status",
        "data_source",
        "is_mock",
        "notes",
    }

    missing_headers = required_headers - set(headers)
    for header in sorted(missing_headers):
        add_message("ERROR", table, "HEADER", f"缺少字段：{header}")

    tri_state_fields = [
        "supports_text",
        "supports_non_stream",
        "supports_stream",
        "supports_cache",
    ]

    allowed_tri_state = {
        "true",
        "false",
        "pending_confirmation",
    }

    seen_ids: set[str] = set()

    for row_number, row in enumerate(rows, start=2):
        matrix_id = row.get("matrix_id") or f"ROW-{row_number}"

        if matrix_id in seen_ids:
            add_message("ERROR", table, matrix_id, "matrix_id重复")
        seen_ids.add(matrix_id)

        for field in required_headers:
            if is_blank(row.get(field)):
                add_message(
                    "ERROR",
                    table,
                    matrix_id,
                    f"必填字段为空：{field}",
                )

        if row.get("observed_at") and not valid_datetime(row["observed_at"]):
            add_message(
                "ERROR",
                table,
                matrix_id,
                f"observed_at格式错误：{row['observed_at']}",
            )

        for field in tri_state_fields:
            value = row.get(field, "").lower()
            if value not in allowed_tri_state:
                add_message(
                    "ERROR",
                    table,
                    matrix_id,
                    f"{field}必须为true、false或pending_confirmation",
                )

        if parse_bool(row.get("is_mock")) is None:
            add_message(
                "ERROR",
                table,
                matrix_id,
                "is_mock只能填写true或false",
            )

        requested_model = row.get("requested_model", "")
        actual_model = row.get("actual_model", "")
        mapping_status = row.get("mapping_status", "")

        if (
            requested_model
            and actual_model
            and requested_model != actual_model
            and mapping_status != "mismatch"
        ):
            add_message(
                "WARNING",
                table,
                matrix_id,
                "请求模型与实际模型不一致，但mapping_status不是mismatch",
            )

        if (
            requested_model
            and actual_model
            and requested_model == actual_model
            and mapping_status == "mismatch"
        ):
            add_message(
                "WARNING",
                table,
                matrix_id,
                "请求模型与实际模型一致，但mapping_status=mismatch",
            )

        combined_text = " ".join(str(value) for value in row.values())
        if contains_secret(combined_text):
            add_message(
                "ERROR",
                table,
                matrix_id,
                "矩阵中疑似包含API Key或完整鉴权头",
            )


def validate_week2_files(snapshot_headers, snapshot_rows, snapshot_dictionary_rows,
                         taxonomy_headers, taxonomy_rows, first_week_rows,
                         request_rows) -> None:
    expected = {"snapshot_version","updated_at","environment","requested_model","actual_model","channel_id","channel_name","local_group","sample_size","success_count","failure_count","excluded_count","success_rate","latency_avg_ms","latency_p50_ms","ttft_avg_ms","input_price_per_1m","output_price_per_1m","cache_read_price_per_1m","cache_creation_price_per_1m","currency","price_version","price_observed_at","data_source","is_mock","confidence_level","notes"}
    for field in expected - set(snapshot_headers): add_message("ERROR", "metrics_snapshot", "HEADER", f"missing: {field}")
    for index, row in enumerate(snapshot_rows, 2):
        rid = f"ROW-{index}"
        if not row.get("snapshot_version"): add_message("ERROR", "metrics_snapshot", rid, "snapshot_version is blank")
        if not valid_datetime(row.get("updated_at", "")): add_message("ERROR", "metrics_snapshot", rid, "invalid updated_at")
        nums = {f: to_int(row.get(f)) for f in ("sample_size","success_count","failure_count","excluded_count")}
        for field, value in nums.items():
            if value is None or value < 0: add_message("ERROR", "metrics_snapshot", rid, f"invalid {field}")
        if all(nums[x] is not None for x in ("sample_size","success_count","failure_count")) and nums["success_count"] + nums["failure_count"] > nums["sample_size"]: add_message("ERROR", "metrics_snapshot", rid, "counts exceed sample_size")
        denominator = (nums["success_count"] or 0) + (nums["failure_count"] or 0)
        rate = to_float(row.get("success_rate"))
        if denominator == 0 and not is_blank(row.get("success_rate")): add_message("ERROR", "metrics_snapshot", rid, "zero denominator requires blank success_rate")
        if rate is not None and not 0 <= rate <= 1: add_message("ERROR", "metrics_snapshot", rid, "success_rate out of range")
        if row.get("price_version") != "pending_confirmation": add_message("ERROR", "metrics_snapshot", rid, "price_version must be pending_confirmation")
        if (nums["sample_size"] or 0) <= 5 and row.get("confidence_level") != "low": add_message("ERROR", "metrics_snapshot", rid, "small sample must have low confidence")
    dictionary_fields = {r.get("field_name", "") for r in snapshot_dictionary_rows}
    for field in expected - dictionary_fields: add_message("ERROR", "metrics_snapshot_dictionary", field, "field not defined")
    required_taxonomy = {"error_category","chinese_name","error_layer","typical_http_status","channel_fault","retryable","fallback_allowed","judgement_basis","example","notes"}
    for field in required_taxonomy - set(taxonomy_headers): add_message("ERROR", "error_taxonomy", "HEADER", f"missing: {field}")
    names, allowed = set(), {"TRUE","FALSE","pending_confirmation"}
    for row in taxonomy_rows:
        category = row.get("error_category", "")
        if category in names: add_message("ERROR", "error_taxonomy", category, "duplicate category")
        names.add(category)
        for field in ("channel_fault","retryable","fallback_allowed"):
            if row.get(field) not in allowed: add_message("ERROR", "error_taxonomy", category, f"invalid {field}")
    historical = {"content_requirement_mismatch","unverified_data","count_mismatch","safety_coverage_insufficient","reasoning_content_exposed"}
    for row in request_rows:
        for category in filter(None, row.get("error_category", "").split(";")):
            if category not in names | historical: add_message("ERROR", "request_records", row.get("record_id", ""), f"unknown error_category: {category}")
    ids = [r.get("record_id", "") for r in first_week_rows]
    if len(ids) != 6 or set(ids) != {f"R{i:03d}" for i in range(2, 8)}: add_message("ERROR", "first_week_source_records", "ROWS", "must contain exactly R002-R007")
    for table, rows in (("metrics_snapshot", snapshot_rows),("metrics_snapshot_dictionary", snapshot_dictionary_rows),("error_taxonomy", taxonomy_rows),("first_week_source_records", first_week_rows)):
        for index, row in enumerate(rows, 2):
            if contains_secret(" ".join(str(v) for v in row.values())): add_message("ERROR", table, f"ROW-{index}", "possible secret")


def print_report() -> int:
    error_count = sum(1 for level, *_ in messages if level == "ERROR")
    warning_count = sum(1 for level, *_ in messages if level == "WARNING")

    print("\n========== 数据校验报告 ==========\n")

    if not messages:
        print("未发现错误或警告。")
    else:
        for level, table, record_id, message in messages:
            print(f"[{level}] [{table}] [{record_id}] {message}")

    print("\n-------------- 汇总 --------------")
    print(f"ERROR：{error_count}")
    print(f"WARNING：{warning_count}")

    if error_count == 0:
        print("结论：结构校验通过，可以进入下一阶段。")
        return 0

    print("结论：存在ERROR，请修复后重新运行。")
    return 1


def main() -> None:
    try:
        request_headers, request_rows = read_csv(REQUEST_FILE)
        dictionary_headers, dictionary_rows = read_csv(DICTIONARY_FILE)
        matrix_headers, matrix_rows = read_csv(MATRIX_FILE)
        snapshot_headers, snapshot_rows = read_csv(SNAPSHOT_FILE)
        snapshot_dictionary_headers, snapshot_dictionary_rows = read_csv(SNAPSHOT_DICTIONARY_FILE)
        taxonomy_headers, taxonomy_rows = read_csv(TAXONOMY_FILE)
        first_week_headers, first_week_rows = read_csv(FIRST_WEEK_FILE)
    except Exception as error:
        print(f"[FATAL] 文件读取失败：{error}")
        sys.exit(2)

    print(f"request_records：{len(request_rows)}条")
    print(f"metric_dictionary：{len(dictionary_rows)}条")
    print(f"model_channel_matrix：{len(matrix_rows)}条")

    validate_request_records(request_headers, request_rows)
    validate_metric_dictionary(
        dictionary_headers,
        dictionary_rows,
        request_headers,
    )
    validate_model_channel_matrix(matrix_headers, matrix_rows)
    print(f"metrics_snapshot: {len(snapshot_rows)} rows x {len(snapshot_headers)} columns")
    print(f"metrics_snapshot_dictionary: {len(snapshot_dictionary_rows)} rows x {len(snapshot_dictionary_headers)} columns")
    print(f"error_taxonomy: {len(taxonomy_rows)} rows x {len(taxonomy_headers)} columns")
    print(f"first_week_source_records: {len(first_week_rows)} rows x {len(first_week_headers)} columns")
    validate_week2_files(snapshot_headers, snapshot_rows, snapshot_dictionary_rows,
                         taxonomy_headers, taxonomy_rows, first_week_rows, request_rows)

    exit_code = print_report()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
