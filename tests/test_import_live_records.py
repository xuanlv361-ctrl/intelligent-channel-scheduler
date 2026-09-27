"""单元测试：import_live_records，完全 Mock，不访问真实网络或正式文件"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import import_live_records as ilr


# ── 辅助 ─────────────────────────────────────────────────────────────

SAMPLE_LIVE_CSV_CONTENT = """tested_at,environment,request_path,requested_model,actual_model,stream,http_status,latency_ms,finish_reason,prompt_tokens,completion_tokens,total_tokens,extended_output_tokens,request_id,result,error_category,data_source,is_mock,notes
2026-07-21 10:34:12,uat_cn,/v1/chat/completions,deepseek-v4-flash,deepseek-v4-flash,FALSE,200,1056,stop,12,32,44,0,req-new-001,pass,,live_api,False,
2026-07-21 10:35:00,uat_cn,/v1/chat/completions,deepseek-v4-flash,,FALSE,401,470,,0,0,0,0,req-auth-fail,fail,authentication_failed,live_api,False,
"""

SAMPLE_MAIN_CSV_HEADER = (
    "record_id,tested_at,environment,request_path,requested_model,actual_model,"
    "channel_id,channel_name,local_group,stream,http_status,latency_ms,ttft_ms,"
    "finish_reason,stream_status,raw_input_tokens,cache_creation_tokens,"
    "prompt_tokens,completion_tokens,total_tokens,extended_output_tokens,"
    "input_price_per_1m,output_price_per_1m,cache_read_price_per_1m,"
    "cache_creation_price_per_1m,cost,currency,request_id,result,"
    "error_category,data_source,is_mock,notes"
)

# 33 字段，用列表确保精确
SAMPLE_MAIN_CSV_FIELDS = [
    "record_id", "tested_at", "environment", "request_path", "requested_model",
    "actual_model", "channel_id", "channel_name", "local_group", "stream",
    "http_status", "latency_ms", "ttft_ms", "finish_reason", "stream_status",
    "raw_input_tokens", "cache_creation_tokens", "prompt_tokens", "completion_tokens",
    "total_tokens", "extended_output_tokens", "input_price_per_1m", "output_price_per_1m",
    "cache_read_price_per_1m", "cache_creation_price_per_1m", "cost", "currency",
    "request_id", "result", "error_category", "data_source", "is_mock", "notes",
]

SAMPLE_MAIN_CSV_VALS: list[list[str]] = [
    ["R001", "2026/7/20", "uat_cn", "/v1/chat/completions", "claude-opus-4-8",
     "claude-opus-4-8", "52", "xl", "default", "TRUE", "200", "121430", "", "stop",
     "done", "993", "37027", "38020", "8893", "46913", "0", "36.5", "182.5", "3.65",
     "", "3.010702", "CNY", "req-old-001", "pass", "", "apifox", "FALSE", "test note"],
    ["R002", "2026/7/21", "uat_cn", "/v1/chat/completions", "gpt-4",
     "", "", "some_channel", "", "TRUE", "200", "1000", "", "stop",
     "", "10", "20", "30", "40", "70", "10", "1", "2", "0.1",
     "", "0.05", "USD", "req-old-002", "pass", "", "live_api", "FALSE", ""],
]

SAMPLE_MAIN_CSV_ROWS = [",".join(vals) for vals in SAMPLE_MAIN_CSV_VALS]


def make_live_csv(tmp_path: Path, content: str = SAMPLE_LIVE_CSV_CONTENT) -> str:
    p = tmp_path / "live_request_records.csv"
    p.write_text(content, encoding="utf-8-sig")
    return str(p)


def make_main_csv(tmp_path: Path, rows: list[str] | None = None) -> str:
    if rows is None:
        rows = SAMPLE_MAIN_CSV_ROWS
    p = tmp_path / "request_records.csv"
    lines = [SAMPLE_MAIN_CSV_HEADER] + rows
    p.write_text("\n".join(lines), encoding="utf-8-sig")
    return str(p)


# ═══════════════════════════════════════════════════════════════════

class TestReadFunctions(unittest.TestCase):
    """读取函数测试"""

    def test_read_main_records_empty_when_file_missing(self) -> None:
        with patch.object(ilr, "MAIN_RECORDS_CSV", new="/nonexistent/path.csv"):
            rows = ilr.read_main_records()
        self.assertEqual(rows, [])

    def test_read_main_records_parses_correctly(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            main_csv = make_main_csv(tmp)
            with patch.object(ilr, "MAIN_RECORDS_CSV", new=main_csv):
                rows = ilr.read_main_records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["record_id"], "R001")
        self.assertEqual(rows[1]["record_id"], "R002")

    def test_read_live_records_parses(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            live_csv = make_live_csv(tmp)
            with patch.object(ilr, "LIVE_RECORDS_CSV", new=live_csv):
                rows = ilr.read_live_records()
        self.assertEqual(len(rows), 2)


class TestRecordIdGeneration(unittest.TestCase):
    """record_id 递增测试"""

    def test_get_next_from_empty(self) -> None:
        self.assertEqual(ilr.get_next_record_id([]), "R001")

    def test_get_next_from_existing(self) -> None:
        existing = [
            {"record_id": "R001"},
            {"record_id": "R002"},
            {"record_id": "R007"},
        ]
        self.assertEqual(ilr.get_next_record_id(existing), "R008")

    def test_get_next_from_single(self) -> None:
        existing = [{"record_id": "R001"}]
        self.assertEqual(ilr.get_next_record_id(existing), "R002")

    def test_get_next_handles_no_record_id(self) -> None:
        existing: list[dict[str, str]] = [{"record_id": ""}, {"other": "val"}]
        self.assertEqual(ilr.get_next_record_id(existing), "R001")


class TestExistingRequestIds(unittest.TestCase):
    """request_id 收集测试"""

    def test_empty_list(self) -> None:
        self.assertEqual(ilr.existing_request_ids([]), set())

    def test_collects_non_empty(self) -> None:
        rows = [
            {"request_id": "rid-001"},
            {"request_id": "rid-002"},
            {"request_id": ""},
        ]
        s = ilr.existing_request_ids(rows)
        self.assertIn("rid-001", s)
        self.assertIn("rid-002", s)
        self.assertNotIn("", s)

    def test_ignores_empty(self) -> None:
        rows = [{"request_id": ""}, {"request_id": ""}]
        self.assertEqual(ilr.existing_request_ids(rows), set())


class TestAutoDetectResult(unittest.TestCase):
    """智能分类检测测试"""

    def test_pass_kept_when_tokens_match(self) -> None:
        row = {"result": "pass", "completion_tokens": "32", "extended_output_tokens": "32", "error_category": "", "notes": ""}
        r, ec, n = ilr.auto_detect_result(row)
        self.assertEqual(r, "pass")
        self.assertEqual(ec, "")
        self.assertEqual(n, "")

    def test_mismatch_sets_warning(self) -> None:
        row = {"result": "pass", "completion_tokens": "32", "extended_output_tokens": "0", "error_category": "", "notes": ""}
        r, ec, n = ilr.auto_detect_result(row)
        self.assertEqual(r, "success_with_warning")
        self.assertEqual(ec, "usage_field_mismatch")
        self.assertIn("completion_tokens=32", n)
        self.assertIn("extended_output_tokens=0", n)

    def test_mismatch_zero_valid_not_missing(self) -> None:
        row = {"result": "pass", "completion_tokens": "0", "extended_output_tokens": "5", "error_category": "", "notes": ""}
        r, ec, n = ilr.auto_detect_result(row)
        self.assertEqual(r, "success_with_warning")
        self.assertEqual(ec, "usage_field_mismatch")

    def test_fail_preserved(self) -> None:
        row = {"result": "fail", "completion_tokens": "0", "extended_output_tokens": "0", "error_category": "timeout", "notes": "conn lost"}
        r, ec, n = ilr.auto_detect_result(row)
        self.assertEqual(r, "fail")
        self.assertEqual(ec, "timeout")
        self.assertEqual(n, "conn lost")

    def test_empty_tokens_no_warning(self) -> None:
        row = {"result": "pass", "completion_tokens": "", "extended_output_tokens": "0", "error_category": "", "notes": ""}
        r, ec, n = ilr.auto_detect_result(row)
        self.assertEqual(r, "pass")
        self.assertEqual(ec, "")

    def test_missing_output_tokens_no_warning(self) -> None:
        row = {"result": "pass", "completion_tokens": "32", "extended_output_tokens": "", "error_category": "", "notes": ""}
        r, ec, n = ilr.auto_detect_result(row)
        self.assertEqual(r, "pass")

    def test_existing_warning_preserved(self) -> None:
        row = {"result": "success_with_warning", "completion_tokens": "32", "extended_output_tokens": "0", "error_category": "usage_field_mismatch", "notes": "existing note"}
        r, ec, n = ilr.auto_detect_result(row)
        self.assertEqual(r, "success_with_warning")
        self.assertEqual(ec, "usage_field_mismatch")
        self.assertEqual(n, "existing note")


class TestErrorCategoryMapping(unittest.TestCase):
    """旧错误分类映射测试"""

    def test_authentication_failed_mapped(self) -> None:
        self.assertEqual(ilr._normalize_error_category("authentication_failed"), "user_authentication_error")

    def test_bad_request_mapped(self) -> None:
        self.assertEqual(ilr._normalize_error_category("bad_request"), "request_parameter_error")

    def test_rate_limited_mapped(self) -> None:
        self.assertEqual(ilr._normalize_error_category("rate_limited"), "rate_limit")

    def test_service_unavailable_mapped(self) -> None:
        self.assertEqual(ilr._normalize_error_category("service_unavailable"), "platform_routing_error")

    def test_unknown_category_preserved(self) -> None:
        self.assertEqual(ilr._normalize_error_category("timeout"), "timeout")

    def test_empty_category_preserved(self) -> None:
        self.assertEqual(ilr._normalize_error_category(""), "")


class TestHttpSuccessCheck(unittest.TestCase):
    """HTTP 状态码成功判断测试"""

    def test_200_is_success(self) -> None:
        self.assertTrue(ilr._is_http_success("200"))

    def test_201_is_success(self) -> None:
        self.assertTrue(ilr._is_http_success("201"))

    def test_401_is_not_success(self) -> None:
        self.assertFalse(ilr._is_http_success("401"))

    def test_503_is_not_success(self) -> None:
        self.assertFalse(ilr._is_http_success("503"))

    def test_empty_is_not_success(self) -> None:
        self.assertFalse(ilr._is_http_success(""))

    def test_2xx_pattern(self) -> None:
        self.assertFalse(ilr._is_http_success("2"))  # too short, but len>=1 & starts with '2'


class TestConvertToMainFormat(unittest.TestCase):
    """33 字段转换测试"""

    def test_success_record_preserves_tokens(self) -> None:
        """200 记录：保留所有 token 字段，warning 自动检测"""
        live_row = {
            "tested_at": "2026-07-21 10:34:12",
            "environment": "uat_cn",
            "request_path": "/v1/chat/completions",
            "requested_model": "deepseek-v4-flash",
            "actual_model": "deepseek-v4-flash",
            "stream": "FALSE",
            "http_status": "200",
            "latency_ms": "1056",
            "finish_reason": "stop",
            "prompt_tokens": "12",
            "completion_tokens": "32",
            "total_tokens": "44",
            "extended_output_tokens": "0",
            "request_id": "req-new-001",
            "result": "pass",
            "error_category": "",
            "data_source": "live_api",
            "is_mock": "False",
            "notes": "",
        }
        rec = ilr.convert_to_main_format(live_row, "R008")
        # 33 字段
        self.assertEqual(len(rec), 33)
        # record_id
        self.assertEqual(rec["record_id"], "R008")
        # 空字段
        for field in ilr.EMPTY_FIELDS:
            self.assertEqual(rec[field], "", f"字段 {field} 应为空字符串")
        # 实时字段保留
        self.assertEqual(rec["requested_model"], "deepseek-v4-flash")
        self.assertEqual(rec["http_status"], "200")
        self.assertEqual(rec["actual_model"], "deepseek-v4-flash")
        # is_mock 规范化 (False → FALSE)
        self.assertEqual(rec["is_mock"], "FALSE")
        # 自动检测：completion_tokens=32 != extended_output_tokens=0 → warning
        self.assertEqual(rec["result"], "success_with_warning")
        self.assertEqual(rec["error_category"], "usage_field_mismatch")
        # Token 字段保留（2xx 不清空）
        self.assertEqual(rec["prompt_tokens"], "12")
        self.assertEqual(rec["completion_tokens"], "32")
        self.assertEqual(rec["total_tokens"], "44")
        self.assertEqual(rec["extended_output_tokens"], "0")
        self.assertEqual(rec["finish_reason"], "stop")

    def test_failure_record_clears_tokens(self) -> None:
        """401 记录：清空 token 字段，分类映射"""
        live_row = {
            "tested_at": "2026-07-21 10:35:00",
            "environment": "uat_cn",
            "request_path": "/v1/chat/completions",
            "requested_model": "deepseek-v4-flash",
            "actual_model": "",
            "stream": "FALSE",
            "http_status": "401",
            "latency_ms": "470",
            "finish_reason": "",
            "prompt_tokens": "0",
            "completion_tokens": "0",
            "total_tokens": "0",
            "extended_output_tokens": "0",
            "request_id": "req-auth-fail",
            "result": "fail",
            "error_category": "authentication_failed",
            "data_source": "live_api",
            "is_mock": "False",
            "notes": "",
        }
        rec = ilr.convert_to_main_format(live_row, "R009")
        self.assertEqual(rec["record_id"], "R009")
        self.assertEqual(rec["result"], "fail")
        # 分类映射：authentication_failed → user_authentication_error
        self.assertEqual(rec["error_category"], "user_authentication_error")
        # 保留字段
        self.assertEqual(rec["http_status"], "401")
        self.assertEqual(rec["latency_ms"], "470")
        self.assertEqual(rec["request_id"], "req-auth-fail")
        self.assertEqual(rec["data_source"], "live_api")
        self.assertEqual(rec["is_mock"], "FALSE")
        # 清空字段（非 2xx）
        for fld in ilr.FAILURE_EMPTY_FIELDS:
            self.assertEqual(rec[fld], "", f"字段 {fld} 应为空字符串")

    def test_all_error_categories_mapped(self) -> None:
        """所有旧错误分类映射验证"""
        cases = [
            ("authentication_failed", "user_authentication_error"),
            ("bad_request", "request_parameter_error"),
            ("rate_limited", "rate_limit"),
            ("service_unavailable", "platform_routing_error"),
        ]
        for raw, expected in cases:
            live_row = {
                "tested_at": "2026-07-21 10:00:00",
                "environment": "uat_cn",
                "request_path": "/v1/chat/completions",
                "requested_model": "m",
                "actual_model": "",
                "stream": "FALSE",
                "http_status": "400",
                "latency_ms": "100",
                "finish_reason": "",
                "prompt_tokens": "0",
                "completion_tokens": "0",
                "total_tokens": "0",
                "extended_output_tokens": "0",
                "request_id": f"req-{raw}",
                "result": "fail",
                "error_category": raw,
                "data_source": "live_api",
                "is_mock": "False",
                "notes": "",
            }
            rec = ilr.convert_to_main_format(live_row, "R999")
            self.assertEqual(rec["error_category"], expected, f"{raw} → {expected}")

    def test_is_mock_true_preserved(self) -> None:
        live_row = {
            "tested_at": "2026-07-21 10:34:12",
            "environment": "uat_cn",
            "request_path": "/v1/chat/completions",
            "requested_model": "m",
            "actual_model": "",
            "stream": "FALSE",
            "http_status": "",
            "latency_ms": "",
            "finish_reason": "",
            "prompt_tokens": "",
            "completion_tokens": "",
            "total_tokens": "",
            "extended_output_tokens": "",
            "request_id": "req-mock",
            "result": "pass",
            "error_category": "",
            "data_source": "live_api",
            "is_mock": True,
            "notes": "",
        }
        rec = ilr.convert_to_main_format(live_row, "R009")
        # TRUE (bool True → "TRUE")
        self.assertEqual(rec["is_mock"], "TRUE")


class TestImportDryRun(unittest.TestCase):
    """dry-run 模式：不写文件"""

    def test_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            live_csv = make_live_csv(tmp)
            main_csv = make_main_csv(tmp)

            with (
                patch.object(ilr, "LIVE_RECORDS_CSV", new=live_csv),
                patch.object(ilr, "MAIN_RECORDS_CSV", new=main_csv),
                patch.object(ilr, "BACKUP_FILE", new=str(tmp / "backup.csv")),
            ):
                result = ilr.import_live(dry_run=True)

            # 不产生 backup
            self.assertFalse(os.path.exists(str(tmp / "backup.csv")))
            # 主文件不变
            self.assertEqual(len(result["main_rows"]), 2)
            self.assertEqual(result["new_count"], 2)  # 两条新记录


class TestImportRealRun(unittest.TestCase):
    """真正导入测试"""

    def test_import_adds_records(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            live_csv = make_live_csv(tmp)
            main_csv = make_main_csv(tmp)
            backup_path = str(tmp / "backup.csv")

            with (
                patch.object(ilr, "LIVE_RECORDS_CSV", new=live_csv),
                patch.object(ilr, "MAIN_RECORDS_CSV", new=main_csv),
                patch.object(ilr, "BACKUP_FILE", new=backup_path),
            ):
                result = ilr.import_live(dry_run=False)
                # 主文件被追加（在 patch 上下文内读取）
                updated = ilr.read_main_records()

            # 备份已创建
            self.assertTrue(os.path.exists(backup_path))
            self.assertEqual(result["new_count"], 2)
            self.assertEqual(result["skip_count"], 0)
            # 生成的 ID
            self.assertEqual(len(result["generated_ids"]), 2)
            # R001, R002 已有 → R003, R004
            self.assertEqual(result["generated_ids"][0], "R003")
            self.assertEqual(result["generated_ids"][1], "R004")

            self.assertEqual(len(updated), 4)

    def test_import_normailzes_records(self) -> None:
        """导入后的记录规范化验证"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            live_csv = make_live_csv(tmp)
            main_csv = make_main_csv(tmp)
            backup_path = str(tmp / "backup.csv")

            with (
                patch.object(ilr, "LIVE_RECORDS_CSV", new=live_csv),
                patch.object(ilr, "MAIN_RECORDS_CSV", new=main_csv),
                patch.object(ilr, "BACKUP_FILE", new=backup_path),
            ):
                result = ilr.import_live(dry_run=False)
                converted = result["converted"]

            # 第 1 条：200 成功记录 → success_with_warning, usage_field_mismatch
            rec_200 = converted[0]
            self.assertEqual(rec_200["http_status"], "200")
            self.assertEqual(rec_200["result"], "success_with_warning")
            self.assertEqual(rec_200["error_category"], "usage_field_mismatch")
            # Token 字段保留
            self.assertEqual(rec_200["prompt_tokens"], "12")
            self.assertEqual(rec_200["completion_tokens"], "32")
            self.assertEqual(rec_200["total_tokens"], "44")
            self.assertEqual(rec_200["extended_output_tokens"], "0")
            self.assertEqual(rec_200["finish_reason"], "stop")
            self.assertEqual(rec_200["actual_model"], "deepseek-v4-flash")

            # 第 2 条：401 失败记录 → fail, user_authentication_error, 清空 token
            rec_401 = converted[1]
            self.assertEqual(rec_401["http_status"], "401")
            self.assertEqual(rec_401["result"], "fail")
            self.assertEqual(rec_401["error_category"], "user_authentication_error")
            self.assertEqual(rec_401["latency_ms"], "470")
            self.assertEqual(rec_401["request_id"], "req-auth-fail")
            # 非 2xx 清空字段
            for fld in ilr.FAILURE_EMPTY_FIELDS:
                self.assertEqual(rec_401[fld], "", f"字段 {fld} 应为空字符串")

    def test_deduplication_by_request_id(self) -> None:
        """重复 request_id 跳过"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            # 主表已有 req-new-001 — 构建 33 字段行
            dup_vals = ["R003", "2026/7/21", "uat_cn", "/v1/chat/completions",
                        "m", "m", "", "", "", "TRUE", "200", "", "", "stop",
                        "", "", "", "", "", "", "", "", "", "", "", "", "",
                        "req-new-001", "pass", "", "live_api", "FALSE", ""]
            main_rows = list(SAMPLE_MAIN_CSV_ROWS)
            main_rows.append(",".join(dup_vals))
            main_csv = make_main_csv(tmp, main_rows)
            live_csv = make_live_csv(tmp)
            backup_path = str(tmp / "backup.csv")

            with (
                patch.object(ilr, "LIVE_RECORDS_CSV", new=live_csv),
                patch.object(ilr, "MAIN_RECORDS_CSV", new=main_csv),
                patch.object(ilr, "BACKUP_FILE", new=backup_path),
            ):
                result = ilr.import_live(dry_run=False)

            # req-new-001 已在主表 → 跳过一个，新增一个（req-auth-fail）
            self.assertEqual(result["new_count"], 1)
            self.assertEqual(result["skip_count"], 1)
            self.assertEqual(result["generated_ids"], ["R004"])

    def test_idempotent_second_run(self) -> None:
        """重复运行幂等"""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            live_csv = make_live_csv(tmp)
            main_csv = make_main_csv(tmp)
            backup_path = str(tmp / "backup.csv")

            with (
                patch.object(ilr, "LIVE_RECORDS_CSV", new=live_csv),
                patch.object(ilr, "MAIN_RECORDS_CSV", new=main_csv),
                patch.object(ilr, "BACKUP_FILE", new=backup_path),
            ):
                # 第一次导入
                r1 = ilr.import_live(dry_run=False)
                self.assertEqual(r1["new_count"], 2)

                # 第二次导入（幂等）
                r2 = ilr.import_live(dry_run=False)
                self.assertEqual(r2["new_count"], 0)
                self.assertEqual(r2["skip_count"], 2)  # 两条都跳过

                # 最终 4 条（在 patch 上下文内读取）
                final = ilr.read_main_records()
                self.assertEqual(len(final), 4)

    def test_skip_empty_tested_at_and_request_id(self) -> None:
        """跳过 tested_at 和 request_id 均为空的旧测试污染行"""
        live_content = (
            "tested_at,environment,request_path,requested_model,actual_model,stream,"
            "http_status,latency_ms,finish_reason,prompt_tokens,completion_tokens,"
            "total_tokens,extended_output_tokens,request_id,result,error_category,"
            "data_source,is_mock,notes\n"
            # 真正有效记录
            "2026-07-21 10:34:12,uat_cn,/v1/chat/completions,deepseek-v4-flash,deepseek-v4-flash,FALSE,200,1056,stop,12,32,44,0,req-valid,pass,,live_api,False,\n"
            # 两条污染行（tested_at 和 request_id 均为空）
            ",,,,,,,,,,,,,,,,,,\n"
            ",,,,,,,,,,,,,,,,,,\n"
        )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            live_csv = make_live_csv(tmp, content=live_content)
            main_csv = make_main_csv(tmp)
            backup_path = str(tmp / "backup.csv")

            with (
                patch.object(ilr, "LIVE_RECORDS_CSV", new=live_csv),
                patch.object(ilr, "MAIN_RECORDS_CSV", new=main_csv),
                patch.object(ilr, "BACKUP_FILE", new=backup_path),
            ):
                result = ilr.import_live(dry_run=False)

            # 1 条新增，2 条跳过（污染行）
            self.assertEqual(result["new_count"], 1)
            self.assertEqual(result["skip_count"], 2)
            self.assertEqual(result["generated_ids"], ["R003"])


class TestBackupCreated(unittest.TestCase):
    """备份创建测试"""

    def test_backup_contains_original_data(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            live_csv = make_live_csv(tmp)
            main_csv = make_main_csv(tmp)
            backup_path = str(tmp / "backup.csv")

            with (
                patch.object(ilr, "LIVE_RECORDS_CSV", new=live_csv),
                patch.object(ilr, "MAIN_RECORDS_CSV", new=main_csv),
                patch.object(ilr, "BACKUP_FILE", new=backup_path),
            ):
                ilr.import_live(dry_run=False)

            # 读取备份
            with open(backup_path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                backup_rows = list(reader)
            self.assertEqual(len(backup_rows), 2)
            self.assertEqual(backup_rows[0]["record_id"], "R001")
            self.assertEqual(backup_rows[1]["record_id"], "R002")


if __name__ == "__main__":
    unittest.main()