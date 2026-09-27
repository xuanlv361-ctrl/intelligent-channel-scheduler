"""Verify the running main API -> Formal Runtime -> independent Host chain."""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:5174"
ORIGIN = BASE


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    csrf = ""

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        nonlocal csrf
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Origin": ORIGIN}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = csrf
        request = Request(BASE + path, method=method, data=data, headers=headers)
        try:
            response = opener.open(request, timeout=15)
        except HTTPError as exc:
            rotated = exc.headers.get("X-CSRF-Token")
            if rotated:
                csrf = rotated
            return exc.code, json.loads(exc.read().decode("utf-8"))
        rotated = response.headers.get("X-CSRF-Token")
        if rotated:
            csrf = rotated
        return response.status, json.loads(response.read().decode("utf-8"))

    bootstrap_http, bootstrap = call("POST", "/api/v1/security/session/bootstrap", {})
    if bootstrap_http != 200:
        raise SystemExit(f"bootstrap_failed:{bootstrap_http}")
    csrf = str(bootstrap["csrf_token"])
    hosts_http, hosts = call("GET", "/api/v1/skills/hosts")
    candidates = [item for item in hosts.get("items", [])
                  if item.get("base_url") == "http://127.0.0.1:8010"
                  and item.get("protocol_version") == "1.0"
                  and item.get("status") == "connected" and item.get("enabled")]
    if not candidates:
        raise SystemExit("connected_host_not_found")
    host = candidates[0]
    host_id = str(host["host_id"])
    install_http, installation = call("POST", f"/api/v1/skills/hosts/{host_id}/installations",
        {"skill_id": "frontend-design", "version": "0.2.0"})
    if install_http != 200:
        raise SystemExit(f"install_failed:{install_http}")
    installation_id = str(installation["installation_id"])
    invoke_http, invocation = call("POST", f"/api/v1/skills/hosts/{host_id}/invoke",
        {"skill_id": "frontend-design", "arguments": {
            "task": "检查真实外部宿主控制面的状态层级与窄屏布局。"}})
    disable_http, _ = call("POST",
        f"/api/v1/skills/hosts/{host_id}/installations/{installation_id}/disable", {})
    rejected_http, rejected = call("POST", f"/api/v1/skills/hosts/{host_id}/invoke",
        {"skill_id": "frontend-design", "arguments": {"task": "停用后调用应被拒绝。"}})
    enable_http, _ = call("POST",
        f"/api/v1/skills/hosts/{host_id}/installations/{installation_id}/enable", {})
    uninstall_http, _ = call("DELETE",
        f"/api/v1/skills/hosts/{host_id}/installations/{installation_id}", {})
    error_detail = rejected.get("detail") if isinstance(rejected, dict) else {}
    error_code = error_detail.get("code") if isinstance(error_detail, dict) else None
    result = invocation.get("data") if isinstance(invocation, dict) else {}
    evidence = {
        "tested_at": now(), "main_url": BASE, "host_url": host["base_url"],
        "bootstrap_http": bootstrap_http, "hosts_http": hosts_http,
        "host_id": host_id, "installation_id": installation_id,
        "install_http": install_http, "invoke_http": invoke_http,
        "disable_http": disable_http, "disabled_invoke_http": rejected_http,
        "disabled_invoke_error": error_code, "enable_http": enable_http,
        "uninstall_http": uninstall_http,
        "invocation_id": invocation.get("invocation_id"),
        "audit_id": invocation.get("audit_id"),
        "remote_invocation_id": (result or {}).get("remote_invocation_id"),
        "remote_audit_id": (result or {}).get("remote_audit_id"),
        "skill_id": "frontend-design", "skill_version": "0.2.0",
        "data_source": (result or {}).get("data_source"),
        "passed": all(status == 200 for status in (
            bootstrap_http, hosts_http, install_http, invoke_http, disable_http,
            enable_http, uninstall_http)) and error_code == "skill_not_enabled",
    }
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    evidence["checksum"] = hashlib.sha256(canonical.encode()).hexdigest()
    target = ROOT / "evidence" / "external-agent-host" / "main-http-host-chain.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"evidence": str(target.relative_to(ROOT)), "passed": evidence["passed"],
                      "invocation_id": evidence["invocation_id"],
                      "remote_invocation_id": evidence["remote_invocation_id"],
                      "checksum": evidence["checksum"]}))
    raise SystemExit(0 if evidence["passed"] else 1)


if __name__ == "__main__":
    main()
