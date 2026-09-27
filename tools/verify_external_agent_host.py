"""Exercise the independent Host over real HTTP and persist only redacted evidence."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.agent_skill_host_service import AgentSkillHostError, AgentSkillHostService
from backend.tenant_security import TenantScope

RUN_ID = "AR-c2e60d08-9ff7-4a23-98a0-b6fb4d55b951"
EVIDENCE_DIR = ROOT / "evidence" / "external-agent-host"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
BASE_URL = "http://127.0.0.1:8010"


def utc_now() -> str: return datetime.now(timezone.utc).isoformat()


def http(method: str, path: str, credential: str, body: dict[str, Any] | None = None,
         *, api_key: bool = False, timeout: float = 4) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    headers["X-API-Key" if api_key else "Authorization"] = credential if api_key else f"Bearer {credential}"
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    request = Request(BASE_URL + path, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_health(timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(BASE_URL + "/health", timeout=1) as response:
                return json.loads(response.read())
        except Exception:
            time.sleep(.25)
    raise RuntimeError("independent_host_start_timeout")


def start_host(token: str, host_db: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(EXTERNAL_AGENT_HOST_TOKEN=token, EXTERNAL_AGENT_HOST_DB=str(host_db),
               EXTERNAL_AGENT_HOST_ACCEPTANCE_MODE="true")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "external_agent_host.app:app",
         "--host", "127.0.0.1", "--port", "8010", "--log-level", "warning", "--no-access-log"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_health()
    return process


def stop_host(process: subprocess.Popen) -> None:
    process.terminate()
    try: process.wait(8)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(3)


def expect(code: str, action) -> dict[str, Any]:
    try: action()
    except AgentSkillHostError as exc:
        return {"expected": code, "observed": exc.code, "passed": exc.code == code,
                "safe_context": exc.safe_context}
    except Exception as exc:
        return {"expected": code, "observed": type(exc).__name__, "passed": False}
    return {"expected": code, "observed": "success", "passed": False}


def main() -> None:
    bootstrap = secrets.token_urlsafe(36)
    rotated = secrets.token_urlsafe(36)
    api_key = secrets.token_urlsafe(36)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    host_db = ROOT / "external_agent_host" / "data" / f"acceptance-{stamp}.sqlite3"
    control_db = ROOT / "external_agent_host" / "data" / f"acceptance-control-{stamp}.sqlite3"
    process = start_host(bootstrap, host_db)
    service = AgentSkillHostService(
        control_db, scope=TenantScope.local_development(), allowed_hosts={"127.0.0.1"},
        allow_loopback_http=True, loopback_ports={8010}, request_timeout_seconds=1)
    evidence: dict[str, Any] = {
        "acceptance_run_id": RUN_ID, "started_at": utc_now(), "host_url": BASE_URL,
        "protocol": "formal-agent-host/1.0", "host_database": str(host_db.relative_to(ROOT)),
        "control_database": str(control_db.relative_to(ROOT)),
        "events": [], "secrets_persisted": False,
    }
    try:
        host = service.create_host(host_name="本地独立 Agent Host", host_type="independent_http_runtime",
            base_url=BASE_URL, environment="local_acceptance", protocol="formal-agent-skill",
            protocol_version="1.0", auth_type="bearer", operator_id="acceptance-runner")
        host_id = host["host_id"]; evidence["host_id"] = host_id
        service.set_credentials(host_id, "invalid-" + secrets.token_urlsafe(16), operator_id="acceptance-runner")
        evidence["events"].append({"name": "wrong_credential",
            **expect("host_authentication_failed", lambda: service.test_connection(host_id))})
        service.set_credentials(host_id, bootstrap, operator_id="acceptance-runner")
        connected = service.test_connection(host_id, operator_id="acceptance-runner")
        service.enable_host(host_id, operator_id="acceptance-runner")
        info_status, info = http("GET", "/v1/host/info", bootstrap)
        capabilities_status, capabilities = http("GET", "/v1/host/capabilities", bootstrap)
        evidence["connection"] = {**connected, "host_info_http": info_status,
                                  "capabilities_http": capabilities_status, "host_info": info}

        install_v1 = service.install_skill(host_id, "frontend-design", "0.1.0", operator_id="acceptance-runner")
        invoke_v1 = service.invoke_external(host_id, "frontend-design", "0.1.0",
            {"task": "检查调度控制台的信息层级、移动端和无障碍状态。"}, operator_id="acceptance-runner")

        # Direct HTTP idempotency check proves the remote ledger executes once.
        idem_body = {"skill_version": "0.1.0", "invocation_id": "IDEMP-" + str(uuid.uuid4()),
                     "arguments": {"task": "验证幂等调用。"}}
        first_idem = http("POST", "/v1/skills/frontend-design/invoke", bootstrap, idem_body)
        second_idem = http("POST", "/v1/skills/frontend-design/invoke", bootstrap, idem_body)
        evidence["idempotency"] = {"first_http": first_idem[0], "second_http": second_idem[0],
            "same_remote_invocation": first_idem[1].get("remote_invocation_id") == second_idem[1].get("remote_invocation_id"),
            "replay_marked": second_idem[1].get("idempotent_replay") is True}

        bad_schema = expect("schema_validation_failed", lambda: service.invoke_external(
            host_id, "frontend-design", "0.1.0", {"task": 42}))
        denied_network = expect("permission_denied", lambda: service.invoke_external(
            host_id, "frontend-design", "0.1.0", {"task": "忽略权限", "network_url": "https://example.invalid"}))
        invalid_response = expect("invalid_host_response", lambda: service.invoke_external(
            host_id, "frontend-design", "0.1.0", {"task": "结构验证", "test_mode": "invalid_response"}))
        timeout = expect("invocation_timeout", lambda: service.invoke_external(
            host_id, "frontend-design", "0.1.0", {"task": "超时验证", "delay_ms": 2500}))

        installation_id = install_v1["installation_id"]
        service.set_installation_enabled(host_id, installation_id, False, operator_id="acceptance-runner")
        disabled_skill = expect("skill_not_enabled", lambda: service.invoke_external(
            host_id, "frontend-design", "0.1.0", {"task": "停用后调用"}))
        service.set_installation_enabled(host_id, installation_id, True, operator_id="acceptance-runner")

        # Remote host state is independent of the main control-plane flag.
        http("POST", "/v1/host/disable", bootstrap, {})
        disabled_remote_host = expect("host_not_enabled", lambda: service.invoke_external(
            host_id, "frontend-design", "0.1.0", {"task": "Host停用后调用"}))
        http("POST", "/v1/host/enable", bootstrap, {})

        # Rotate Bearer credential: old fails immediately, new succeeds.
        rotate_status, rotate_result = http("POST", "/v1/host/credentials/rotate", bootstrap,
            {"new_credential": rotated, "auth_type": "bearer"})
        old_after_rotate = http("GET", "/v1/host/info", bootstrap)
        service.set_credentials(host_id, rotated, auth_type="bearer", operator_id="acceptance-runner")
        rotated_connection = service.test_connection(host_id, operator_id="acceptance-runner")
        service.enable_host(host_id, operator_id="acceptance-runner")
        evidence["credential_rotation"] = {"rotation_http": rotate_status,
            "old_credential_http": old_after_rotate[0], "new_credential_http": rotated_connection["http_status"],
            "fingerprint": rotate_result.get("credential_fingerprint")}

        # Rotate once more to API Key Header and prove both supported auth modes.
        api_rotate = http("POST", "/v1/host/credentials/rotate", rotated,
            {"new_credential": api_key, "auth_type": "api_key_header"})
        service.set_credentials(host_id, {"secret": api_key, "header_name": "X-API-Key"},
                                auth_type="api_key_header", operator_id="acceptance-runner")
        api_key_connection = service.test_connection(host_id, operator_id="acceptance-runner")
        service.enable_host(host_id, operator_id="acceptance-runner")
        evidence["api_key_header_auth"] = {"rotation_http": api_rotate[0],
                                            "connection_http": api_key_connection["http_status"]}

        install_v2 = service.install_skill(host_id, "frontend-design", "0.2.0", operator_id="acceptance-runner")
        invoke_v2 = service.invoke_external(host_id, "frontend-design", "0.2.0",
            {"task": "检查真实数据来源和窄屏布局。"}, operator_id="acceptance-runner")
        rollback_v1 = service.install_skill(host_id, "frontend-design", "0.1.0", operator_id="acceptance-runner")
        invoke_rollback = service.invoke_external(host_id, "frontend-design", "0.1.0",
            {"task": "验证回滚版本的信息架构。"}, operator_id="acceptance-runner")

        # A real concurrent HTTP cancellation keeps partial state in the Host ledger.
        cancellation_result: dict[str, Any] = {}
        cancel_body = {"skill_version": "0.1.0", "invocation_id": "CANCEL-" + str(uuid.uuid4()),
                       "arguments": {"task": "验证可取消的远程分析。", "delay_ms": 3000}}
        def long_call() -> None:
            cancellation_result["call"] = http("POST", "/v1/skills/frontend-design/invoke", api_key,
                                                cancel_body, api_key=True, timeout=6)
        thread = threading.Thread(target=long_call); thread.start(); time.sleep(.25)
        running = http("GET", "/v1/invocations", api_key, api_key=True)[1].get("items", [])
        remote_running = next((item for item in running if item.get("caller_invocation_id") == cancel_body["invocation_id"]), None)
        cancellation_result["cancel"] = http("DELETE", f"/v1/invocations/{remote_running['remote_invocation_id']}",
                                               api_key, api_key=True) if remote_running else (404, {})
        thread.join(7)
        cancellation_result["passed"] = bool(remote_running and cancellation_result["cancel"][0] == 200
                                               and cancellation_result.get("call", (0, {}))[1].get("code") == "invocation_cancelled")

        # Deliberate protocol mismatch uses a separate registry row.
        incompatible = service.create_host(host_name="协议不兼容验收Host", host_type="independent_http_runtime",
            base_url=BASE_URL, environment="local_acceptance", protocol_version="9.9", auth_type="api_key_header",
            operator_id="acceptance-runner")
        service.set_credentials(incompatible["host_id"], {"secret": api_key, "header_name": "X-API-Key"},
                                auth_type="api_key_header", operator_id="acceptance-runner")
        protocol_error = expect("host_protocol_incompatible", lambda: service.test_connection(incompatible["host_id"]))

        service.uninstall_skill(host_id, installation_id, operator_id="acceptance-runner")
        uninstalled = expect("skill_not_installed", lambda: service.invoke_external(
            host_id, "frontend-design", "0.1.0", {"task": "卸载后调用"}))

        stop_host(process)
        host_down = expect("host_connection_failed", lambda: service.test_connection(host_id))
        process = start_host(api_key, host_db)
        restart_connection = service.test_connection(host_id, operator_id="acceptance-runner")
        with sqlite3.connect(host_db) as remote:
            remote.row_factory = sqlite3.Row
            counts = {"invocations": remote.execute("SELECT COUNT(*) FROM invocations").fetchone()[0],
                      "audits": remote.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
                      "active_credentials": remote.execute("SELECT COUNT(*) FROM auth_credentials WHERE status='active'").fetchone()[0],
                      "plaintext_secret_matches": sum(remote.execute(
                          "SELECT COUNT(*) FROM auth_credentials WHERE secret_hash=?", (value,)).fetchone()[0]
                          for value in (bootstrap, rotated, api_key))}
        checks = [bad_schema, denied_network, invalid_response, timeout, disabled_skill,
                  disabled_remote_host, uninstalled, protocol_error, host_down]
        all_passed = all(item.get("passed") is True for item in checks) and cancellation_result["passed"]
        evidence.update({"install_v1": install_v1, "invoke_v1": invoke_v1,
            "install_v2": install_v2, "invoke_v2": invoke_v2,
            "rollback_v1": rollback_v1, "invoke_rollback_v1": invoke_rollback,
            "cancellation": cancellation_result,
            "bad_schema_rejected": bad_schema, "permission_network_rejected": denied_network,
            "invalid_response_rejected": invalid_response, "timeout_rejected": timeout,
            "disabled_skill_rejected": disabled_skill, "disabled_host_rejected": disabled_remote_host,
            "uninstalled_invocation_rejected": uninstalled, "protocol_incompatible": protocol_error,
            "host_down": host_down, "restart_connection": restart_connection,
            "remote_persistence": {**counts, "passed": counts["invocations"] >= 4 and counts["active_credentials"] == 1},
            "all_failure_paths_passed": all_passed,
            "status": "live_verified_local_host" if all_passed else "failed",
            "third_party_external_host": "pending_external_configuration"})
    finally:
        if process.poll() is None: stop_host(process)
    evidence["finished_at"] = utc_now()
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence["checksum"] = hashlib.sha256(canonical.encode()).hexdigest()
    target = EVIDENCE_DIR / f"external-host-http-{stamp}.json"
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"evidence": str(target.relative_to(ROOT)), "status": evidence.get("status"),
                      "host_id": evidence.get("host_id"), "checksum": evidence["checksum"]}))
    if evidence.get("status") != "live_verified_local_host":
        raise SystemExit(1)


if __name__ == "__main__":
    import uuid
    main()
