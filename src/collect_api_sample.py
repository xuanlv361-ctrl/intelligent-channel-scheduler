"""受控真实API调用采集

仅使用 Python 标准库 urllib，无第三方依赖。
API Key 仅从环境变量 WEIMETA_API_KEY 读取，绝不写入代码/日志/CSV。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any

# ── 项目根目录（基于 __file__ 计算，不依赖 cwd） ─────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
OBSERVATION_FILE = os.path.join(OUTPUT_DIR, "live_observation.json")
RECORDS_CSV = os.path.join(OUTPUT_DIR, "live_request_records.csv")

# ── 常量 ────────────────────────────────────────────────────────────

API_URL = "https://api-uat.weimeta.cn/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROMPT = "只回复：UAT结构化采集成功"
TIMEOUT_SECONDS = 60
ENV_KEY_NAME = "WEIMETA_API_KEY"

CSV_FIELDS = [
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


# ── 工具函数 ─────────────────────────────────────────────────────────


def beijing_now() -> str:
    """返回北京时间字符串 YYYY-MM-DD HH:mm:ss"""
    utc_dt = datetime.now(timezone.utc)
    bj_dt = utc_dt + timedelta(hours=8)
    return bj_dt.strftime("%Y-%m-%d %H:%M:%S")


def ensure_output_dir() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def read_api_key() -> str:
    """读取 WEIMETA_API_KEY，不存在或为空则返回空字符串"""
    return (os.environ.get(ENV_KEY_NAME) or "").strip()


def sanitize_error_text(text: str) -> str:
    """脱敏错误响应文本，隐藏可能的 API Key"""
    patterns = [
        r"sk-[A-Za-z0-9_-]{10,}",
        r"Authorization\s*:\s*Bearer\s+\S+",
        r"Bearer\s+[A-Za-z0-9_-]{20,}",
    ]
    result = text
    for pat in patterns:
        result = re.sub(pat, "***", result, flags=re.IGNORECASE)
    return result


# ── 请求构建（使用传入的 model 和 prompt） ────────────────────────────


def build_request_body(model: str, prompt: str) -> dict[str, Any]:
    """构建请求体，只包含 model / messages / stream，无 temperature 等"""
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }


def build_http_request(api_key: str, model: str, prompt: str) -> urllib.request.Request:
    body_bytes = json.dumps(build_request_body(model, prompt)).encode("utf-8")
    return urllib.request.Request(
        API_URL,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )


# ── 结构化错误分类 ────────────────────────────────────────────────────


def classify_error(status: int | None, reason: str, body_text: str) -> str:
    """返回统一错误分类名称"""
    # 连接层错误（status 为 None）
    if status is None:
        if reason == "timeout":
            return "timeout"
        return "protocol_compatibility_error"

    # HTTP 错误
    if status == 400:
        return "request_parameter_error"
    if status == 401:
        return "user_authentication_error"
    if status == 429:
        return "rate_limit"
    if status == 503:
        lower = body_text.lower()
        if "model_not_found" in lower or "no available channel" in lower:
            return "model_unavailable"
        return "platform_routing_error"
    return "unknown_error"


# ── 基础记录模板 ─────────────────────────────────────────────────────


def _empty_record_base(model: str = DEFAULT_MODEL) -> dict[str, Any]:
    return {
        "tested_at": beijing_now(),
        "environment": "uat_cn",
        "request_path": "/v1/chat/completions",
        "requested_model": model,
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
        "result": "",
        "error_category": "",
        "data_source": "live_api",
        "is_mock": False,
        "notes": "",
    }


# ── 响应解析 ─────────────────────────────────────────────────────────


def _get_latency_ms(start_ms: float) -> int:
    return int((time.time() * 1000) - start_ms)


def parse_success_response(response: urllib.request.addinfourl, start_ms: float, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    latency_ms = _get_latency_ms(start_ms)
    body = json.loads(response.read().decode("utf-8"))
    usage = body.get("usage") or {}

    # Request ID：仅从 Header，没有则留空
    request_id = response.headers.get("X-Oneapi-Request-Id", "")

    actual_model = body.get("model", "")

    # finish_reason：仅从 choices[0].finish_reason
    finish_reason_raw = ""
    choices = body.get("choices", [])
    if choices and isinstance(choices, list) and isinstance(choices[0], dict):
        finish_reason_raw = choices[0].get("finish_reason", "") or ""

    # Token 计数：真实存在则原样记录，不存在则留空
    prompt_tokens = usage.get("prompt_tokens", "")
    completion_tokens = usage.get("completion_tokens", "")
    total_tokens = usage.get("total_tokens", "")

    # extended_output_tokens = usage.output_tokens
    # 字段真实存在时原样记录（含 0），不存在时留空
    if "output_tokens" in usage:
        extended_output = usage["output_tokens"]
    else:
        extended_output = ""

    # ── usage 不一致检测 ─────────────────────────────────────
    # HTTP 200 时，如果 completion_tokens 和 extended_output_tokens 均存在且数值不相等，
    # 则标记为 success_with_warning / usage_field_mismatch
    result_field: str = "pass"
    error_category_field: str = ""
    notes_field: str = ""

    ct_is_present = completion_tokens != "" and completion_tokens is not None
    eo_is_present = extended_output != "" and extended_output is not None

    if ct_is_present and eo_is_present:
        ct_val = int(completion_tokens) if not isinstance(completion_tokens, int) else completion_tokens
        eo_val = int(extended_output) if not isinstance(extended_output, int) else extended_output
        if ct_val != eo_val:
            result_field = "success_with_warning"
            error_category_field = "usage_field_mismatch"
            notes_field = f"completion_tokens={ct_val}, extended_output_tokens={eo_val}"

    record = _empty_record_base(model=model)
    record.update({
        "tested_at": beijing_now(),
        "actual_model": actual_model,
        "http_status": 200,
        "latency_ms": latency_ms,
        "finish_reason": finish_reason_raw,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "extended_output_tokens": extended_output,
        "request_id": request_id,
        "result": result_field,
        "error_category": error_category_field,
        "notes": notes_field,
    })
    return record


def parse_http_error_response(error: urllib.error.HTTPError, start_ms: float, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    latency_ms = _get_latency_ms(start_ms)
    status = error.code
    text = error.read().decode("utf-8", errors="replace")
    text_safe = sanitize_error_text(text)
    category = classify_error(status, "", text_safe)

    request_id = error.headers.get("X-Oneapi-Request-Id", "") if error.headers else ""

    record = _empty_record_base(model=model)
    record.update({
        "http_status": status,
        "latency_ms": latency_ms,
        "request_id": request_id,
        "result": "fail",
        "error_category": category,
        "notes": f"请求失败：{category} - {text_safe[:200]}",
    })
    return record


def parse_connection_error(exception: Exception, start_ms: float, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """解析连接层错误（URLError / socket.timeout / TimeoutError）"""
    latency_ms = _get_latency_ms(start_ms)

    # 判断是否为真正的超时
    reason: str = ""
    if isinstance(exception, socket.timeout):
        reason = "timeout"
    elif isinstance(exception, TimeoutError):
        reason = "timeout"
    elif isinstance(exception, urllib.error.URLError):
        inner = exception.reason
        if isinstance(inner, str):
            reason = inner
        elif isinstance(inner, Exception):
            if isinstance(inner, (socket.timeout, TimeoutError)):
                reason = "timeout"
            else:
                reason = type(inner).__name__
        else:
            reason = str(inner)
    else:
        reason = type(exception).__name__

    category = classify_error(None, reason, "")

    record = _empty_record_base(model=model)
    record.update({
        "latency_ms": latency_ms,
        "result": "fail",
        "error_category": category,
        "notes": f"连接失败：{category} - {reason}",
    })
    return record


# ── 保存结果 ─────────────────────────────────────────────────────────


def save_json(record: dict[str, Any]) -> None:
    with open(OBSERVATION_FILE, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"[INFO] JSON 已保存至 {OBSERVATION_FILE}")


def append_csv(record: dict[str, Any]) -> None:
    file_exists = os.path.isfile(RECORDS_CSV)
    with open(RECORDS_CSV, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(record)
    print(f"[INFO] CSV 已追加至 {RECORDS_CSV}")


# ── 核心采集函数 ──────────────────────────────────────────────────────


def collect(api_key: str, model: str, prompt: str) -> dict[str, Any]:
    """执行一次 API 调用并返回记录字典"""
    ensure_output_dir()
    start_ms = time.time() * 1000

    req = build_http_request(api_key, model, prompt)

    try:
        response = urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS)
        record = parse_success_response(response, start_ms, model=model)
    except urllib.error.HTTPError as e:
        record = parse_http_error_response(e, start_ms, model=model)
    except (socket.timeout, TimeoutError, urllib.error.URLError) as e:
        record = parse_connection_error(e, start_ms, model=model)
    except Exception as e:
        # 兜底：其他未知异常
        record = _empty_record_base(model=model)
        record.update({
            "latency_ms": _get_latency_ms(start_ms),
            "result": "fail",
            "error_category": "unknown_error",
            "notes": f"未知异常：{type(e).__name__}: {e}",
        })

    return record


# ── 主入口 ────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="受控真实API调用采集器")
    parser.add_argument("--dry-run", action="store_true", help="仅检查配置，不发起真实请求")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名称（默认：{DEFAULT_MODEL}）")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help=f"用户提示语（默认：{DEFAULT_PROMPT}）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """返回退出码：0=成功/dry-run, 1=API失败, 2=配置错误"""
    args = parse_args(argv)

    api_key = read_api_key()

    if args.dry_run:
        print("========== Dry-Run 模式 ==========")
        print(f"API URL:       {API_URL}")
        print(f"默认模型:      {args.model}")
        print(f"请求方式:      POST, stream=false")
        print(f"超时:          {TIMEOUT_SECONDS}s")
        print(f"WEIMETA_API_KEY: {'已设置' if api_key else '未设置'}")
        print(f"输出 JSON:     {OBSERVATION_FILE}")
        print(f"输出 CSV:      {RECORDS_CSV}")
        print("==================================")
        return 0

    if not api_key:
        print("[FATAL] WEIMETA_API_KEY 未设置，无法发起真实请求。使用 --dry-run 查看配置。", file=sys.stderr)
        return 2

    record = collect(api_key, args.model, args.prompt)

    is_success = record.get("result") == "pass"

    print(f"\n========== 采集结果 ==========")
    print(f"tested_at:      {record.get('tested_at', '')}")
    print(f"http_status:    {record.get('http_status', '')}")
    print(f"latency_ms:     {record.get('latency_ms', '')}")
    print(f"actual_model:   {record.get('actual_model', '')}")
    print(f"request_id:     {record.get('request_id', '')}")
    print(f"result:         {record.get('result', '')}")
    print(f"error_category: {record.get('error_category', '')}")
    print("===============================\n")

    save_json(record)
    append_csv(record)

    return 0 if is_success else 1


if __name__ == "__main__":
    sys.exit(main())