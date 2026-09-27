import json

import pytest

from backend.model_catalog import UatModelCatalog


KEY = "mock-secret-key-never-persist"


def response(data, status=200, content_type="application/json"):
    body = data if isinstance(data, bytes) else json.dumps(data).encode()
    return {"status": status, "headers": {"content-type": content_type}, "body": body}


def test_multiple_models_are_cleaned_deduplicated_and_stably_sorted():
    catalog = UatModelCatalog(60)
    payload = {"data": [
        {"id": "z-model", "owned_by": "vendor-z"},
        {"id": " a-model ", "owned_by": "vendor-a"},
        {"id": "z-model", "owned_by": "vendor-z"},
        {"id": ""}, {"id": 42}, {"owned_by": "missing"},
    ]}
    result, called = catalog.fetch(KEY, lambda *_: response(payload), 5)
    assert called is True
    assert result["status"] == "ready" and result["model_count"] == 2
    assert [item["id"] for item in result["models"]] == ["a-model", "z-model"]
    assert result["models"][0] == {
        "id": "a-model", "display_name": "a-model", "owned_by": "vendor-a",
        "execution_allowed": True, "stream_capability": "pending_confirmation",
        "source_environment": "china_uat",
    }


def test_empty_model_list_is_a_factual_ready_catalog():
    result, _ = UatModelCatalog().fetch(KEY, lambda *_: response({"data": []}), 5)
    assert result["status"] == "ready" and result["models"] == [] and result["model_count"] == 0


@pytest.mark.parametrize(("transport", "code"), [
    (lambda *_: response({}, 401), "uat_authentication_failed"),
    (lambda *_: response({}, 403), "uat_model_catalog_forbidden"),
    (lambda *_: response({}, 429), "uat_model_catalog_rate_limited"),
    (lambda *_: response({}, 503), "uat_model_catalog_upstream_error"),
    (lambda *_: (_ for _ in ()).throw(TimeoutError()), "uat_model_catalog_timeout"),
    (lambda *_: response(b"<html>bad gateway</html>", 200, "text/html"), "uat_model_catalog_html_response"),
    (lambda *_: response(b"not-json", 200, "text/plain"), "uat_model_catalog_invalid_json"),
])
def test_structured_failures_do_not_expose_credentials(transport, code):
    result, called = UatModelCatalog().fetch(KEY, transport, 5)
    serialized = json.dumps(result)
    assert called is True and result["status"] == "error"
    assert result["error"]["code"] == code
    assert KEY not in serialized
    assert "Authorization" not in serialized and "Cookie" not in serialized


def test_cache_and_forced_refresh():
    calls = 0

    def transport(*_):
        nonlocal calls
        calls += 1
        return response({"data": [{"id": f"model-{calls}"}]})

    catalog = UatModelCatalog(60)
    first, first_called = catalog.fetch(KEY, transport, 5)
    cached, cached_called = catalog.fetch(KEY, transport, 5)
    refreshed, refreshed_called = catalog.fetch(KEY, transport, 5, refresh=True)
    assert first_called is True and cached_called is False and refreshed_called is True
    assert cached["models"][0]["id"] == first["models"][0]["id"] == "model-1"
    assert refreshed["models"][0]["id"] == "model-2"
    assert calls == 2


def test_catalog_unavailable_and_forged_models_are_blocked(tmp_path):
    from tests.test_uat_integration import body, settings
    from backend.uat_service import UatStore, validate

    store = UatStore(tmp_path / "db.sqlite3")
    unavailable = validate(body(), settings(), store, None)
    forged = validate(body(requested_model="forged-model"), settings(), store, {"allowed-model": True})
    unauthorized = validate(body(requested_model="restricted"), settings(), store, {"restricted": False})
    assert "uat_model_catalog_unavailable" in unavailable["blocking_reasons"]
    assert "selected_model_not_available" in forged["blocking_reasons"]
    assert "selected_model_not_authorized" in unauthorized["blocking_reasons"]


def test_fastapi_models_endpoint_uses_current_backend_key_cache_and_refresh(monkeypatch, tmp_path):
    import backend.app as application
    from tests.enterprise_test_client import authenticated_test_client

    calls = []
    monkeypatch.setenv("WEIMETA_UAT_API_KEY", KEY)
    monkeypatch.setattr(application.STORE, "path", tmp_path / "api.sqlite3")
    monkeypatch.setattr(application, "MODEL_CATALOG", UatModelCatalog(60))

    def transport(url, key, timeout):
        calls.append((url, key, timeout))
        return response({"data": [{"id": "model-b"}, {"id": "model-a"}]})

    monkeypatch.setattr(application, "MODELS_TRANSPORT", transport)
    client = authenticated_test_client(
        application.app, runtime=application.ENTERPRISE_HTTP,
        roles=("domestic_uat_operator",))
    first = client.get("/api/v1/uat/models")
    cached = client.get("/api/v1/uat/models")
    refreshed = client.get("/api/v1/uat/models?refresh=true")
    assert first.status_code == cached.status_code == refreshed.status_code == 200
    assert [item["id"] for item in first.json()["models"]] == ["model-a", "model-b"]
    assert len(calls) == 2 and all(call[0].endswith("/v1/models") for call in calls)
    assert KEY not in first.text and "Authorization" not in first.text
    assert KEY not in (tmp_path / "api.sqlite3").read_bytes().decode(errors="ignore")
