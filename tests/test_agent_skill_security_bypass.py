"""End-to-end proofs that caller labels never bypass server security."""

from fastapi.testclient import TestClient

import backend.app as application


APPROVED_ORIGIN = "http://127.0.0.1:5174"
CALLER_SPOOF_HEADERS = {
    "X-Agent-Role": "admin_agent",
    "X-Skill-Id": "uat_write_override",
    "X-Internal-Caller": "trusted",
    "X-Execution-Guard-Bypass": "true",
}
SYNC_BODY = {
    "environment_id": "china_uat",
    "date_from": "2026-07-29T02:00:20Z",
    "date_to": "2026-07-29T03:01:56Z",
    "timezone": "Asia/Shanghai",
    "sync_interval_seconds": 7,
    "maximum_records": 2,
}


def _client() -> TestClient:
    return TestClient(
        application.app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50123),
    )


def test_forged_agent_and_skill_identity_cannot_replace_required_origin():
    response = _client().post(
        "/api/v1/log-sync/jobs",
        headers={"Content-Type": "application/json", **CALLER_SPOOF_HEADERS},
        json=SYNC_BODY,
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required"


def test_forged_internal_identity_cannot_allow_hostile_origin():
    response = _client().post(
        "/api/v1/log-sync/jobs",
        headers={
            "Content-Type": "application/json",
            "Origin": "http://127.0.0.1:5174.evil.test",
            **CALLER_SPOOF_HEADERS,
        },
        json=SYNC_BODY,
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ORIGIN_REJECTED"


def test_forged_skill_cannot_confirm_manipulated_uat_log_page():
    preview = _client().post(
        "/api/v1/environments/china_uat/log-page/preview",
        headers={
            "Content-Type": "application/json",
            "Origin": APPROVED_ORIGIN,
            **CALLER_SPOOF_HEADERS,
        },
        json={"url": "https://uat.weimeta.cn.evil.test/console/billing/logs"},
    )
    # A forged skill without a verified enterprise session is rejected before
    # any URL-validation handler can be reached.
    assert preview.status_code == 401
    rendered = preview.text.casefold()
    assert "authentication_required" in rendered
    assert "authorization" not in rendered
    assert "cookie" not in rendered
    assert "api_key" not in rendered


def test_agent_labels_do_not_turn_read_only_monitoring_route_into_post():
    response = _client().post(
        "/api/v1/circuit-breakers/status",
        headers={
            "Content-Type": "application/json",
            "Origin": APPROVED_ORIGIN,
            **CALLER_SPOOF_HEADERS,
        },
        json={},
    )
    assert response.status_code == 405
