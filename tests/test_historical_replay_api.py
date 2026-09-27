from __future__ import annotations

import backend.app as application
from tests.enterprise_test_client import authenticated_test_client


def client():
    return authenticated_test_client(
        application.app, runtime=application.ENTERPRISE_HTTP,
        roles=("operations_admin",), base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 54211))


def test_historical_replay_api_uses_real_ledger_and_exports(tmp_path, monkeypatch):
    database = tmp_path / "historical-replay-api.sqlite3"
    monkeypatch.setattr(application, "STORE", application.Store(database))
    monkeypatch.setattr(application, "CALL_LOGS", None)
    logs = application.current_call_logs()
    for index in range(3):
        record = logs.start_execution(
            request_id=f"REQ-API-{index}", decision_id=f"DEC-API-{index}",
            environment_id="china_uat", requested_model="model-a", stream=False,
            channel_id="channel-a", configuration_version="policy-v1")
        logs.finish_execution(record, status="SUCCESS", response_id=f"RESP-API-{index}",
            actual_model="model-a", http_status=200, total_latency_ms=100 + index,
            input_tokens=10, output_tokens=20, channel_id="channel-a")

    web = client()
    source = web.get("/api/v1/historical-replay/source?environment_id=china_uat")
    assert source.status_code == 200, source.text
    assert source.json()["sample_count"] == 3
    assert source.json()["is_mock"] is False

    run = web.post("/api/v1/historical-replay/runs", json={
        "environment_id": "china_uat", "baseline_strategy": "actual_observed",
        "candidate_strategy": "latency_first", "exact_only": False, "limit": 50})
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["network_calls"] == 0
    assert payload["items"][0]["request_id"].startswith("REQ-API-")
    assert payload["cost"]["candidate"] is None

    export = web.get(f"/api/v1/historical-replay/runs/{payload['replay_id']}/export.csv")
    assert export.status_code == 200, export.text
    assert "REQ-API-" in export.text
    assert "authorization" not in export.text.lower()
