from __future__ import annotations

from pathlib import Path

import backend.app as application
from tests.enterprise_test_client import authenticated_test_client


CSV_PATH = Path(r"E:\lx\调用日志_20260730_172539.csv")


def client():
    return authenticated_test_client(
        application.app, runtime=application.ENTERPRISE_HTTP,
        roles=("qa_auditor",), base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 54200),
    )


def test_call_log_list_analytics_and_import_status_are_tenant_scoped(tmp_path, monkeypatch):
    database = tmp_path / "call-log-api.sqlite3"
    monkeypatch.setattr(application, "STORE", application.Store(database))
    monkeypatch.setattr(application, "CALL_LOGS", None)
    service = application.current_call_logs()
    service.initialize_historical_csv(CSV_PATH)

    web = client()
    listed = web.get("/api/v1/call-logs?environment_id=china_uat&limit=10")
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 646
    assert listed.json()["historical_count"] == 646
    assert len(listed.json()["items"]) == 10
    assert all(item["request_id"] is None for item in listed.json()["items"])
    assert all(item["channel_id"] is None for item in listed.json()["items"])

    analytics = web.get("/api/v1/call-logs/analytics?environment_id=china_uat")
    assert analytics.status_code == 200, analytics.text
    assert analytics.json()["request_count"] == 646
    assert analytics.json()["total_cost"] == "13.256422"

    status = web.get("/api/v1/call-logs/import-status")
    assert status.status_code == 200, status.text
    assert status.json()["report"]["file_sha256"].startswith("BACBC4F5")
    serialized = listed.text.lower() + status.text.lower()
    assert "authorization" not in serialized
    assert "source_ip_digest" not in serialized
    assert "api_key_alias" not in serialized

