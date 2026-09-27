"""Ephemeral, credential-scoped Weimeta UAT model catalog."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UatModelCatalog:
    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def credential_id(api_key: str, environment_id: str = "china_uat") -> str:
        return f"{environment_id}:{hashlib.sha256(api_key.encode()).hexdigest()}"

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def cached(self, api_key: str, environment_id: str = "china_uat") -> dict[str, Any] | None:
        if not api_key:
            return None
        with self._lock:
            entry = self._cache.get(self.credential_id(api_key, environment_id))
            if not entry or time.monotonic() - entry[0] >= self.ttl_seconds:
                return None
            return entry[1]

    def model_ids(self, api_key: str, environment_id: str = "china_uat") -> set[str] | None:
        result = self.cached(api_key, environment_id)
        return {item["id"] for item in result["models"]} if result else None

    def fetch(
        self, api_key: str, transport: Callable[[str, str, int], dict],
        timeout: int, refresh: bool = False, environment_id: str = "china_uat",
        environment_name: str = "国内 UAT",
        models_url: str | None = "https://api-uat.weimeta.cn/v1/models",
    ) -> tuple[dict[str, Any], bool]:
        error = lambda code, message, status=None: self._error(
            code, message, status, environment_id, environment_name)
        if not models_url:
            code = "environment_configuration_incomplete"
            return self._error(code, "当前环境的模型 API 地址尚未确认。",
                               environment_id=environment_id, environment_name=environment_name), False
        if not api_key:
            return error("uat_key_not_configured", "尚未配置当前环境的 API Key。"), False
        if not refresh:
            result = self.cached(api_key, environment_id)
            if result:
                return {**result, "cache": "hit"}, False
        try:
            response = transport(models_url, api_key, timeout)
        except (TimeoutError, OSError):
            return error("uat_model_catalog_timeout", "读取模型目录超时。"), True
        code = response.get("status")
        content_type = str(response.get("headers", {}).get("content-type", "")).lower()
        body = response.get("body", b"")[:1_000_000]
        stripped = body.lstrip().lower()
        if code == 401:
            return error("uat_authentication_failed", "当前环境 API Key 鉴权失败。", code), True
        if code == 403:
            return error("uat_model_catalog_forbidden", "The API key cannot read this model catalog.", code), True
        if code == 429:
            return error("uat_model_catalog_rate_limited", "模型目录请求受到速率限制。", code), True
        if "html" in content_type or stripped.startswith((b"<html", b"<!doctype html")):
            return error("uat_model_catalog_html_response", "模型目录返回了 HTML 错误页。", code), True
        if isinstance(code, int) and code >= 500:
            return error("uat_model_catalog_upstream_error", "模型目录服务暂时不可用。", code), True
        if code != 200:
            return error("uat_model_catalog_request_failed", "模型目录请求失败。", code), True
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error("uat_model_catalog_invalid_json", "模型目录返回了非 JSON 响应。", code), True
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list):
            return error("uat_model_catalog_invalid_schema", "模型目录响应结构无效。", code), True
        unique: dict[str, dict[str, Any]] = {}
        for raw in parsed["data"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                continue
            model_id = raw["id"].strip()
            if not model_id or len(model_id) > 256 or any(ord(c) < 33 for c in model_id):
                continue
            owned_by = raw.get("owned_by") if isinstance(raw.get("owned_by"), str) else None
            unique[model_id] = {
                "id": model_id, "display_name": model_id, "owned_by": owned_by,
                "execution_allowed": True, "stream_capability": "pending_confirmation",
            }
        models = sorted(unique.values(), key=lambda item: (item["display_name"].casefold(), item["id"]))
        result = {
            "status": "ready", "catalog_status": "ready", "source": "remote_model_endpoint", "fetched_at": _now(),
            "environment_id": environment_id, "environment_name": environment_name,
            "model_count": len(models), "models": models, "cache": "miss",
        }
        for item in models:
            item["source_environment"] = environment_id
        with self._lock:
            self._cache[self.credential_id(api_key, environment_id)] = (time.monotonic(), result)
        return result, True

    @staticmethod
    def _error(code: str, message: str, http_status: int | None = None,
               environment_id: str = "china_uat", environment_name: str = "国内 UAT") -> dict[str, Any]:
        return {
            "status": "error", "catalog_status": "blocked", "source": "remote_model_endpoint", "fetched_at": _now(),
            "environment_id": environment_id, "environment_name": environment_name,
            "model_count": 0, "models": [], "error": {"code": code, "message": message},
            "http_status": http_status, "cache": "miss",
        }
