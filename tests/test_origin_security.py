import json

from fastapi.testclient import TestClient
import pytest

import backend.app as application
from backend.origin_security import (
    DEFAULT_DEVELOPMENT_ORIGINS, DEFAULT_LOCAL_API_HOSTS,
    authorize_read_only_origin, load_frontend_origins,
    load_local_api_hosts, normalize_host, normalize_origin,
)

BODY = {
    "environment_id": "all",
    "date_from": "2026-07-29T00:00:00Z",
    "date_to": "2026-07-29T01:00:00Z",
    "timezone": "UTC",
    "sync_interval_seconds": 7,
    "maximum_records": 10,
}


def client():
    return TestClient(
        application.app, base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50123))


def test_exact_5174_and_5173_development_origins_are_shared_with_cors():
    assert application.FRONTEND_ORIGINS == frozenset(DEFAULT_DEVELOPMENT_ORIGINS)
    for origin in DEFAULT_DEVELOPMENT_ORIGINS:
        response = client().post(
            "/api/v1/log-sync/jobs",
            headers={"Origin": origin, "Content-Type": "application/json"},
            json=BODY,
        )
        assert response.status_code == 401
        assert response.json()["detail"]["code"]=="authentication_required"
        preflight = client().options(
            "/api/v1/log-sync/jobs",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert preflight.headers["access-control-allow-origin"] == origin


def test_unknown_malicious_missing_null_and_lookalike_origins_are_rejected():
    rejected = (
        "http://127.0.0.1:5175",
        "http://localhost:5174.evil.test",
        "https://127.0.0.1:5174",
        "file://local",
        "null",
        None,
    )
    for origin in rejected:
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        result = client().post("/api/v1/log-sync/jobs",headers=headers,json=BODY)
        assert result.status_code == (401 if origin is None else 403)
        body = result.json()["detail"]
        assert body["code"] == ("authentication_required" if origin is None else "ORIGIN_REJECTED")
        assert "api_key" not in result.text.casefold()


def test_origin_and_referer_normalization_are_strict():
    assert normalize_origin("HTTP://LOCALHOST:5174/") == "http://localhost:5174"
    assert normalize_origin("http://localhost:5174/path") is None
    assert normalize_origin(
        "http://localhost:5174/collector?tab=sync", referer=True
    ) == "http://localhost:5174"
    assert normalize_origin("https://localhost:5174.evil.test/") is None
    assert normalize_origin("null") is None
    assert normalize_origin("file:///tmp/index.html", referer=True) is None


def test_referer_never_substitutes_for_missing_origin():
    result = client().post(
        "/api/v1/log-sync/jobs",
        headers={
            "Referer": "http://127.0.0.1:5174/collector",
            "Content-Type": "application/json",
        },
        json=BODY,
    )
    assert result.status_code == 401
    detail = result.json()["detail"]
    assert detail["code"]=="authentication_required"


def test_configured_origins_are_normalized_without_wildcards():
    configured = load_frontend_origins(
        "HTTP://LOCALHOST:5174/,http://127.0.0.1:5174")
    assert configured == {
        "http://localhost:5174", "http://127.0.0.1:5174"}


def test_read_only_origin_policy_accepts_only_exact_loopback_contract():
    allowed_origins = frozenset(DEFAULT_DEVELOPMENT_ORIGINS)
    allowed_hosts = frozenset(DEFAULT_LOCAL_API_HOSTS)
    assert authorize_read_only_origin(
        "http://127.0.0.1:5174", "127.0.0.1:5174",
        peer_host="127.0.0.1",
        allowed_origins=allowed_origins,
        allowed_local_hosts=allowed_hosts,
    ) == "approved_origin_read_only"
    assert authorize_read_only_origin(
        None, "127.0.0.1:8000", peer_host="127.0.0.1",
        referer="http://127.0.0.1:5174/collector",
        sec_fetch_site="same-origin", allowed_origins=allowed_origins,
        allowed_local_hosts=allowed_hosts,
    ) == "origin_absent_read_only"
    assert authorize_read_only_origin(
        None, "127.0.0.1:8000", peer_host="127.0.0.1",
        allowed_origins=allowed_origins,
        allowed_local_hosts=allowed_hosts,
    ) == "origin_absent_read_only"
    assert authorize_read_only_origin(
        "http://localhost:5173", "localhost:8000", peer_host="::1",
        allowed_origins=allowed_origins,
        allowed_local_hosts=allowed_hosts,
    ) == "approved_origin_read_only"


@pytest.mark.parametrize("origin,host,peer,referer,fetch_site", [
    (None, "127.0.0.1:8000", "203.0.113.7",
     "http://127.0.0.1:5174/collector", "same-origin"),
    (None, "evil.test:8000", "127.0.0.1",
     "http://127.0.0.1:5174/collector", "same-origin"),
    (None, "127.0.0.1:8000", "127.0.0.1",
     "http://localhost:5174.evil.test/collector", "same-origin"),
    (None, "127.0.0.1:8000", "127.0.0.1",
     "http://127.0.0.1:5174/collector", "cross-site"),
    ("", "127.0.0.1:8000", "127.0.0.1",
     "http://127.0.0.1:5174/collector", "same-origin"),
    ("http://localhost:5174.evil.test", "127.0.0.1:8000", "127.0.0.1",
     "http://127.0.0.1:5174/collector", "same-origin"),
    ("http://user:pass@localhost:5174", "127.0.0.1:8000", "127.0.0.1",
     "http://127.0.0.1:5174/collector", "same-origin"),
    ("http://localhost:5174?confuse=1", "127.0.0.1:8000", "127.0.0.1",
     "http://127.0.0.1:5174/collector", "same-origin"),
])
def test_read_only_origin_policy_rejects_spoofing(
        origin, host, peer, referer, fetch_site):
    assert authorize_read_only_origin(
        origin, host, peer_host=peer, referer=referer,
        sec_fetch_site=fetch_site,
        allowed_origins=frozenset(DEFAULT_DEVELOPMENT_ORIGINS),
        allowed_local_hosts=frozenset(DEFAULT_LOCAL_API_HOSTS),
    ) is None


def test_local_api_host_configuration_is_exact_and_configurable():
    assert "127.0.0.1:5174" in DEFAULT_LOCAL_API_HOSTS
    assert "localhost:5174" in DEFAULT_LOCAL_API_HOSTS
    assert normalize_host("LOCALHOST:8000") == "localhost:8000"
    assert normalize_host("localhost") is None
    assert normalize_host("localhost:8000.evil.test") is None
    assert load_local_api_hosts(
        "localhost:9000,127.0.0.1:9000") == {
            "localhost:9000", "127.0.0.1:9000"}


def test_read_only_session_validation_route_allows_loopback_without_origin(
        monkeypatch):
    events = []
    session_id = "PSESSION-ROUTE-SYNTHETIC"
    monkeypatch.setattr(
        application.PERSISTENT_SESSIONS, "inspect_session",
        lambda selected, environment, **_kwargs: {
            "persistent_session_id": selected,
            "status": "metadata_valid",
            "environment_id": environment,
            "schema_version": 1,
            "approved_origins": ["https://uat.weimeta.cn"],
            "created_at": "2026-07-29T00:00:00Z",
            "expires_at": "2026-07-29T01:00:00Z",
            "storage_types": ["cookies"],
        })
    monkeypatch.setattr(
        application.PERSISTENT_SESSIONS, "_event",
        lambda event_type, details, job_id=None, **_kwargs:
        events.append((event_type, details, job_id)))
    local_client = TestClient(
        application.app,base_url="http://127.0.0.1:8000",
        client=("127.0.0.1",50123))
    response = local_client.get(
        f"/api/v1/log-sync/persistent/sessions/{session_id}/validate",
        params={"environment_id": "china_uat"})
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert events==[]
    rendered = response.text + json.dumps(events)
    assert session_id not in json.dumps(events)
    assert not any(secret in rendered.casefold() for secret in (
        "vault_reference_id", "ciphertext_sha256", "authorization",
        "cookie_value", "api_key"))


@pytest.mark.parametrize("origin", [
    "http://localhost:5174.evil.test",
    "http://user:pass@localhost:5174",
    "http://localhost:5174?confuse=1",
])
def test_read_only_session_validation_route_rejects_hostile_origin(
        origin, monkeypatch):
    called = []
    monkeypatch.setattr(
        application.PERSISTENT_SESSIONS, "inspect_session",
        lambda *_args: called.append(True))
    local_client = TestClient(
        application.app, base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50123))
    response = local_client.get(
        "/api/v1/log-sync/persistent/sessions/PSESSION-SYNTHETIC/validate",
        params={"environment_id": "china_uat"},
        headers={"Origin": origin})
    assert response.status_code == 403
    assert not called


def test_read_only_session_validation_route_rejects_non_loopback_peer(
        monkeypatch):
    called = []
    monkeypatch.setattr(
        application.PERSISTENT_SESSIONS, "inspect_session",
        lambda *_args: called.append(True))
    remote_client = TestClient(
        application.app,base_url="http://127.0.0.1:8000",
        client=("203.0.113.9",50123))
    response = remote_client.get(
        "/api/v1/log-sync/persistent/sessions/PSESSION-SYNTHETIC/validate",
        params={"environment_id": "china_uat"})
    assert response.status_code == 401
    assert not called
