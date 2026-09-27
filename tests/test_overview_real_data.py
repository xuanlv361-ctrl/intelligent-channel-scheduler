from pathlib import Path

import backend.app as application
from tests.enterprise_test_client import authenticated_test_client


CSV_PATH = Path(r"E:\lx\调用日志_20260730_172539.csv")


def _client():
    return authenticated_test_client(
        application.app,
        runtime=application.ENTERPRISE_HTTP,
        roles=("qa_auditor",),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 54220),
    )


def test_overview_defaults_to_real_historical_ledger_without_demo_channels(
    tmp_path, monkeypatch,
):
    database = tmp_path / "overview.sqlite3"
    monkeypatch.setattr(application, "STORE", application.Store(database))
    monkeypatch.setattr(application, "CALL_LOGS", None)
    application.current_call_logs().initialize_historical_csv(CSV_PATH)

    response = _client().get("/api/v1/overview")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["mode"] == "uat"
    assert payload["data_mode"] == "real"
    assert payload["is_mock"] is False
    assert payload["records"] == 646
    assert payload["metrics"]["request_count"] == 646
    assert payload["channels"] == 0
    assert payload["channel_summary"] == []
    assert payload["routing_distribution"] == {}
    assert "历史 CSV" in payload["channel_evidence_note"]
    assert all(item["channel_id"] is None for item in payload["recent_executions"])
    assert payload["last_synced_at"] is None
    assert payload["runtime"]["log_sync_status"] == "never_synchronized"
    assert payload["runtime"]["freshness_status"] == "historical_only"
    assert payload["metrics"]["total_cost"] == "13.256422"


def test_overview_time_window_and_missing_today_cost_are_explicit(
    tmp_path, monkeypatch,
):
    database = tmp_path / "overview-window.sqlite3"
    monkeypatch.setattr(application, "STORE", application.Store(database))
    monkeypatch.setattr(application, "CALL_LOGS", None)
    application.current_call_logs().initialize_historical_csv(CSV_PATH)

    response = _client().get("/api/v1/overview?time_range=24h")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["time_range"] == "24h"
    assert payload["records"] == 0
    assert payload["budget"]["today_uat_spend"] is None
    assert payload["metrics"]["measurement_coverage"] == {
        "latency": 0, "tokens": 0, "cost": 0,
    }
