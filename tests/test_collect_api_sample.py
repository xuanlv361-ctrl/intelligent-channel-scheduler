"""单元测试：collect_api_sample，完全 Mock，不访问真实网络"""

from __future__ import annotations

import io
import hashlib
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import collect_api_sample as cas


def _existing_file_sha256(path: str) -> str | None:
    target = Path(path)
    return hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None


_LIVE_OUTPUT_BASELINE = {
    cas.OBSERVATION_FILE: _existing_file_sha256(cas.OBSERVATION_FILE),
    cas.RECORDS_CSV: _existing_file_sha256(cas.RECORDS_CSV),
}


# ── 辅助 ─────────────────────────────────────────────────────────────

def _make_mock_response(body: dict, headers: dict | None = None) -> MagicMock:
    m = MagicMock()
    m.read.return_value = json.dumps(body).encode("utf-8")
    m.headers = headers or {}
    return m


def _make_http_error(code: int, body_bytes: bytes,
                     headers: dict | None = None) -> cas.urllib.error.HTTPError:
    return cas.urllib.error.HTTPError(
        url="https://example.com",
        code=code,
        msg="error",
        hdrs=headers or {},
        fp=io.BytesIO(body_bytes),
    )


# ═══════════════════════════════════════════════════════════════════
# 1. 工具函数
# ═══════════════════════════════════════════════════════════════════

class TestUtilityFunctions(unittest.TestCase):
    """工具函数测试"""

    def test_beijing_now_format(self) -> None:
        result = cas.beijing_now()
        self.assertRegex(result, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_sanitize_sk_key(self) -> None:
        text = '{"error": "key sk-abcdefghijklmnopqrstuvwxyz1234"}'  # secret-scan: allow
        safe = cas.sanitize_error_text(text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz1234", safe)  # secret-scan: allow
        self.assertIn("***", safe)

    def test_sanitize_bearer(self) -> None:
        text = "Authorization: Bearer token12345_secret"  # secret-scan: allow
        safe = cas.sanitize_error_text(text)
        self.assertNotIn("Bearer token12345_secret", safe)
        self.assertIn("***", safe)

    def test_read_api_key_from_env(self) -> None:
        with patch.dict(os.environ, {"WEIMETA_API_KEY": "sk-real-key"}):
            self.assertEqual(cas.read_api_key(), "sk-real-key")

    def test_read_api_key_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cas.read_api_key(), "")


# ═══════════════════════════════════════════════════════════════════
# 2. 请求体构建与参数传递
# ═══════════════════════════════════════════════════════════════════

class TestRequestBuilding(unittest.TestCase):
    """请求构建测试"""

    def test_build_request_body_uses_model(self) -> None:
        body = cas.build_request_body("my-model", "hello")
        self.assertEqual(body["model"], "my-model")

    def test_build_request_body_uses_prompt(self) -> None:
        body = cas.build_request_body("m", "my prompt")
        self.assertEqual(body["messages"][0]["content"], "my prompt")

    def test_build_request_body_stream_false(self) -> None:
        body = cas.build_request_body("m", "p")
        self.assertIs(body["stream"], False)

    def test_build_request_body_no_extra(self) -> None:
        body = cas.build_request_body("m", "p")
        allowed = {"model", "messages", "stream"}
        for key in body:
            self.assertIn(key, allowed, f"不应包含字段：{key}")

    def test_build_http_request_auth(self) -> None:
        req = cas.build_http_request("sk-test-key", "m", "p")
        auth = req.headers.get("Authorization", "")
        self.assertTrue(auth.startswith("Bearer "))
        self.assertIn("sk-test-key", auth)

    def test_build_http_request_url_method(self) -> None:
        req = cas.build_http_request("sk-k", "m", "p")
        self.assertEqual(req.full_url, cas.API_URL)
        self.assertEqual(req.method, "POST")


# ═══════════════════════════════════════════════════════════════════
# 3. 错误分类
# ═══════════════════════════════════════════════════════════════════

class TestErrorClassification(unittest.TestCase):
    """统一错误分类测试"""

    def test_400(self) -> None:
        self.assertEqual(cas.classify_error(400, "", ""), "request_parameter_error")

    def test_401(self) -> None:
        self.assertEqual(cas.classify_error(401, "", ""), "user_authentication_error")

    def test_429(self) -> None:
        self.assertEqual(cas.classify_error(429, "", ""), "rate_limit")

    def test_503_model_not_found(self) -> None:
        body = '{"error": "model_not_found"}'
        self.assertEqual(cas.classify_error(503, "", body), "model_unavailable")

    def test_503_no_available_channel(self) -> None:
        body = 'No available channel for model'
        self.assertEqual(cas.classify_error(503, "", body), "model_unavailable")

    def test_503_other(self) -> None:
        body = '{"error": "overloaded"}'
        self.assertEqual(cas.classify_error(503, "", body), "platform_routing_error")

    def test_timeout_connection(self) -> None:
        self.assertEqual(cas.classify_error(None, "timeout", ""), "timeout")

    def test_protocol_compatibility(self) -> None:
        self.assertEqual(cas.classify_error(None, "conn_refused", ""), "protocol_compatibility_error")

    def test_unknown_http(self) -> None:
        self.assertEqual(cas.classify_error(502, "", ""), "unknown_error")


# ═══════════════════════════════════════════════════════════════════
# 4. 成功响应解析
# ═══════════════════════════════════════════════════════════════════

class TestParseSuccessResponse(unittest.TestCase):
    """成功响应解析测试"""

    @patch("time.time", return_value=2000.0)
    def test_request_id_from_header(self, _) -> None:
        resp = _make_mock_response(
            {"id": "body-id", "model": "m", "choices": [{"finish_reason": "stop"}], "usage": {}},
            {"X-Oneapi-Request-Id": "header-req-999"},
        )
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["request_id"], "header-req-999")

    @patch("time.time", return_value=2000.0)
    def test_request_id_missing_is_empty(self, _) -> None:
        resp = _make_mock_response(
            {"id": "body-id", "model": "m", "choices": [{"finish_reason": "stop"}], "usage": {}},
            {},
        )
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["request_id"], "")

    @patch("time.time", return_value=2000.0)
    def test_finish_reason_from_choices(self, _) -> None:
        resp = _make_mock_response(
            {"model": "m", "choices": [{"finish_reason": "length"}], "usage": {}},
        )
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["finish_reason"], "length")

    @patch("time.time", return_value=2000.0)
    def test_extended_output_present(self, _) -> None:
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"output_tokens": 7},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["extended_output_tokens"], 7)

    @patch("time.time", return_value=2000.0)
    def test_extended_output_zero(self, _) -> None:
        """output_tokens 为 0 时应原样记录"""
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"output_tokens": 0},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["extended_output_tokens"], 0)

    @patch("time.time", return_value=2000.0)
    def test_extended_output_missing_is_empty(self, _) -> None:
        """output_tokens 不存在时留空"""
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["extended_output_tokens"], "")

    @patch("time.time", return_value=2000.0)
    def test_tokens_preserved(self, _) -> None:
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["prompt_tokens"], 10)
        self.assertEqual(rec["completion_tokens"], 20)
        self.assertEqual(rec["total_tokens"], 30)

    @patch("time.time", return_value=2000.0)
    def test_http_status_200(self, _) -> None:
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["http_status"], 200)
        self.assertEqual(rec["result"], "pass")

    @patch("time.time", return_value=2000.0)
    def test_success_requested_model_from_param(self, _) -> None:
        """成功响应中 requested_model 必须等于传入的 model 参数"""
        custom_model = "my-custom-v1"
        resp = _make_mock_response(
            {"model": "actual-gpt", "choices": [{"finish_reason": "stop"}], "usage": {}},
        )
        rec = cas.parse_success_response(resp, 1000000.0, model=custom_model)
        self.assertEqual(rec["requested_model"], custom_model)

    @patch("time.time", return_value=2000.0)
    def test_success_requested_model_default(self, _) -> None:
        """不传 model 时使用 DEFAULT_MODEL"""
        resp = _make_mock_response(
            {"model": "m", "choices": [{"finish_reason": "stop"}], "usage": {}},
        )
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["requested_model"], cas.DEFAULT_MODEL)

    # ── usage_field_mismatch 检测 ─────────────────────────────────

    @patch("time.time", return_value=2000.0)
    def test_usage_match_pass(self, _) -> None:
        """completion_tokens == extended_output_tokens 时保持 pass"""
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"completion_tokens": 32, "output_tokens": 32},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["result"], "pass")
        self.assertEqual(rec["error_category"], "")

    @patch("time.time", return_value=2000.0)
    def test_usage_mismatch_sets_warning(self, _) -> None:
        """completion_tokens=32, extended_output_tokens=0 → success_with_warning"""
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"completion_tokens": 32, "output_tokens": 0},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["result"], "success_with_warning")
        self.assertEqual(rec["error_category"], "usage_field_mismatch")
        self.assertIn("completion_tokens=32", rec["notes"])
        self.assertIn("extended_output_tokens=0", rec["notes"])

    @patch("time.time", return_value=2000.0)
    def test_usage_mismatch_zero_valid_not_missing(self, _) -> None:
        """0 是有效值，不是缺失；completion_tokens=0, output_tokens=5 也应标记"""
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"completion_tokens": 0, "output_tokens": 5},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["result"], "success_with_warning")
        self.assertEqual(rec["error_category"], "usage_field_mismatch")

    @patch("time.time", return_value=2000.0)
    def test_usage_mismatch_output_tokens_missing_no_warning(self, _) -> None:
        """output_tokens 缺失时（字段不存在）不触发 warning"""
        resp = _make_mock_response({
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"completion_tokens": 32},
        })
        rec = cas.parse_success_response(resp, 1000000.0)
        self.assertEqual(rec["result"], "pass")
        self.assertEqual(rec["error_category"], "")


# ═══════════════════════════════════════════════════════════════════
# 5. HTTP 错误响应解析
# ═══════════════════════════════════════════════════════════════════

class TestParseHttpErrorResponse(unittest.TestCase):
    """HTTP 错误响应解析测试"""

    @patch("time.time", return_value=3000.0)
    def test_400_error(self, _) -> None:
        err = _make_http_error(400, b'{"error":"bad request"}')
        rec = cas.parse_http_error_response(err, 2000000.0)
        self.assertEqual(rec["http_status"], 400)
        self.assertEqual(rec["error_category"], "request_parameter_error")

    @patch("time.time", return_value=3000.0)
    def test_401_error(self, _) -> None:
        err = _make_http_error(401, b'{"error":"unauthorized"}', {"X-Oneapi-Request-Id": "err-id"})
        rec = cas.parse_http_error_response(err, 2000000.0)
        self.assertEqual(rec["http_status"], 401)
        self.assertEqual(rec["error_category"], "user_authentication_error")
        self.assertEqual(rec["request_id"], "err-id")

    @patch("time.time", return_value=3000.0)
    def test_429_error(self, _) -> None:
        err = _make_http_error(429, b'{"error":"rate limit"}')
        rec = cas.parse_http_error_response(err, 2000000.0)
        self.assertEqual(rec["http_status"], 429)
        self.assertEqual(rec["error_category"], "rate_limit")

    @patch("time.time", return_value=3000.0)
    def test_503_model_unavailable(self, _) -> None:
        err = _make_http_error(503, b'{"error":"model_not_found"}')
        rec = cas.parse_http_error_response(err, 2000000.0)
        self.assertEqual(rec["error_category"], "model_unavailable")

    @patch("time.time", return_value=3000.0)
    def test_503_platform_routing(self, _) -> None:
        err = _make_http_error(503, b'{"error":"overloaded"}')
        rec = cas.parse_http_error_response(err, 2000000.0)
        self.assertEqual(rec["error_category"], "platform_routing_error")

    @patch("time.time", return_value=3000.0)
    def test_error_unmeasured_fields_empty(self, _) -> None:
        err = _make_http_error(401, b'{"error":"no"}')
        rec = cas.parse_http_error_response(err, 2000000.0)
        self.assertEqual(rec["prompt_tokens"], "")
        self.assertEqual(rec["completion_tokens"], "")
        self.assertEqual(rec["total_tokens"], "")
        self.assertEqual(rec["extended_output_tokens"], "")
        self.assertEqual(rec["finish_reason"], "")
        self.assertEqual(rec["actual_model"], "")

    @patch("time.time", return_value=3000.0)
    def test_error_sanitized(self, _) -> None:
        err = _make_http_error(400, b'{"error":"key sk-test-abc12345"}')
        rec = cas.parse_http_error_response(err, 2000000.0)
        self.assertNotIn("sk-test-abc12345", rec["notes"])
        self.assertIn("***", rec["notes"])

    @patch("time.time", return_value=3000.0)
    def test_error_no_request_id_header(self, _) -> None:
        err = _make_http_error(503, b'overloaded')
        rec = cas.parse_http_error_response(err, 2000000.0)
        self.assertEqual(rec["request_id"], "")

    @patch("time.time", return_value=3000.0)
    def test_http_error_requested_model_from_param(self, _) -> None:
        """HTTP 错误中 requested_model 必须等于传入的 model 参数"""
        err = _make_http_error(401, b'{"error":"no"}')
        rec = cas.parse_http_error_response(err, 2000000.0, model="custom-err-model")
        self.assertEqual(rec["requested_model"], "custom-err-model")


# ═══════════════════════════════════════════════════════════════════
# 6. 连接错误解析
# ═══════════════════════════════════════════════════════════════════

class TestParseConnectionError(unittest.TestCase):
    """连接错误解析测试"""

    @patch("time.time", return_value=65000.0)
    def test_socket_timeout(self, _) -> None:
        exc = socket.timeout("timed out")
        rec = cas.parse_connection_error(exc, 1000.0)
        self.assertEqual(rec["error_category"], "timeout")
        self.assertEqual(rec["result"], "fail")
        self.assertEqual(rec["http_status"], "")

    @patch("time.time", return_value=65000.0)
    def test_urlerror_timeout(self, _) -> None:
        exc = cas.urllib.error.URLError(socket.timeout("timed out"))
        rec = cas.parse_connection_error(exc, 1000.0)
        self.assertEqual(rec["error_category"], "timeout")

    @patch("time.time", return_value=65000.0)
    def test_urlerror_connection_refused(self, _) -> None:
        exc = cas.urllib.error.URLError("Connection refused")
        rec = cas.parse_connection_error(exc, 1000.0)
        self.assertEqual(rec["error_category"], "protocol_compatibility_error")
        self.assertEqual(rec["result"], "fail")

    @patch("time.time", return_value=65000.0)
    def test_connection_error_unmeasured_empty(self, _) -> None:
        exc = cas.urllib.error.URLError("conn lost")
        rec = cas.parse_connection_error(exc, 1000.0)
        self.assertEqual(rec["http_status"], "")
        self.assertEqual(rec["prompt_tokens"], "")
        self.assertEqual(rec["completion_tokens"], "")
        self.assertEqual(rec["extended_output_tokens"], "")
        self.assertEqual(rec["actual_model"], "")
        self.assertEqual(rec["request_id"], "")

    @patch("time.time", return_value=65000.0)
    def test_connection_error_requested_model_from_param(self, _) -> None:
        """连接错误中 requested_model 必须等于传入的 model 参数"""
        exc = socket.timeout("timed out")
        rec = cas.parse_connection_error(exc, 1000.0, model="custom-conn-model")
        self.assertEqual(rec["requested_model"], "custom-conn-model")


# ═══════════════════════════════════════════════════════════════════
# 7. main() 退出码（Mock collect + save_json + append_csv）
# ═══════════════════════════════════════════════════════════════════

class TestMainExitCodes(unittest.TestCase):
    """main() 退出码测试（不得向正式 output 写文件）"""

    def test_dry_run_returns_0(self) -> None:
        code = cas.main(["--dry-run"])
        self.assertEqual(code, 0)

    def test_no_key_returns_2(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            code = cas.main([])
        self.assertEqual(code, 2)

    @patch("collect_api_sample.append_csv")
    @patch("collect_api_sample.save_json")
    @patch("collect_api_sample.collect")
    def test_success_returns_0(self, mock_collect, mock_save_json, mock_append_csv) -> None:
        mock_collect.return_value = {"result": "pass"}
        with patch.dict(os.environ, {"WEIMETA_API_KEY": "sk-key"}):
            code = cas.main(["--model", "m", "--prompt", "p"])
        self.assertEqual(code, 0)

    @patch("collect_api_sample.append_csv")
    @patch("collect_api_sample.save_json")
    @patch("collect_api_sample.collect")
    def test_fail_returns_1(self, mock_collect, mock_save_json, mock_append_csv) -> None:
        mock_collect.return_value = {"result": "fail"}
        with patch.dict(os.environ, {"WEIMETA_API_KEY": "sk-key"}):
            code = cas.main(["--model", "m", "--prompt", "p"])
        self.assertEqual(code, 1)


# ═══════════════════════════════════════════════════════════════════
# 8. dry-run 零网络调用 + 不写 output
# ═══════════════════════════════════════════════════════════════════

class TestDryRun(unittest.TestCase):
    """dry-run 模式测试"""

    @patch("collect_api_sample.urllib.request.urlopen")
    def test_dry_run_never_calls_urlopen(self, mock_urlopen) -> None:
        code = cas.main(["--dry-run"])
        self.assertEqual(code, 0)
        mock_urlopen.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# 9. 完整 collect 流程 Mock 集成
# ═══════════════════════════════════════════════════════════════════

class TestCollectIntegrationMocked(unittest.TestCase):
    """完整 collect Mock 集成测试 + requested_model 验证"""

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_success(self, _) -> None:
        body = {
            "id": "i1",
            "model": "deepseek-v4-flash",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
        }
        with patch("collect_api_sample.urllib.request.urlopen",
                    return_value=_make_mock_response(body)):
            rec = cas.collect("sk-key", "deepseek-v4-flash", "hi")
        self.assertEqual(rec["http_status"], 200)
        self.assertEqual(rec["prompt_tokens"], 5)
        self.assertEqual(rec["completion_tokens"], 8)

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_http_401(self, _) -> None:
        err = _make_http_error(401, b'{"error":"bad key"}')
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=err):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["http_status"], 401)
        self.assertEqual(rec["error_category"], "user_authentication_error")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_http_503_model_unavailable(self, _) -> None:
        err = _make_http_error(503, b'{"error":"No available channel"}')
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=err):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["error_category"], "model_unavailable")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_timeout(self, _) -> None:
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=socket.timeout("timed out")):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["error_category"], "timeout")
        self.assertEqual(rec["http_status"], "")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_connection_refused(self, _) -> None:
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=cas.urllib.error.URLError("Connection refused")):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["error_category"], "protocol_compatibility_error")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_unknown_exception(self, _) -> None:
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=ValueError("weird")):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["error_category"], "unknown_error")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_output_tokens_present(self, _) -> None:
        body = {
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"output_tokens": 3},
        }
        with patch("collect_api_sample.urllib.request.urlopen",
                    return_value=_make_mock_response(body)):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["extended_output_tokens"], 3)

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_output_tokens_zero(self, _) -> None:
        body = {
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {"output_tokens": 0},
        }
        with patch("collect_api_sample.urllib.request.urlopen",
                    return_value=_make_mock_response(body)):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["extended_output_tokens"], 0)

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_output_tokens_missing(self, _) -> None:
        body = {
            "model": "m",
            "choices": [{"finish_reason": "stop"}],
            "usage": {},
        }
        with patch("collect_api_sample.urllib.request.urlopen",
                    return_value=_make_mock_response(body)):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["extended_output_tokens"], "")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_request_id_from_header(self, _) -> None:
        body = {"model": "m", "choices": [{"finish_reason": "stop"}], "usage": {}}
        resp = _make_mock_response(body, {"X-Oneapi-Request-Id": "req-abc"})
        with patch("collect_api_sample.urllib.request.urlopen",
                    return_value=resp):
            rec = cas.collect("sk-key", "m", "p")
        self.assertEqual(rec["request_id"], "req-abc")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_success_requested_model(self, _) -> None:
        """成功 collect 返回的 requested_model 必须等于传入的 model"""
        body = {
            "model": "actual-model-name",
            "choices": [{"finish_reason": "stop"}],
            "usage": {},
        }
        with patch("collect_api_sample.urllib.request.urlopen",
                    return_value=_make_mock_response(body)):
            rec = cas.collect("sk-key", "my-custom-success-model", "p")
        self.assertEqual(rec["requested_model"], "my-custom-success-model")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_http_error_requested_model(self, _) -> None:
        """HTTP 错误 collect 返回的 requested_model 必须等于传入的 model"""
        err = _make_http_error(401, b'{"error":"bad"}')
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=err):
            rec = cas.collect("sk-key", "my-custom-http-error-model", "p")
        self.assertEqual(rec["requested_model"], "my-custom-http-error-model")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_timeout_requested_model(self, _) -> None:
        """超时 collect 返回的 requested_model 必须等于传入的 model"""
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=socket.timeout("timed out")):
            rec = cas.collect("sk-key", "my-custom-timeout-model", "p")
        self.assertEqual(rec["requested_model"], "my-custom-timeout-model")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_connection_refused_requested_model(self, _) -> None:
        """连接拒绝 collect 返回的 requested_model 必须等于传入的 model"""
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=cas.urllib.error.URLError("Connection refused")):
            rec = cas.collect("sk-key", "my-custom-conn-refused-model", "p")
        self.assertEqual(rec["requested_model"], "my-custom-conn-refused-model")

    @patch("time.time", side_effect=[1000.0, 2000.0])
    def test_collect_unknown_error_requested_model(self, _) -> None:
        """未知异常 collect 返回的 requested_model 必须等于传入的 model"""
        with patch("collect_api_sample.urllib.request.urlopen",
                    side_effect=ValueError("weird")):
            rec = cas.collect("sk-key", "my-custom-unknown-model", "p")
        self.assertEqual(rec["requested_model"], "my-custom-unknown-model")


# ═══════════════════════════════════════════════════════════════════
# 10. 输出文件保存（使用 TemporaryDirectory，不污染正式 output）
# ═══════════════════════════════════════════════════════════════════

class TestSaveFunctions(unittest.TestCase):
    """保存函数测试（使用临时目录，不写入正式 output）"""

    def setUp(self) -> None:
        self.rec = {
            "tested_at": "2026-07-21 10:00:00",
            "environment": "uat_cn",
            "request_path": "/v1/chat/completions",
            "requested_model": "deepseek-v4-flash",
            "actual_model": "",
            "stream": False,
            "http_status": "",
            "latency_ms": "",
            "finish_reason": "",
            "prompt_tokens": "",
            "completion_tokens": "",
            "total_tokens": "",
            "extended_output_tokens": "",
            "request_id": "",
            "result": "fail",
            "error_category": "timeout",
            "data_source": "live_api",
            "is_mock": False,
            "notes": "test",
        }
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_save_json(self) -> None:
        obs_file = str(self.tmp_path / "test_obs.json")
        with patch("collect_api_sample.OBSERVATION_FILE", new=obs_file):
            cas.ensure_output_dir()
            cas.save_json(self.rec)
        self.assertTrue(os.path.exists(obs_file))
        with open(obs_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["request_id"], "")
        self.assertEqual(data["error_category"], "timeout")

    def test_append_csv(self) -> None:
        csv_file = str(self.tmp_path / "test_records.csv")
        with patch("collect_api_sample.RECORDS_CSV", new=csv_file):
            cas.ensure_output_dir()
            cas.append_csv(self.rec)
        self.assertTrue(os.path.exists(csv_file))
        with open(csv_file, "r", encoding="utf-8-sig") as f:
            content = f.read()
        self.assertIn("timeout", content)
        self.assertIn("tested_at", content)


# ═══════════════════════════════════════════════════════════════════
# 11. 正式 output 完整性：运行测试后不新增/改变 live 输出文件
# ═══════════════════════════════════════════════════════════════════

class TestOutputUnchanged(unittest.TestCase):
    """确认运行测试后正式 output 目录不变（不删除用户已有文件）"""

    def test_output_live_files_not_created(self) -> None:
        """测试运行后不应产生正式 live 输出文件"""
        obs = cas.OBSERVATION_FILE
        csv_ = cas.RECORDS_CSV
        # 如果文件已存在（来自历史真实调用），则跳过不判定（不删除用户数据）
        # Existing historical files are verified by their import-time digest.
        # 文件不存在时，确认本次测试没有创建它们
        self.assertEqual(_existing_file_sha256(obs), _LIVE_OUTPUT_BASELINE[obs])
        self.assertEqual(_existing_file_sha256(csv_), _LIVE_OUTPUT_BASELINE[csv_])


# ═══════════════════════════════════════════════════════════════════
# 12. —model 和 —prompt 参数传递
# ═══════════════════════════════════════════════════════════════════

class TestArgParsing(unittest.TestCase):
    """命令行参数测试"""

    def test_default_model(self) -> None:
        args = cas.parse_args(["--dry-run"])
        self.assertEqual(args.model, cas.DEFAULT_MODEL)

    def test_custom_model(self) -> None:
        args = cas.parse_args(["--model", "gpt-4o-mini"])
        self.assertEqual(args.model, "gpt-4o-mini")

    def test_default_prompt(self) -> None:
        args = cas.parse_args(["--dry-run"])
        self.assertEqual(args.prompt, cas.DEFAULT_PROMPT)

    def test_custom_prompt(self) -> None:
        args = cas.parse_args(["--prompt", "hello"])
        self.assertEqual(args.prompt, "hello")

    def test_dry_run_flag(self) -> None:
        args = cas.parse_args(["--dry-run"])
        self.assertTrue(args.dry_run)

    def test_no_dry_run(self) -> None:
        args = cas.parse_args([])
        self.assertFalse(args.dry_run)


# ═══════════════════════════════════════════════════════════════════
# 13. 基础记录模板
# ═══════════════════════════════════════════════════════════════════

class TestEmptyRecordBase(unittest.TestCase):
    """基础记录模板测试"""

    def test_all_unmeasured_empty_string(self) -> None:
        rec = cas._empty_record_base()
        for key in ("http_status", "latency_ms", "finish_reason",
                    "prompt_tokens", "completion_tokens", "total_tokens",
                    "extended_output_tokens", "request_id", "error_category",
                    "actual_model", "result"):
            self.assertEqual(rec[key], "", f"字段 {key} 应为空字符串")

    def test_default_model_in_record(self) -> None:
        rec = cas._empty_record_base()
        self.assertEqual(rec["requested_model"], cas.DEFAULT_MODEL)

    def test_custom_model_in_record(self) -> None:
        rec = cas._empty_record_base(model="my-custom-model")
        self.assertEqual(rec["requested_model"], "my-custom-model")


if __name__ == "__main__":
    unittest.main()
