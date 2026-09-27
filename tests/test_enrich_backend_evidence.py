"""测试后台证据补全功能"""

from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 将被测模块所在目录加入 sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import enrich_backend_evidence as ebe

# ── 测试用常量 ────────────────────────────────────────────────────

EVIDENCE = {
    "record_id": "R010",
    "request_id": "202607210234117798612548268d9d6EwEg56lA",
    "tested_at": "2026-07-21 10:34:12",
    "channel_id": 48,
    "channel_name": "Test-Duan-DeepSeek测试",
    "local_group": "default",
    "requested_model": "deepseek-v4-flash",
    "actual_model": "deepseek-v4-flash",
    "http_status": 200,
    "prompt_tokens": 12,
    "completion_tokens": 32,
    "input_price_per_1m": 1,
    "output_price_per_1m": 2,
    "cache_read_price_per_1m": 0.02,
    "cache_creation_price_per_1m": 1.25,
    "cost": 0.000076,
    "currency": "CNY",
    "request_path": "/v1/chat/completions",
    "request_conversion": "OpenAI Compatible -> Claude Messages",
    "billing_mode": "upstream_return",
    "stream": False,
    "upstream_group": "pending_confirmation",
    "temperature_policy": "pending_confirmation",
    "supports_stream": "pending_confirmation",
}

RECORDS_HEADER = ebe.MAIN_FIELDS

# 模拟 R010 的初始状态（未补全）
RECORD_R010_BEFORE = {
    "record_id": "R010",
    "tested_at": "2026-07-21 10:34:12",
    "environment": "uat_cn",
    "request_path": "/v1/chat/completions",
    "requested_model": "deepseek-v4-flash",
    "actual_model": "deepseek-v4-flash",
    "channel_id": "",
    "channel_name": "",
    "local_group": "",
    "stream": "False",
    "http_status": "200",
    "latency_ms": "1056",
    "ttft_ms": "",
    "finish_reason": "stop",
    "stream_status": "",
    "raw_input_tokens": "",
    "cache_creation_tokens": "",
    "prompt_tokens": "12",
    "completion_tokens": "32",
    "total_tokens": "44",
    "extended_output_tokens": "0",
    "input_price_per_1m": "",
    "output_price_per_1m": "",
    "cache_read_price_per_1m": "",
    "cache_creation_price_per_1m": "",
    "cost": "",
    "currency": "",
    "request_id": "202607210234117798612548268d9d6EwEg56lA",
    "result": "success_with_warning",
    "error_category": "usage_field_mismatch",
    "data_source": "live_api",
    "is_mock": "FALSE",
    "notes": "completion_tokens=32, extended_output_tokens=0",
}

MATRIX_HEADER = ebe.MATRIX_FIELDS


def _write_csv(filepath: str, rows: list[dict[str, str]], fields: list[str]) -> None:
    with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _write_evidence(evidence_dir: str, data: dict | None = None) -> str:
    """写证据 JSON，返回路径"""
    os.makedirs(evidence_dir, exist_ok=True)
    path = os.path.join(evidence_dir, "R010_backend_log.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data or EVIDENCE, f, ensure_ascii=False, indent=2)
    return path


# ── 测试类 ───────────────────────────────────────────────────────


class TestEnrichRequestRecords(unittest.TestCase):
    """字段补全测试"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_env(self, records: list[dict] | None = None, matrix: list[dict] | None = None):
        """创建临时文件环境"""
        if records is None:
            records = [RECORD_R010_BEFORE.copy()]
        rec_path = os.path.join(self.tmp, "request_records.csv")
        _write_csv(rec_path, records, RECORDS_HEADER)

        if matrix is None:
            matrix = []
        mat_path = os.path.join(self.tmp, "model_channel_matrix.csv")
        _write_csv(mat_path, matrix, MATRIX_HEADER)

        ev_path = _write_evidence(self.tmp)

        # 打补丁
        self._orig_rec_csv = ebe.MAIN_RECORDS_CSV
        self._orig_mat_csv = ebe.MATRIX_CSV
        self._orig_ev_file = ebe.EVIDENCE_FILE
        self._orig_backup_dir = ebe.BACKUP_DIR

        ebe.MAIN_RECORDS_CSV = rec_path
        ebe.MATRIX_CSV = mat_path
        ebe.EVIDENCE_FILE = ev_path
        ebe.BACKUP_DIR = self.tmp

    def _restore(self):
        ebe.MAIN_RECORDS_CSV = self._orig_rec_csv
        ebe.MATRIX_CSV = self._orig_mat_csv
        ebe.EVIDENCE_FILE = self._orig_ev_file
        ebe.BACKUP_DIR = self._orig_backup_dir

    def test_enrich_fields(self):
        """补全后渠道/价格字段正确写入"""
        self._make_env()
        rows = ebe.read_csv(ebe.MAIN_RECORDS_CSV, RECORDS_HEADER)
        rows, updated, rid = ebe.enrich_request_records(rows, EVIDENCE)
        self.assertEqual(updated, 1)
        self.assertEqual(rid, "R010")
        row = rows[0]
        self.assertEqual(row["channel_id"], "48")
        self.assertEqual(row["channel_name"], "Test-Duan-DeepSeek测试")
        self.assertEqual(row["local_group"], "default")
        self.assertEqual(row["input_price_per_1m"], "1")
        self.assertEqual(row["output_price_per_1m"], "2")
        self.assertEqual(row["cache_read_price_per_1m"], "0.02")
        self.assertEqual(row["cache_creation_price_per_1m"], "1.25")
        self.assertEqual(row["cost"], "0.000076")
        self.assertEqual(row["currency"], "CNY")
        self.assertEqual(row["data_source"], "live_api+backend_log")
        self._restore()

    def test_usage_warning_preserved(self):
        """保留 usage 告警字段：result / completion_tokens / extended_output_tokens"""
        self._make_env()
        rows = ebe.read_csv(ebe.MAIN_RECORDS_CSV, RECORDS_HEADER)
        rows, _, _ = ebe.enrich_request_records(rows, EVIDENCE)
        row = rows[0]
        self.assertEqual(row["result"], "success_with_warning")
        self.assertEqual(row["error_category"], "usage_field_mismatch")
        self.assertEqual(row["completion_tokens"], "32")
        self.assertEqual(row["extended_output_tokens"], "0")
        self._restore()

    def test_notes_appended(self):
        """notes 追加了请求转换/计费模式/费用复核信息"""
        self._make_env()
        rows = ebe.read_csv(ebe.MAIN_RECORDS_CSV, RECORDS_HEADER)
        rows, _, _ = ebe.enrich_request_records(rows, EVIDENCE)
        notes = rows[0]["notes"]
        self.assertIn("请求转换OpenAI Compatible -> Claude Messages", notes)
        self.assertIn("计费模式为上游返回", notes)
        self.assertIn("费用复核通过", notes)
        self.assertIn("completion_tokens=32, extended_output_tokens=0", notes)
        self._restore()

    def test_cost_correct(self):
        """费用计算：12*1/1M + 32*2/1M = 0.000012 + 0.000064 = 0.000076"""
        expected_cost = 12 * 1 / 1_000_000 + 32 * 2 / 1_000_000
        self.assertAlmostEqual(EVIDENCE["cost"], expected_cost)


class TestMatrixOperations(unittest.TestCase):
    """渠道矩阵测试"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_matrix_insert_new(self):
        """新增矩阵行 MC001"""
        rows = []
        rows, op, mid = ebe.enrich_matrix(rows, EVIDENCE)
        self.assertEqual(op, "inserted")
        self.assertEqual(mid, "MC001")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["channel_id"], "48")
        self.assertEqual(row["requested_model"], "deepseek-v4-flash")
        self.assertEqual(row["supports_non_stream"], "TRUE")
        self.assertEqual(row["supports_stream"], "pending_confirmation")
        self.assertEqual(row["availability_status"], "available")
        self.assertEqual(row["evidence_request_id"], EVIDENCE["request_id"])

    def test_matrix_idempotent_by_request_id(self):
        """相同 request_id 幂等：第二次更新而非新增"""
        rows = []
        rows, op1, mid1 = ebe.enrich_matrix(rows, EVIDENCE)
        self.assertEqual(op1, "inserted")
        self.assertEqual(mid1, "MC001")

        # 第二次，相同 evidence
        rows, op2, mid2 = ebe.enrich_matrix(rows, EVIDENCE)
        self.assertEqual(op2, "updated")
        self.assertEqual(mid2, "MC001")
        self.assertEqual(len(rows), 1)

    def test_matrix_idempotent_by_channel_model(self):
        """相同 channel_id + requested_model 幂等"""
        rows = []
        rows, _, _ = ebe.enrich_matrix(rows, EVIDENCE)
        self.assertEqual(len(rows), 1)

        # 构筑另一个 request_id 但相同 channel+model
        ev2 = dict(EVIDENCE)
        ev2["request_id"] = "different-request-id"
        rows, op2, mid2 = ebe.enrich_matrix(rows, ev2)
        self.assertEqual(op2, "updated")
        self.assertEqual(mid2, "MC001")
        self.assertEqual(len(rows), 1)

    def test_matrix_fields(self):
        """矩阵行字段完整性"""
        rows = []
        rows, _, mid = ebe.enrich_matrix(rows, EVIDENCE)
        row = rows[0]
        self.assertEqual(row["matrix_id"], "MC001")
        self.assertEqual(row["channel_type"], "DeepSeek")
        self.assertEqual(row["upstream_group"], "pending_confirmation")
        self.assertEqual(row["mapping_status"], "match")
        self.assertEqual(row["pricing_type"], "per_token")
        self.assertEqual(row["is_mock"], "FALSE")
        self.assertIn("OpenAI Compatible", row["notes"])

    def test_upstream_group_pending(self):
        """未确认上游分组写入 pending_confirmation"""
        rows = []
        _, _, _ = ebe.enrich_matrix(rows, EVIDENCE)
        row = rows[0]
        self.assertEqual(row["upstream_group"], "pending_confirmation")

    def test_all_matrix_fields_non_empty(self):
        """MC004 必填字段均不为空"""
        rows = []
        _, _, _ = ebe.enrich_matrix(rows, EVIDENCE)
        row = rows[0]
        for field in ebe.MATRIX_FIELDS:
            val = (row.get(field) or "").strip()
            self.assertNotEqual(val, "", f"字段 {field} 不应为空")

    def test_repeat_enrich_no_duplicate(self):
        """重复执行更新现有 MC004，不新增重复记录"""
        rows = []
        rows, op1, mid1 = ebe.enrich_matrix(rows, EVIDENCE)
        self.assertEqual(op1, "inserted")

        # 模拟已写入后再次执行补全（相同 evidence）
        rows2 = rows[:]
        rows2, op2, mid2 = ebe.enrich_matrix(rows2, EVIDENCE)
        self.assertEqual(op2, "updated")
        self.assertEqual(mid2, mid1)
        self.assertEqual(len(rows2), 1)


class TestBackup(unittest.TestCase):
    """备份测试"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_backup_created_for_records(self):
        """补全后 request_records 备份被创建"""
        # 准备环境
        rec_path = os.path.join(self.tmp, "request_records.csv")
        _write_csv(rec_path, [RECORD_R010_BEFORE.copy()], RECORDS_HEADER)
        mat_path = os.path.join(self.tmp, "model_channel_matrix.csv")
        _write_csv(mat_path, [], MATRIX_HEADER)
        ev_path = _write_evidence(self.tmp)

        orig_rec = ebe.MAIN_RECORDS_CSV
        orig_mat = ebe.MATRIX_CSV
        orig_ev = ebe.EVIDENCE_FILE
        orig_backup = ebe.BACKUP_DIR

        ebe.MAIN_RECORDS_CSV = rec_path
        ebe.MATRIX_CSV = mat_path
        ebe.EVIDENCE_FILE = ev_path
        ebe.BACKUP_DIR = self.tmp

        try:
            ebe.enrich(dry_run=False)
            # 验证备份文件存在（至少 2 个备份）
            backup_files = [f for f in os.listdir(self.tmp) if "before_enrich" in f]
            self.assertGreaterEqual(len(backup_files), 2)
        finally:
            ebe.MAIN_RECORDS_CSV = orig_rec
            ebe.MATRIX_CSV = orig_mat
            ebe.EVIDENCE_FILE = orig_ev
            ebe.BACKUP_DIR = orig_backup


class TestDryRun(unittest.TestCase):
    """dry-run 无写入测试"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_dry_run_no_files_written(self):
        """dry-run 模式不写任何正式文件"""
        rec_path = os.path.join(self.tmp, "request_records.csv")
        _write_csv(rec_path, [RECORD_R010_BEFORE.copy()], RECORDS_HEADER)
        mat_path = os.path.join(self.tmp, "model_channel_matrix.csv")
        _write_csv(mat_path, [], MATRIX_HEADER)
        ev_path = _write_evidence(self.tmp)

        orig_rec = ebe.MAIN_RECORDS_CSV
        orig_mat = ebe.MATRIX_CSV
        orig_ev = ebe.EVIDENCE_FILE
        orig_backup = ebe.BACKUP_DIR

        ebe.MAIN_RECORDS_CSV = rec_path
        ebe.MATRIX_CSV = mat_path
        ebe.EVIDENCE_FILE = ev_path
        ebe.BACKUP_DIR = self.tmp

        # 记录初始 mtime
        rec_mtime = os.path.getmtime(rec_path)
        mat_mtime = os.path.getmtime(mat_path)

        try:
            ebe.enrich(dry_run=True)

            # 文件未被修改
            self.assertEqual(os.path.getmtime(rec_path), rec_mtime)
            self.assertEqual(os.path.getmtime(mat_path), mat_mtime)

            # 无备份文件
            backup_files = [f for f in os.listdir(self.tmp) if "before_enrich" in f]
            self.assertEqual(len(backup_files), 0)
        finally:
            ebe.MAIN_RECORDS_CSV = orig_rec
            ebe.MATRIX_CSV = orig_mat
            ebe.EVIDENCE_FILE = orig_ev
            ebe.BACKUP_DIR = orig_backup


if __name__ == "__main__":
    unittest.main()