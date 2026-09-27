from __future__ import annotations

from dataclasses import replace

import backend.app as application
from backend.live_acceptance_service import LiveAcceptanceService
from backend.uat_service import UatSettings
from tests.enterprise_test_client import authenticated_test_client


def client():
    return authenticated_test_client(
        application.app, runtime=application.ENTERPRISE_HTTP,
        roles=("operations_admin",), base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 54000))


class FakeProbeExecutor:
    def __init__(self, database_path, scope=None, **_kwargs):
        self.service = LiveAcceptanceService(database_path, scope)
        self._credential_provider = lambda: ""

    def start(self, run_id, _configuration, **_kwargs):
        self.service.update_probe(run_id, status="RUNNING", desired_status="RUNNING")
        return {"probe_run_id": run_id, "status": "RUNNING"}

    def pause(self, run_id, **_kwargs):
        self.service.update_probe(run_id, status="PAUSED", desired_status="PAUSED")
        return {"probe_run_id": run_id, "status": "PAUSED"}

    def resume(self, run_id, **_kwargs):
        self.service.update_probe(run_id, status="RUNNING", desired_status="RUNNING")
        return {"probe_run_id": run_id, "status": "RUNNING"}

    def stop(self, run_id, **_kwargs):
        self.service.update_probe(run_id, status="STOPPED", desired_status="STOPPED")
        return {"probe_run_id": run_id, "status": "STOPPED"}

    def shutdown(self, _timeout=10):
        return {"stopped": True, "active_probe_run_ids": []}


def test_probe_api_creates_server_id_lists_and_routes_by_id(tmp_path, monkeypatch):
    database = tmp_path / "probe-api.sqlite3"
    monkeypatch.setattr(application, "STORE", application.Store(database))
    application.CONTINUOUS_PROBE_EXECUTORS.clear()
    settings = replace(UatSettings.load("china_uat"), enabled=True, api_key="test-only-key")
    monkeypatch.setattr(application, "_resolved", lambda *_args: (
        settings, "test_vault", None, {"key_fingerprint": "test"}))
    monkeypatch.setattr(application.MODEL_CATALOG, "fetch", lambda *_args, **_kwargs: ({
        "status": "ready", "models": [{"id": "real-model-a"}], "model_count": 1,
    }, False))
    monkeypatch.setattr(application, "ContinuousProbeExecutionService", FakeProbeExecutor)

    web = client()
    created = web.post("/api/v1/probes", json={
        "name": "一分钟真实探测", "environment_id": "china_uat",
        "base_url": "https://api-uat.weimeta.cn", "endpoint": "/v1/chat/completions",
        "method": "POST", "models": ["real-model-a"], "stream": False,
        "prompt": "请只回答：OK", "duration_seconds": 60,
        "interval_seconds": 5, "max_concurrency": 1, "timeout_seconds": 30,
        "max_tokens": 16, "rotation_mode": "round_robin",
        "stop_thresholds": {"consecutive_failures": 5},
    })
    assert created.status_code == 200, created.text
    run_id = created.json()["probe_run_id"]
    assert run_id.startswith("PRB-")
    assert created.json()["status"] == "RUNNING"

    listing = web.get("/api/v1/probes?q=一分钟")
    assert listing.status_code == 200
    assert [row["probe_run_id"] for row in listing.json()["items"]] == [run_id]
    detail = web.get(f"/api/v1/probes/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["task_name"] == "一分钟真实探测"
    paused = web.post(f"/api/v1/probes/{run_id}/pause", json={})
    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"
    resumed = web.post(f"/api/v1/probes/{run_id}/resume", json={})
    assert resumed.status_code == 200
    stopped = web.post(f"/api/v1/probes/{run_id}/stop", json={})
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "STOPPED"
    missing = web.get("/api/v1/probes/PRB-NOT-FOUND")
    assert missing.status_code == 404
    assert missing.json()["detail"]["message"] == "未找到该探测任务。"


def test_probe_api_rejects_non_uat_target_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "STORE", application.Store(tmp_path / "probe-denied.sqlite3"))
    response = client().post("/api/v1/probes", json={
        "name": "越界探测", "environment_id": "china_uat",
        "base_url": "https://example.com", "endpoint": "/v1/chat/completions",
        "method": "POST", "models": ["real-model-a"], "stream": False,
        "prompt": "OK", "duration_seconds": 60, "interval_seconds": 5,
        "max_concurrency": 1, "timeout_seconds": 30, "max_tokens": 16,
    })
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "probe_environment_not_allowed"
