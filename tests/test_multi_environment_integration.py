import json
import sqlite3

import backend.app as application
from backend.credential_store import SessionCredentialStore
from backend.model_catalog import UatModelCatalog
from backend.persistent_credential_vault import PersistentCredentialVault
from backend.uat_service import UatStore
from tests.enterprise_test_client import authenticated_test_client
from tests.test_persistent_credential_vault import FakeProtector


ORIGIN = {"Origin": "http://127.0.0.1:5173", "Content-Type": "application/json"}
CONFIRMATION = {"confirmed": True, "text": "I authorize encrypted persistence of this API key for the selected environment until replacement or logout."}


def client(monkeypatch, tmp_path):
    monkeypatch.setattr(application, "SESSION_CREDENTIALS", SessionCredentialStore(30, 20))
    monkeypatch.setattr(application, "PERSISTENT_CREDENTIALS",
                        PersistentCredentialVault(tmp_path / "credentials", FakeProtector()))
    monkeypatch.setattr(application, "PERSISTENT_CREDENTIAL_ERROR", None)
    monkeypatch.setattr(application, "MODEL_CATALOG", UatModelCatalog(60))
    monkeypatch.setattr(application.STORE, "path", tmp_path / "multi.sqlite3")
    return authenticated_test_client(
        application.app, runtime=application.ENTERPRISE_HTTP,
        roles=("operations_admin",), base_url="http://127.0.0.1:8000")


def test_environment_registry_exposes_only_confirmed_values(monkeypatch, tmp_path):
    result = client(monkeypatch, tmp_path).get("/api/v1/environments").json()
    items = {item["environment_id"]: item for item in result["environments"]}
    assert items["china_uat"]["api_base_url"] == "https://api-uat.weimeta.cn"
    assert items["china_uat"]["currency"] == "CNY"
    assert items["overseas"]["console_base_url"] == "https://weimeta.ai"
    assert items["overseas"]["api_base_url"] == "https://api.weimeta.ai"
    assert items["overseas"]["models_path"] == "/v1/models"
    assert items["overseas"]["chat_completions_path"] == "/v1/chat/completions"
    assert items["overseas"]["logs_page_url"] is None
    assert items["overseas"]["currency"] is None
    assert items["overseas"]["real_execution_supported"] is False


def test_environment_credentials_are_isolated_and_never_rendered(monkeypatch, tmp_path):
    web = client(monkeypatch, tmp_path)
    china, overseas = "china-secret-not-real", "overseas-secret-not-real"
    for environment_id, key in (("china_uat", china), ("overseas", overseas)):
        response = web.post(f"/api/v1/environments/{environment_id}/credentials/session",
            headers=ORIGIN, json={"api_key": key, "confirmation": CONFIRMATION})
        assert response.status_code == 200 and key not in response.text
    assert web.get("/api/v1/environments/china_uat/credentials/status").json()["configured"] is True
    assert web.get("/api/v1/environments/overseas/credentials/status").json()["configured"] is True
    web.request("DELETE", "/api/v1/environments/overseas/credentials/session", headers=ORIGIN, json={})
    assert web.get("/api/v1/environments/china_uat/credentials/status").json()["configured"] is True
    assert web.get("/api/v1/environments/overseas/credentials/status").json()["configured"] is False
    raw = (tmp_path / "multi.sqlite3").read_bytes().decode(errors="ignore")
    assert china not in raw and overseas not in raw


def test_overseas_model_catalog_requires_manual_validation_without_transport(monkeypatch, tmp_path):
    web = client(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(application, "MODELS_TRANSPORT", lambda *args: calls.append(args))
    result = web.get("/api/v1/environments/overseas/models").json()
    status = web.get("/api/v1/environments/overseas/status").json()
    assert result["error"]["code"] == "overseas_model_validation_pending"
    assert "overseas_completion_not_yet_authorized" in status["blocking_reasons"]
    assert calls == []


def test_confirmed_validation_makes_one_get_and_keeps_completion_blocked(monkeypatch, tmp_path):
    web = client(monkeypatch, tmp_path)
    key = "overseas-secret-not-real"
    web.post("/api/v1/environments/overseas/credentials/session", headers=ORIGIN,
        json={"api_key": key, "confirmation": CONFIRMATION})
    calls = []
    def transport(url, supplied_key, timeout):
        calls.append((url, supplied_key, timeout))
        return {"status": 200, "headers": {"content-type": "application/json"},
                "body": json.dumps({"data": [{"id": "z-model"}, {"id": "a-model"},
                                              {"id": "a-model"}]}).encode(),
                "elapsed_ms": 12}
    monkeypatch.setattr(application, "MODELS_TRANSPORT", transport)
    response = web.post("/api/v1/environments/overseas/discovery/validate",
        headers=ORIGIN, json={"confirmation":{"confirmed":True,
        "text":"I confirm one read-only overseas model-catalog validation request."}})
    assert response.status_code == 200
    result = response.json()
    assert len(calls) == 1
    assert calls[0][0] == "https://api.weimeta.ai/v1/models"
    assert result["models_endpoint_status"] == "read_only_validated"
    assert result["completion_endpoint_status"] == "not_validated"
    assert result["validation"]["model_count"] == 2
    assert key not in response.text
    assert "overseas_completion_not_yet_authorized" in web.get(
        "/api/v1/environments/overseas/status").json()["blocking_reasons"]
    catalog = web.get("/api/v1/environments/overseas/models").json()
    assert [item["id"] for item in catalog["models"]] == ["a-model", "z-model"]


def test_overseas_log_page_preview_confirm_and_status_api(monkeypatch, tmp_path):
    web = client(monkeypatch, tmp_path)
    preview = web.post("/api/v1/environments/overseas/log-page/preview",
        headers=ORIGIN, json={"url":"https://weimeta.ai/operator-observed/usage-logs"})
    assert preview.status_code == 200
    evidence = preview.json()
    assert evidence["live_validation_status"] == "not_attempted"
    assert web.get("/api/v1/environments/overseas/log-page/status").json()[
        "logs_page_url"] is None
    rejected = web.post("/api/v1/environments/overseas/log-page/confirm",
        headers=ORIGIN, json={"validation_id":evidence["validation_id"],
        "value_sha256":evidence["value_sha256"],"explicit_confirmation":False})
    assert rejected.status_code == 409
    preview = web.post("/api/v1/environments/overseas/log-page/preview",
        headers=ORIGIN, json={"url":"https://weimeta.ai/operator-observed/usage-logs"}).json()
    confirmed = web.post("/api/v1/environments/overseas/log-page/confirm",
        headers=ORIGIN, json={"validation_id":preview["validation_id"],
        "value_sha256":preview["value_sha256"],"explicit_confirmation":True})
    assert confirmed.status_code == 200
    assert confirmed.json()["log_page_status"] == "operator_confirmed"
    assert "api_key" not in confirmed.text.casefold()


def test_overseas_discovery_requires_confirmation_and_key_before_read_only_transport(monkeypatch, tmp_path):
    web = client(monkeypatch, tmp_path);calls=[]
    monkeypatch.setattr(application, "MODELS_TRANSPORT", lambda *args: calls.append(args))
    draft = {"api_base_url":"https://api.example.com","allowed_api_host":"api.example.com",
      "models_path":"/v1/models","chat_completions_path":"/v1/chat/completions",
      "logs_page_url":"https://weimeta.ai/console/operator-confirmed-log","currency":"USD",
      "api_protocol":"openai_compatible"}
    assert web.post("/api/v1/environments/overseas/discovery/validate",headers=ORIGIN,json=draft).status_code == 400
    confirmed={**draft,"confirmation":{"confirmed":True,"text":"I confirm one read-only overseas model-catalog validation request."}}
    result=web.post("/api/v1/environments/overseas/discovery/validate",headers=ORIGIN,json=confirmed)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "environment_key_not_configured"
    assert calls == []


def test_model_catalog_cache_isolated_by_environment_and_key():
    catalog = UatModelCatalog(60)
    calls = []
    def transport(url, key, timeout):
        calls.append((url, key))
        model = "china-model" if "china" in key else "overseas-model"
        return {"status": 200, "headers": {"content-type": "application/json"},
                "body": json.dumps({"data": [{"id": model}]}).encode()}
    china, _ = catalog.fetch("china-key", transport, 5, environment_id="china_uat")
    overseas, _ = catalog.fetch("overseas-key", transport, 5, environment_id="overseas",
        environment_name="海外站", models_url="https://confirmed.example/v1/models")
    assert china["models"][0]["source_environment"] == "china_uat"
    assert overseas["models"][0]["source_environment"] == "overseas"
    assert catalog.model_ids("china-key", "overseas") is None
    assert catalog.model_ids("overseas-key", "china_uat") is None


def test_legacy_execution_rows_are_backfilled_without_payload_rewrite(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    payload = '{"execution_id":"OLD","request_id":"COLLISION"}'
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE uat_executions(
          execution_id TEXT PRIMARY KEY,local_request_id TEXT UNIQUE,decision_id TEXT,created_at TEXT,
          status TEXT,estimated_cost_cny REAL,payload TEXT)""")
        db.execute("INSERT INTO uat_executions VALUES(?,?,?,?,?,?,?)",
            ("OLD", "COLLISION", "D", "2026-01-01", "succeeded", 0.1, payload))
    store = UatStore(path)
    with store.connect() as db:
        row = db.execute("SELECT environment_id,payload FROM uat_executions WHERE execution_id='OLD'").fetchone()
    assert row[0] == "china_uat" and row[1] == payload
