"""Capture approved domestic-UAT JSON interfaces through the operator Chrome.

The browser injects its existing authentication.  This tool never reads or
persists request headers, cookies, browser storage, or credential values.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
import websocket

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.domestic_uat_chrome_manager import DomesticUatChromeManager


OUT = ROOT / "evidence" / "provider-correlation"
APPROVED = {
    "/console/models": "/api/pricing",
    "/console/billing/logs": "/api/log/self",
}
SENSITIVE = {
    "authorization", "cookie", "set-cookie", "password", "api_key",
    "apikey", "access_token", "refresh_token",
}


def sha(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def scrub(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): scrub(item) for key, item in value.items()
            if str(key).casefold().replace("-", "_") not in SENSITIVE
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def shape(value: object) -> object:
    if isinstance(value, dict):
        return {key: shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return {
            "type": "array", "count": len(value),
            "item_fields": sorted(value[0]) if value and isinstance(value[0], dict)
            else [],
        }
    return type(value).__name__


def capture() -> dict[str, object]:
    manager = DomesticUatChromeManager()
    targets = manager._request_json("/json/list")  # loopback-only CDP
    captured: dict[str, object] = {}
    for console_path, api_path in APPROVED.items():
        target = next((item for item in targets if
                       urlsplit(str(item.get("url") or "")).hostname ==
                       "uat.weimeta.cn" and
                       urlsplit(str(item.get("url") or "")).path == console_path), None)
        if target is None:
            raise RuntimeError(f"approved_operator_page_missing:{console_path}")
        socket = websocket.create_connection(
            str(target["webSocketDebuggerUrl"]), timeout=20,
            suppress_origin=True)
        try:
            socket.send(json.dumps({"id": 1, "method": "Network.enable"}))
            socket.send(json.dumps({"id": 2, "method": "Page.reload",
                                    "params": {"ignoreCache": True}}))
            response_meta = None
            while response_meta is None:
                event = json.loads(socket.recv())
                if event.get("method") != "Network.responseReceived":
                    continue
                params = event.get("params") or {}
                meta = params.get("response") or {}
                if (urlsplit(str(meta.get("url") or "")).hostname ==
                        "uat.weimeta.cn" and
                        urlsplit(str(meta.get("url") or "")).path == api_path):
                    response_meta = {
                        "request_id": params["requestId"],
                        "status": int(meta.get("status") or 0),
                        "url": str(meta.get("url") or ""),
                    }
            socket.send(json.dumps({
                "id": 3, "method": "Network.getResponseBody",
                "params": {"requestId": response_meta["request_id"]},
            }))
            body_result = None
            while body_result is None:
                event = json.loads(socket.recv())
                if event.get("id") == 3:
                    body_result = event.get("result") or {}
            if response_meta["status"] != 200:
                raise RuntimeError(
                    f"provider_interface_http_{response_meta['status']}:{api_path}")
            body = scrub(json.loads(str(body_result.get("body") or "{}")))
        finally:
            socket.close()
        captured[api_path] = {
            "method": "GET",
            "status": response_meta["status"],
            "query_keys": sorted(item.split("=")[0] for item in
                                 urlsplit(response_meta["url"]).query.split("&")
                                 if item),
            "schema": shape(body),
            "payload_sha256": sha(body),
            "body": body if api_path == "/api/pricing" else None,
        }
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "schema_version": "domestic_uat_provider_interfaces_v1",
        "captured_at": now,
        "environment_id": "china_uat",
        "source_origin": "https://uat.weimeta.cn",
        "credentials_captured": False,
        "request_headers_captured": False,
        "interfaces": captured,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "provider-interface-capture.json"
    target.write_text(json.dumps(
        result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "evidence_file": str(target.relative_to(ROOT)),
        "evidence_sha256": sha(result),
        "interfaces": sorted(captured),
    }


if __name__ == "__main__":
    print(json.dumps(capture(), ensure_ascii=False, sort_keys=True))
