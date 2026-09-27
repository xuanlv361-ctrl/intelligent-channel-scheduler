from __future__ import annotations

import base64
import json
from pathlib import Path
import sqlite3

from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend.entry_security import EnterpriseIngressMiddleware, build_ingress_profile
from backend.security.http import (
    CSRF_HEADER,
    EnterpriseHTTPAuthorizationMiddleware,
    EnterpriseHTTPRuntime,
    PrincipalPreResolutionMiddleware,
    annotate_route_permissions,
    clear_session_cookie,
    permission_for_scope,
    request_principal,
    set_session_cookie,
)


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:5174"
MASTER = base64.b64encode(b"enterprise-api-security-test-key-material-0000000001").decode()


def _profile():
    return build_ingress_profile("development", {
        "bind_host": "127.0.0.1", "tls_mode": "disabled",
        "allowed_hosts": ["127.0.0.1:8000"],
        "allowed_origins": [ORIGIN], "trusted_proxy_cidrs": [],
        "reject_untrusted_forwarding": True,
        "limits": {
            "maximum_request_body_bytes": 100_000, "maximum_header_count": 32,
            "maximum_header_bytes": 10_000, "request_timeout_seconds": 5,
            "global_concurrency": 8, "scoped_concurrency": 4,
            "global_requests_per_window": 100, "scoped_requests_per_window": 50,
            "sensitive_requests_per_window": 20, "rate_window_seconds": 60,
        },
    })


def _app(tmp_path: Path) -> tuple[TestClient, EnterpriseHTTPRuntime, FastAPI]:
    profile = _profile()
    runtime = EnterpriseHTTPRuntime.build(
        profile=profile, database_path=tmp_path / "security.sqlite3", root=ROOT,
        environ={"ROUTING_CONSOLE_ENTERPRISE_SIGNING_KEY": MASTER})
    app = FastAPI()

    @app.get("/api/v1/system/status")
    def status(): return {"status": "ok"}

    @app.get("/api/v1/system/readiness")
    def ready(): return {"status": "ready"}

    @app.post("/api/v1/security/session/bootstrap")
    def bootstrap(request: Request, response: Response):
        token, validation, csrf = runtime.bootstrap_development(request)
        set_session_cookie(response, token, profile)
        return {"csrf_token": csrf, "principal": {
            "principal_id": validation.principal.principal_id,
            "tenant_id": validation.principal.tenant_id,
            "workspace_id": validation.principal.workspace_id,
            "roles": sorted(validation.principal.roles)}}

    @app.get("/api/v1/security/session/csrf")
    def csrf(request: Request):
        return {"csrf_token": runtime.csrf.issue(request.state.enterprise_session)}

    @app.post("/api/v1/security/session/logout")
    def logout(request: Request, response: Response):
        runtime.sessions.revoke_session(
            request.state.enterprise_session.session_id, "test_logout")
        clear_session_cookie(response, profile)
        return {"status": "revoked"}

    @app.get("/api/v1/evidence")
    def evidence(request: Request):
        principal = request_principal(request)
        return {"tenant_id": principal.tenant_id, "roles": sorted(principal.roles)}

    @app.post("/api/v1/circuit-breakers/test")
    def circuit_write(request: Request):
        return {"tenant_id": request_principal(request).tenant_id}

    @app.post("/api/v1/imports/confirm")
    def import_write(): return {"unsafe": True}

    annotate_route_permissions(app)
    app.add_middleware(
        EnterpriseHTTPAuthorizationMiddleware, runtime=runtime, route_app=app)
    app.add_middleware(
        EnterpriseIngressMiddleware, profile=profile,
        action_resolver=permission_for_scope,
        sensitive_actions=frozenset({"evidence.import", "circuit.manage"}))
    app.add_middleware(PrincipalPreResolutionMiddleware, runtime=runtime)
    return TestClient(app, base_url="http://127.0.0.1:8000",
                      client=("127.0.0.1", 50123)), runtime, app


def _bootstrap(client: TestClient) -> str:
    response = client.post("/api/v1/security/session/bootstrap",
                           headers={"Origin": ORIGIN}, json={})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_route_metadata_is_default_deny_and_public_exemptions_are_explicit(tmp_path):
    _client, _runtime, app = _app(tmp_path)
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1/"):
            continue
        assert (getattr(route.endpoint, "__enterprise_permission__", None) or
                getattr(route.endpoint, "__enterprise_public__", False)), route.path


def test_unauthenticated_and_forged_identity_headers_fail_closed(tmp_path):
    client, _runtime, _ = _app(tmp_path)
    assert client.get("/api/v1/system/status").status_code == 200
    unauthenticated = client.get("/api/v1/evidence", headers={"Origin": ORIGIN})
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["access-control-allow-origin"] == ORIGIN
    assert unauthenticated.headers["access-control-allow-credentials"] == "true"
    hostile = client.get(
        "/api/v1/evidence", headers={"Origin": "https://evil.example"})
    assert hostile.status_code == 403
    assert "access-control-allow-origin" not in hostile.headers
    forged = client.get("/api/v1/evidence", headers={
        "X-Principal-ID": "admin", "X-Roles": "security_engineering_owner",
        "X-Permissions": "tenant.manage", "X-Tenant-ID": "tenant-other",
        "X-Workspace-ID": "workspace-other",
    })
    assert forged.status_code == 401
    assert "tenant-other" not in forged.text


def test_loopback_bootstrap_uses_server_identity_and_explicit_full_dev_roles(tmp_path):
    client, runtime, _ = _app(tmp_path)
    csrf = _bootstrap(client)
    result = client.get("/api/v1/evidence", headers={
        "X-Tenant-ID": "tenant-forged", "X-Roles": "operations_admin"})
    assert result.status_code == 200
    assert result.json()["tenant_id"] == runtime.identity_config.default_development_tenant_id
    allowed = client.post("/api/v1/imports/confirm", headers={
        "Origin": ORIGIN, CSRF_HEADER: csrf, "X-Permissions": "evidence.import"},
        json={})
    assert allowed.status_code == 200
    assert allowed.json() == {"unsafe": True}


def test_csrf_missing_cross_session_origin_and_replay_are_rejected(tmp_path):
    first, runtime, _ = _app(tmp_path)
    second = TestClient(first.app, base_url="http://127.0.0.1:8000",
                        client=("127.0.0.1", 50124))
    first_token, second_token = _bootstrap(first), _bootstrap(second)
    assert first.post("/api/v1/circuit-breakers/test",
                      headers={"Origin": ORIGIN}, json={}).status_code == 403
    crossed = first.post("/api/v1/circuit-breakers/test", headers={
        "Origin": ORIGIN, CSRF_HEADER: second_token}, json={})
    assert crossed.status_code == 403
    assert crossed.json()["detail"]["code"] == "csrf_session_scope_mismatch"
    accepted = first.post("/api/v1/circuit-breakers/test", headers={
        "Origin": ORIGIN, CSRF_HEADER: first_token}, json={})
    assert accepted.status_code == 200
    with sqlite3.connect(runtime.database_path) as db:
        row = db.execute("""SELECT tenant_id,workspace_id,details_json
          FROM enterprise_session_audit WHERE event_type='high_risk_http_action'
          ORDER BY created_at DESC LIMIT 1""").fetchone()
    details = json.loads(row[2])
    assert row[:2] == (runtime.identity_config.default_development_tenant_id,
                       runtime.identity_config.default_development_workspace_id)
    assert details["action"] == "circuit.manage"
    assert details["result"] == "succeeded"
    assert details["reason"] == "http_status_200"
    replay = first.post("/api/v1/circuit-breakers/test", headers={
        "Origin": ORIGIN, CSRF_HEADER: first_token}, json={})
    assert replay.status_code == 403
    assert replay.json()["detail"]["code"] == "csrf_token_replay_detected"
    rejected_origin = second.post("/api/v1/circuit-breakers/test", headers={
        "Origin": "https://evil.example", CSRF_HEADER: second_token}, json={})
    assert rejected_origin.status_code == 403
    assert "evil.example" not in rejected_origin.text


def test_revoked_session_cookie_cannot_be_replayed(tmp_path):
    client, _runtime, _ = _app(tmp_path)
    csrf = _bootstrap(client)
    old_cookie = client.cookies.get("rqc_enterprise_session")
    logout = client.post("/api/v1/security/session/logout", headers={
        "Origin": ORIGIN, CSRF_HEADER: csrf}, json={})
    assert logout.status_code == 200
    replay = TestClient(client.app, base_url="http://127.0.0.1:8000",
                        client=("127.0.0.1", 50125))
    replay.cookies.set("rqc_enterprise_session", old_cookie, path="/api/v1")
    result = replay.get("/api/v1/evidence")
    assert result.status_code == 401
    assert result.json()["detail"]["code"] == "session_revoked"


def test_production_without_external_key_material_is_pending_and_fails_closed(tmp_path):
    raw = {
        "bind_host": "0.0.0.0", "tls_mode": "trusted_proxy",
        "allowed_hosts": ["console.example.test"],
        "allowed_origins": ["https://console.example.test"],
        "trusted_proxy_cidrs": ["10.0.0.0/24"],
        "reject_untrusted_forwarding": True,
        "limits": {
            "maximum_request_body_bytes": 100_000, "maximum_header_count": 32,
            "maximum_header_bytes": 10_000, "request_timeout_seconds": 5,
            "global_concurrency": 8, "scoped_concurrency": 4,
            "global_requests_per_window": 100, "scoped_requests_per_window": 50,
            "sensitive_requests_per_window": 20, "rate_window_seconds": 60,
        },
    }
    runtime = EnterpriseHTTPRuntime.build(
        profile=build_ingress_profile("production", raw),
        database_path=tmp_path / "production.sqlite3", root=ROOT, environ={})
    assert runtime.ready is False
    assert runtime.unavailable_reason == "external_key_material_pending_external_configuration"
