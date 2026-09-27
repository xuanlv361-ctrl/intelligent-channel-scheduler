from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from backend import app as application
from backend.credential_store import SessionCredentialStore
from backend.security.http import EnterpriseHTTPRuntime
from backend.uat_http_workbench_service import (
    CredentialConnectionRegistry, WorkbenchError, default_transport, normalize_request,
    validate_workbench,
)
from backend.uat_service import UatSettings
from services.console_service import Store
from tests.enterprise_test_client import authenticated_test_client


ORIGIN = {"Origin": "http://127.0.0.1:5174", "Content-Type": "application/json"}
READ_ORIGIN = {"Origin": "http://127.0.0.1:5174", "Referer": "http://127.0.0.1:5174/routing/execute", "Sec-Fetch-Site": "same-origin"}
SECRET = "test-only-workbench-key-not-real"
ROOT = Path(__file__).resolve().parents[1]
TEST_MASTER = base64.b64encode(
    b"uat-workbench-test-signing-material-00000000000001").decode()


def settings(enabled: bool = True,
             base_url: str = "https://api-uat.weimeta.cn") -> UatSettings:
    return UatSettings(
        environment="uat", base_url=base_url, api_key="",
        enabled=enabled, allowed_hosts=("api-uat.weimeta.cn",), timeout_seconds=30,
        max_tokens=None, daily_request_limit=None, daily_budget_cny=None,
        max_request_cost_cny=.2,
    )


def body(method="GET", path="/v1/models", **overrides):
    value = {
        "method": method, "environment_id": "china_uat", "path": path,
        "query_params": [], "headers": [], "auth_session_id": "opaque",
        "auth": {"method": "bearer", "header_name": "Authorization", "prefix": "Bearer"},
        "body": {"type": "none", "value": None}, "content_type": None,
        "timeout_seconds": 10, "stream": False, "model": None,
        "channel_id": None, "routing_policy": None, "execution_limits": {"max_attempts": 1},
    }
    value.update(overrides)
    return value


def client(monkeypatch, tmp_path, roles=("domestic_uat_operator",)):
    isolated_runtime = EnterpriseHTTPRuntime.build(
        profile=application.ENTERPRISE_HTTP.profile,
        database_path=tmp_path / "enterprise-security.sqlite3",
        root=ROOT,
        environ={"ROUTING_CONSOLE_ENTERPRISE_SIGNING_KEY": TEST_MASTER},
    )
    assert isolated_runtime.ready
    for name in ("sessions", "csrf", "service_identities", "database_path",
                 "unavailable_reason"):
        monkeypatch.setattr(application.ENTERPRISE_HTTP, name,
                            getattr(isolated_runtime, name))
    monkeypatch.setattr(application, "SESSION_CREDENTIALS", SessionCredentialStore(30, 20))
    monkeypatch.setattr(application, "WORKBENCH_CONNECTIONS", CredentialConnectionRegistry())
    monkeypatch.setattr(application, "STORE", Store(tmp_path / "workbench.sqlite3"))
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "workbench.sqlite3")
    return authenticated_test_client(
        application.app, runtime=application.ENTERPRISE_HTTP,
        roles=roles, base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50123))


def save_and_test(c, monkeypatch):
    saved = c.post("/api/v1/environments/china_uat/credentials/temporary",
                   headers=ORIGIN, json={"api_key": SECRET})
    assert saved.status_code == 200 and SECRET not in saved.text
    sid = saved.json()["auth_session_id"]
    monkeypatch.setattr(application, "WORKBENCH_TRANSPORT", lambda *_: {
        "status": 200, "headers": {"content-type": "application/json"},
        "body": b'{"data":[]}', "elapsed_ms": 3})
    tested = c.post("/api/v1/environments/china_uat/credentials/temporary/test",
                    headers=ORIGIN, json={"auth_session_id": sid})
    assert tested.status_code == 200 and tested.json()["connection_status"] == "success"
    return sid


def test_explicit_vault_reuse_creates_only_a_redacted_temporary_session(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)

    class Vault:
        def load(self, _scope):
            return SECRET, {"key_fingerprint": "sha256:test-only", "created_at": "2026-08-05T00:00:00Z"}

    monkeypatch.setattr(application, "PERSISTENT_CREDENTIALS", Vault())
    restored = c.post(
        "/api/v1/environments/china_uat/credentials/temporary/from-persistent",
        headers=ORIGIN, json={},
    )
    assert restored.status_code == 200
    assert restored.json()["credential_source"] == "temporary_session"
    assert restored.json()["connection_status"] == "not_tested"
    assert restored.json()["auth_session_id"]
    assert SECRET not in restored.text

    monkeypatch.setattr(application, "WORKBENCH_TRANSPORT", lambda *_: {
        "status": 200, "headers": {"content-type": "application/json"},
        "body": b'{"data":[]}', "elapsed_ms": 3,
    })
    tested = c.post(
        "/api/v1/environments/china_uat/credentials/temporary/test",
        headers=ORIGIN, json={"auth_session_id": restored.json()["auth_session_id"]},
    )
    assert tested.status_code == 200
    assert tested.json()["connection_status"] == "success"
    assert SECRET not in tested.text


def test_static_validation_needs_no_key_and_makes_no_network():
    result = validate_workbench(body(), settings(enabled=False), for_execution=False)
    assert result["valid"] and result["structurally_valid"]
    assert "real_execution_disabled" not in result["blocking_reasons"]


def test_static_validation_api_uses_uat_permission_and_no_network(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    monkeypatch.setattr(application, "WORKBENCH_TRANSPORT",
                        lambda *_: pytest.fail("static validation called transport"))
    result = c.post("/api/v1/environments/china_uat/request-workbench/validate",
                    headers=ORIGIN, json=body())
    assert result.status_code == 200
    assert result.json()["structurally_valid"] is True


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_all_reviewed_methods_are_preserved(method):
    payload = body(method=method, path="/v1/example")
    if method not in {"GET", "HEAD"}:
        payload["body"] = {"type": "json", "value": {"value": 1}}
    assert normalize_request(payload, settings())["method"] == method


def test_query_encoding_and_json_body_are_deterministic():
    payload = body(query_params=[{"name": "q", "value": "a b", "enabled": True}])
    assert normalize_request(payload, settings())["url"].endswith("/v1/models?q=a+b")
    post = body("POST", "/v1/example", body={"type": "json", "value": '{"x":1}'})
    assert normalize_request(post, settings())["body_bytes"] == b'{"x":1}'


def test_form_data_and_urlencoded_use_distinct_wire_formats():
    fields = [{"name": "name", "value": "a b", "enabled": True}]
    multipart = normalize_request(body("POST", "/v1/example",
        body={"type": "form_data", "value": fields}), settings())
    encoded = normalize_request(body("POST", "/v1/example",
        body={"type": "urlencoded", "value": fields}), settings())
    assert multipart["content_type"].startswith("multipart/form-data; boundary=ics-")
    assert b'Content-Disposition: form-data; name="name"' in multipart["body_bytes"]
    assert encoded["content_type"] == "application/x-www-form-urlencoded"
    assert encoded["body_bytes"] == b"name=a+b"


@pytest.mark.parametrize("path", ["https://evil.example/v1/models", "//evil.example/x", "/v1/../admin", "/v1/%2e%2e/admin", "/v1\\models"])
def test_external_hosts_and_path_traversal_fail_closed(path):
    with pytest.raises(WorkbenchError):
        normalize_request(body(path=path), settings())


def test_environment_base_url_must_be_exact_https_uat_host():
    with pytest.raises(WorkbenchError, match="uat_environment_target_not_allowed"):
        normalize_request(body(), settings(base_url="https://api-uat.weimeta.cn.evil.example"))


def test_private_dns_resolution_is_rejected_before_transport(monkeypatch):
    spec = normalize_request(body(), settings())
    monkeypatch.setattr("backend.uat_http_workbench_service.socket.getaddrinfo",
                        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(WorkbenchError, match="uat_dns_address_not_public"):
        default_transport(spec, SECRET)


@pytest.mark.parametrize("name", ["Authorization", "Host", "Cookie", "Content-Length", "Proxy-Authorization", "X-Internal-Trace"])
def test_dangerous_headers_are_rejected(name):
    with pytest.raises(WorkbenchError, match="uat_header_forbidden"):
        normalize_request(body(headers=[{"name": name, "value": SECRET}]), settings())


def test_real_execution_requires_key_but_not_a_separate_connection_test():
    assert validate_workbench(body(), settings(), for_execution=True)["blocking_reasons"] == ["temporary_api_key_required"]
    assert validate_workbench(
        body(), settings(), credential_configured=True,
        connection_verified=False, for_execution=True)["blocking_reasons"] == []
    assert "real_execution_disabled" in validate_workbench(
        body(), settings(False), credential_configured=True,
        connection_verified=True, for_execution=True)["blocking_reasons"]


def test_pending_text_capability_warns_but_does_not_block_execution():
    payload = body("POST", "/v1/chat/completions", model="model-pending",
                   body={"type": "json", "value": {"messages": [{"role": "user", "content": "hello"}]}})
    result = validate_workbench(
        payload, settings(), credential_configured=True, connection_verified=True,
        capabilities={"model-pending": {"status": "pending_confirmation"}},
        for_execution=True)
    assert result["valid"] is True
    assert result["blocking_reasons"] == []
    assert "capability_pending" in result["warnings"]


def test_explicit_unsupported_capability_still_fails_closed():
    payload = body("POST", "/v1/chat/completions", model="model-unsupported",
                   body={"type": "json", "value": {"messages": [{"role": "user", "content": "hello"}]}})
    result = validate_workbench(
        payload, settings(), credential_configured=True, connection_verified=True,
        capabilities={"model-unsupported": {"status": "unsupported"}},
        for_execution=True)
    assert result["valid"] is False
    assert result["blocking_reasons"] == ["capability_unsupported"]


def test_automatic_selection_does_not_require_preselected_model():
    payload = body("POST", "/v1/chat/completions", model_selection_mode="automatic",
                   body={"type": "json", "value": {"messages": [{"role": "user", "content": "hello"}]}})
    result = validate_workbench(
        payload, settings(), credential_configured=True, connection_verified=True,
        capabilities={}, for_execution=True)
    assert result["valid"] is True
    assert "model_required" not in result["blocking_reasons"]


def test_temporary_key_lifecycle_and_key_change_invalidates_test(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    sid = save_and_test(c, monkeypatch)
    status_response = c.get("/api/v1/environments/china_uat/credentials/temporary/status", headers=READ_ORIGIN)
    assert status_response.status_code == 200, status_response.text
    status = status_response.json()
    assert status["configured"] and status["connection_status"] == "success"
    replaced = c.post("/api/v1/environments/china_uat/credentials/temporary",
                      headers=ORIGIN, json={"api_key": SECRET + "-changed"})
    assert replaced.json()["auth_session_id"] == sid
    status = c.get("/api/v1/environments/china_uat/credentials/temporary/status", headers=READ_ORIGIN).json()
    assert status["connection_status"] == "not_tested"
    cleared = c.delete("/api/v1/environments/china_uat/credentials/temporary", headers=ORIGIN)
    assert cleared.json()["configured"] is False
    assert SECRET not in (tmp_path / "workbench.sqlite3").read_bytes().decode(errors="ignore")


def test_connection_test_uses_selected_auth_contract_and_scopes_model_cache(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    saved = c.post("/api/v1/environments/china_uat/credentials/temporary",
                   headers=ORIGIN, json={"api_key": SECRET}).json()
    captured = {}
    def transport(spec, key):
        captured.update(spec)
        assert key == SECRET
        return {"status": 200, "headers": {"content-type": "application/json"},
                "body": b'{"data":[{"id":"model-a","owned_by":"provider"}]}',
                "elapsed_ms": 1}
    monkeypatch.setattr(application, "WORKBENCH_TRANSPORT", transport)
    tested = c.post("/api/v1/environments/china_uat/credentials/temporary/test",
        headers=ORIGIN, json={"auth_session_id": saved["auth_session_id"],
          "auth": {"method": "api_key_header", "header_name": "X-API-Key", "prefix": ""}})
    assert tested.status_code == 200 and tested.json()["model_count"] == 1
    assert captured["method"] == "GET" and captured["path"] == "/v1/models"
    assert captured["auth"] == {"method": "api_key_header", "header_name": "X-API-Key", "prefix": ""}
    catalog = c.post("/api/v1/environments/china_uat/request-workbench/models",
        headers=ORIGIN, json={"auth_session_id": saved["auth_session_id"]})
    assert catalog.status_code == 200
    assert [item["id"] for item in catalog.json()["models"]] == ["model-a"]
    assert SECRET not in tested.text + catalog.text


def test_api_executes_actual_methods_preserves_http_status_and_redacts(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    c.put("/api/v1/environments/china_uat/execution-state", json={"enabled": True})
    sid = save_and_test(c, monkeypatch)
    calls = []
    def transport(spec, key):
        calls.append((spec["method"], spec["path"], spec["url"], key))
        return {"status": 405 if spec["method"] == "DELETE" else 200,
                "headers": {"Content-Type": "application/json", "Set-Cookie": SECRET,
                            "X-Request-ID": "upstream-safe-id"},
                "body": json.dumps({"ok": True, "Authorization": f"Bearer {SECRET}"}).encode(),
                "elapsed_ms": 8,
                "first_token_latency_ms": 2.5 if spec["method"] == "POST" else None}
    monkeypatch.setattr(application, "WORKBENCH_TRANSPORT", transport)
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        payload = body(method, "/v1/example", auth_session_id=sid)
        if method not in {"GET", "HEAD"}:
            payload["body"] = {"type": "json", "value": {"safe": True}}
        response = c.post("/api/v1/environments/china_uat/request-workbench/execute",
                          json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["method"] == method and result["network_called"] is True
        assert result["http_status"] == (405 if method == "DELETE" else 200)
        assert result["first_token_latency_ms"] == (2.5 if method == "POST" else None)
        assert result["response_body"] is None if method == "HEAD" else SECRET not in response.text
        assert "set-cookie" not in result["response_headers"]
        assert result["request_id"].startswith("REQ-")
    assert [item[0] for item in calls] == ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    assert all(item[3] == SECRET for item in calls)
    assert SECRET not in (tmp_path / "workbench.sqlite3").read_bytes().decode(errors="ignore")
    performance=c.get("/api/v1/observability/scheduler-performance?environment_id=china_uat&time_range=24h&traffic_class=business",
                      headers=READ_ORIGIN)
    assert performance.status_code == 200
    assert performance.json()["kpis"]["request_count"] == 7
    assert performance.json()["coverage"]["provider_samples"] == 7
    assert performance.json()["kpis"]["scheduler_p95_ms"] < 1000


def test_stream_endpoint_relays_deltas_and_persists_the_final_result(monkeypatch, tmp_path):
    c = client(monkeypatch, tmp_path)
    c.put("/api/v1/environments/china_uat/execution-state", json={"enabled": True})
    sid = save_and_test(c, monkeypatch)

    def stream_transport(spec, key, on_chunk):
        assert key == SECRET
        assert spec["stream"] is True
        payload = (
            b'data: {"id":"RESP-STREAM-1","model":"model-a",'
            b'"choices":[{"delta":{"reasoning_content":"hello "}}]}\n\n'
            b'data: {"id":"RESP-STREAM-1","model":"model-a",'
            b'"choices":[{"delta":{"content":"world"}}],'
            b'"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n'
            b'data: [DONE]\n\n'
        )
        on_chunk(payload[:79])
        on_chunk(payload[79:])
        return {"status": 200, "headers": {"content-type": "text/event-stream",
                "x-request-id": "REQ-STREAM-1"}, "body": payload,
                "elapsed_ms": 18, "first_token_latency_ms": 3}

    monkeypatch.setattr(application, "WORKBENCH_STREAM_TRANSPORT", stream_transport)
    payload = body("POST", "/v1/chat/completions", auth_session_id=sid,
        stream=True, model="model-a", model_selection_mode="specified",
        body={"type": "json", "value": {"model": "model-a",
          "messages": [{"role": "user", "content": "hello"}], "stream": True}})
    with c.stream("POST", "/api/v1/environments/china_uat/request-workbench/execute-stream",
                  headers=c._security_headers("POST"), json=payload) as response:
        assert response.status_code == 200
        raw_response = response.read()
        events = [json.loads(line) for line in raw_response.splitlines() if line]
    assert "".join(event["content"] for event in events if event["type"] == "delta") == "hello world", (events,dict(response.headers),raw_response)
    final = next(event["result"] for event in events if event["type"] == "result")
    assert final["request_id"].startswith("REQ-")
    assert final["response_id"] == "RESP-STREAM-1"
    assert final["first_token_latency_ms"] == 3
    assert final["total_tokens"] == 6
    assert SECRET not in response.text
    logs = c.get("/api/v1/call-logs?source_type=realtime_execution", headers=READ_ORIGIN)
    assert logs.status_code == 200
    assert any(item["request_id"] == final["request_id"] for item in logs.json()["items"])


def test_mock_preview_endpoint_is_not_workbench_transport(monkeypatch, tmp_path):
    monkeypatch.setattr(application, "SESSION_CREDENTIALS", SessionCredentialStore(30, 20))
    monkeypatch.setattr(application, "WORKBENCH_CONNECTIONS", CredentialConnectionRegistry())
    monkeypatch.setattr(application, "STORE", Store(tmp_path / "workbench.sqlite3"))
    c = client(monkeypatch, tmp_path, roles=("operations_admin",))
    monkeypatch.setattr(application, "WORKBENCH_TRANSPORT", lambda *_: pytest.fail("real transport called"))
    response = c.post("/api/v1/replay/run", json={"strategy": "latency_first"})
    assert response.status_code == 200
