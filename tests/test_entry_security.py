import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from backend.entry_security import (
    EnterpriseIngressMiddleware, EntrySecurityError, build_ingress_profile,
    load_ingress_profile, readiness,
)


ROOT = Path(__file__).resolve().parents[1]


def raw_profile(**updates):
    value = {
        "bind_host": "127.0.0.1",
        "tls_mode": "disabled",
        "allowed_hosts": ["testserver"],
        "allowed_origins": ["http://localhost:5173"],
        "trusted_proxy_cidrs": [],
        "reject_untrusted_forwarding": True,
        "limits": {
            "maximum_request_body_bytes": 1024,
            "maximum_header_count": 32,
            "maximum_header_bytes": 8192,
            "request_timeout_seconds": 2,
            "global_concurrency": 8,
            "scoped_concurrency": 4,
            "global_requests_per_window": 100,
            "scoped_requests_per_window": 20,
            "sensitive_requests_per_window": 2,
            "rate_window_seconds": 60,
        },
    }
    value.update(updates)
    return value


def client_for(profile, *, sensitive=frozenset()):
    app = FastAPI()

    @app.get("/health")
    def health(request: Request):
        return {"ok": True, "entry": request.state.enterprise_ingress}

    @app.get("/page")
    def page():
        return {"ok": True}

    @app.get("/api/read")
    def api_read():
        return {"ok": True}

    @app.post("/echo")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    @app.post("/api/echo")
    async def api_echo(request: Request):
        return {"size": len(await request.body())}

    @app.get("/slow")
    async def slow():
        await asyncio.sleep(.1)
        return {"ok": True}

    secured = EnterpriseIngressMiddleware(
        app, profile, sensitive_actions=sensitive)
    return TestClient(secured, client=("127.0.0.1", 50123)), secured


def test_default_development_profile_preserves_loopback_operation():
    profile = load_ingress_profile(
        ROOT / "config" / "enterprise_ingress_policy_v1.json", environ={})
    assert profile.name == "development"
    assert profile.bind_host == "127.0.0.1"
    assert profile.tls_mode == "disabled"
    assert "127.0.0.1:8000" in profile.allowed_hosts
    assert readiness(profile)["production_certificate"] == "pending_external_configuration"


def test_policy_and_schema_identifiers_are_version_bound():
    policy = json.loads((ROOT / "config" / "enterprise_ingress_policy_v1.json").read_text(
        encoding="utf-8"))
    schema = json.loads((ROOT / "schemas" / "enterprise_ingress_policy_v1.schema.json").read_text(
        encoding="utf-8"))
    assert policy["schema_version"] == "enterprise_ingress_policy_v1"
    assert schema["properties"]["schema_version"]["const"] == policy["schema_version"]
    assert set(policy["profiles"]) == {"development", "lan", "production"}
    assert set(policy["external_configuration_status"].values()) == {
        "pending_external_configuration"}


def test_external_profiles_fail_closed_until_explicitly_configured():
    policy = ROOT / "config" / "enterprise_ingress_policy_v1.json"
    with pytest.raises(EntrySecurityError, match="external_host_and_origin_allowlists_required"):
        load_ingress_profile(policy, environ={
            "ROUTING_CONSOLE_DEPLOYMENT_PROFILE": "production"})
    configured = load_ingress_profile(policy, environ={
        "ROUTING_CONSOLE_DEPLOYMENT_PROFILE": "production",
        "ROUTING_CONSOLE_ALLOWED_HOSTS": "console.example.test",
        "ROUTING_CONSOLE_ALLOWED_ORIGINS": "https://console.example.test",
        "ROUTING_CONSOLE_TRUSTED_PROXY_CIDRS": "10.20.0.0/16",
        "ROUTING_CONSOLE_TLS_MODE": "trusted_proxy",
        "ROUTING_CONSOLE_BIND_HOST": "0.0.0.0",
    })
    assert configured.name == "production"
    assert configured.external_configuration_status == "pending_external_configuration"


@pytest.mark.parametrize("origin,code", [
    ("https://*.example.test", "invalid_allowed_origin_configuration"),
    ("http://console.example.test", "production_origin_requires_https"),
])
def test_production_forbids_wildcard_and_plain_http_origins(origin, code):
    with pytest.raises(EntrySecurityError, match=code):
        build_ingress_profile("production", raw_profile(
            bind_host="0.0.0.0", tls_mode="direct",
            allowed_hosts=["console.example.test"], allowed_origins=[origin]))


def test_middleware_enforces_host_origin_and_adds_security_headers():
    profile = build_ingress_profile("development", raw_profile())
    client, _middleware = client_for(profile)
    accepted = client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert accepted.status_code == 200
    assert accepted.headers["x-content-type-options"] == "nosniff"
    assert accepted.headers["x-frame-options"] == "DENY"
    assert accepted.headers["content-security-policy"].startswith("default-src")
    assert accepted.json()["entry"]["forwarded"] is False
    assert client.get("/health", headers={"Host": "evil.test"}).status_code == 400
    rejected = client.get("/health", headers={"Origin": "http://localhost.evil.test"})
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "ORIGIN_REJECTED"


def test_untrusted_forwarding_cannot_forge_https_or_host():
    profile = build_ingress_profile("development", raw_profile())
    client, _middleware = client_for(profile)
    result = client.get("/health", headers={
        "X-Forwarded-Proto": "https", "X-Forwarded-Host": "testserver"})
    assert result.status_code == 400
    assert result.json()["detail"]["code"] == "untrusted_forwarding_headers"


def test_trusted_proxy_can_supply_tls_only_from_reviewed_cidr():
    profile = build_ingress_profile("production", raw_profile(
        bind_host="0.0.0.0", tls_mode="trusted_proxy",
        allowed_hosts=["console.example.test"],
        allowed_origins=["https://console.example.test"],
        trusted_proxy_cidrs=["10.0.0.0/8"]))

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = EnterpriseIngressMiddleware(app, profile)
    sent = []
    scope = {
        "type": "http", "method": "GET", "path": "/health",
        "scheme": "http", "client": ("10.1.2.3", 1234),
        "headers": [
            (b"host", b"internal:8000"),
            (b"forwarded", b"for=203.0.113.4;proto=https;host=console.example.test"),
            (b"origin", b"https://console.example.test"),
        ],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    assert sent[0]["status"] == 200
    assert (b"strict-transport-security", b"max-age=31536000; includeSubDomains") in sent[0]["headers"]


def test_request_body_and_timeout_limits_fail_closed():
    body_profile = build_ingress_profile("development", raw_profile(
        limits={**raw_profile()["limits"], "maximum_request_body_bytes": 3}))
    body_client, _ = client_for(body_profile)
    response = body_client.post("/echo", content=b"four")
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"

    timeout_profile = build_ingress_profile("development", raw_profile(
        limits={**raw_profile()["limits"], "request_timeout_seconds": .01}))
    timeout_client, _ = client_for(timeout_profile)
    response = timeout_client.get("/slow")
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "request_timeout"


def test_header_count_and_size_limits_fail_before_application():
    count_profile = build_ingress_profile("development", raw_profile(
        limits={**raw_profile()["limits"], "maximum_header_count": 2}))
    count_client, _ = client_for(count_profile)
    response = count_client.get("/health")
    assert response.status_code == 431
    assert response.json()["detail"]["code"] == "request_header_count_exceeded"

    size_profile = build_ingress_profile("development", raw_profile(
        limits={**raw_profile()["limits"], "maximum_header_bytes": 16}))
    size_client, _ = client_for(size_profile)
    response = size_client.get("/health")
    assert response.status_code == 431
    assert response.json()["detail"]["code"] == "request_header_size_exceeded"


def test_scoped_sensitive_rate_limit_is_enforced():
    profile = build_ingress_profile("development", raw_profile(
        limits={**raw_profile()["limits"], "sensitive_requests_per_window": 1}))
    client, _middleware = client_for(profile, sensitive=frozenset({"POST:/api/echo"}))
    assert client.post("/api/echo", content=b"1").status_code == 200
    denied = client.post("/api/echo", content=b"1")
    assert denied.status_code == 429
    assert denied.headers["retry-after"] == "60"


def test_spa_navigation_does_not_consume_protected_api_scope_quota():
    profile = build_ingress_profile("development", raw_profile(
        limits={**raw_profile()["limits"], "scoped_requests_per_window": 1,
                "sensitive_requests_per_window": 1}))
    client, _middleware = client_for(profile)
    assert client.get("/page").status_code == 200
    assert client.get("/page").status_code == 200
    assert client.get("/api/read").status_code == 200
    assert client.get("/api/read").status_code == 429


def test_incomplete_authenticated_rate_scope_is_rejected():
    profile = build_ingress_profile("development", raw_profile())

    async def app(scope, receive, send):
        raise AssertionError("application must not run")

    middleware = EnterpriseIngressMiddleware(app, profile)
    sent = []
    scope = {
        "type": "http", "method": "GET", "path": "/protected",
        "scheme": "http", "client": ("127.0.0.1", 1234),
        "headers": [(b"host", b"testserver")],
        "state": {"principal_context": {
            "principal_type": "human", "principal_id": "USER-1",
            "tenant_id": "", "workspace_id": "WS-1"}},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    assert sent[0]["status"] == 403
