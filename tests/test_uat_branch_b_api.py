from datetime import datetime, timedelta, timezone

from backend import app as application
from backend.app import app
from tests.enterprise_test_client import authenticated_test_client


def client():
    return authenticated_test_client(app, runtime=application.ENTERPRISE_HTTP,
                                     roles=("domestic_uat_operator",))


def test_runtime_capability_review_is_scoped_audited_and_no_network(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "branch-b.sqlite3")
    now = datetime.now(timezone.utc)
    body = {"model_id":"deepseek-v4-flash","provider":"reviewed-provider",
      "channel_id":"unified-routing","max_context_tokens":32768,
      "max_input_tokens":24576,"max_output_tokens":8192,
      "streaming_supported":False,"multimodal_capabilities":[],
      "evidence_source":"reviewed-versioned-metadata",
      "evidence_type":"versioned_channel_metadata","evidence_version":"v1",
      "observed_at":now.isoformat(),"expires_at":(now+timedelta(hours=1)).isoformat(),
      "confidence_status":"confirmed","supersede_existing":True}
    reviewed = client().post("/api/v1/uat/output-capabilities/review", json=body)
    assert reviewed.status_code == 200
    assert reviewed.json()["capability"]["reviewed_by"]
    catalog = client().get("/api/v1/uat/output-capabilities?channel_id=unified-routing").json()
    selected = next(item for item in catalog["models"] if item["model_id"] == body["model_id"])
    assert selected["status"] == "confirmed" and selected["max_output_tokens"] == 8192
    assert len([item for item in catalog["models"] if item["status"] == "confirmed"]) == 1
    assert catalog["audit_log"] and "Authorization" not in str(catalog)


def test_execution_control_api_requires_three_transitions(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "DB_PATH", tmp_path / "branch-b.sqlite3")
    now = datetime.now(timezone.utc)
    payload = {"environment_id":"china_uat",
      "allowed_models":["deepseek-v4-flash"],
      "allowed_channels":["unified-routing"],"max_requests":1,
      "max_total_cost":"0.10","cost_currency":"CNY",
      "max_duration_seconds":300,"max_concurrency":1,
      "max_attempts_per_request":1,
      "expires_at":(now+timedelta(minutes=4)).isoformat(),
      "explicit_confirmation":True}
    c = client()
    draft = c.post("/api/v1/uat/execution-control", json=payload)
    assert draft.status_code == 200 and draft.json()["status"] == "DRAFT"
    task_id = draft.json()["task"]["task_id"]
    assert not draft.json()["execution_ready"]
    approved = c.post(f"/api/v1/uat/execution-control/{task_id}/approve", json={
      "approval_reference":"operator-reviewed",
      "approval_expires_at":(now+timedelta(minutes=3)).isoformat()})
    assert approved.json()["status"] == "APPROVED" and not approved.json()["execution_ready"]
    active = c.post(f"/api/v1/uat/execution-control/{task_id}/activate")
    assert active.json()["status"] == "ACTIVE" and active.json()["execution_ready"]
    killed = c.post("/api/v1/uat/execution-control/stop", json={
      "reason":"test_kill","kill_switch":True})
    assert killed.json()["status"] == "KILLED" and not killed.json()["execution_ready"]
