import backend.app as application
from tests.enterprise_test_client import authenticated_test_client


def client(peer="127.0.0.1"):
    return authenticated_test_client(
        application.app, runtime=application.ENTERPRISE_HTTP,
        roles=("qa_auditor",), base_url="http://127.0.0.1:8000",
        client=(peer, 50123))


def test_governance_read_apis_are_local_network_free_and_redacted():
    for path in (
        "/api/v1/circuit-breakers/status",
        "/api/v1/exploration-governance/status",
        "/api/v1/exploration-governance/audit",
        "/api/v1/capability-evidence",
        "/api/v1/capability-evidence/audit",
        "/api/v1/traffic-change-governance/status",
        "/api/v1/traffic-change-governance/audit",
        "/api/v1/probe-governance/status",
        "/api/v1/probe-governance/audit",
        "/api/v1/high-cost-test-governance/status",
        "/api/v1/high-cost-test-governance/audit",
        "/api/v1/safety-governance/timeline",
        "/api/v1/formal-agent-skills/discovery",
        "/api/v1/formal-agent-skills/status",
        "/api/v1/formal-agent-skills/audit-summary",
        "/api/v1/scheduler-attribution/chains",
    ):
        response = client().get(path)
        assert response.status_code == 200, (path, response.text)
        assert response.json()["network_called"] is False
        lowered = response.text.casefold()
        assert not any(term in lowered for term in (
            "authorization\"", "cookie_value", "api_key\"", "password\"",
            "response_body", "prompt\"", "dpapi"))
        assert not any(term in lowered for term in (
            "actor_id", "proposer_id", "approver_id", "approved_by", "details_json"))


def test_governance_read_apis_reject_hostile_origin_and_non_loopback_peer():
    for path in ("/api/v1/circuit-breakers/status","/api/v1/traffic-change-governance/status",
                 "/api/v1/probe-governance/status","/api/v1/high-cost-test-governance/status",
                 "/api/v1/safety-governance/timeline","/api/v1/formal-agent-skills/status"):
        hostile = client().get(path, headers={"Origin": "http://localhost:5174.evil.test"})
        assert hostile.status_code == 403
        assert client("203.0.113.5").get(path).status_code == 403
        assert client().post(path).status_code == 405


def test_governance_routes_are_in_openapi():
    paths = set(application.app.openapi()["paths"])
    assert {
        "/api/v1/circuit-breakers/status",
        "/api/v1/circuit-breakers/{circuit_id}/transitions",
        "/api/v1/exploration-governance/status",
        "/api/v1/exploration-governance/audit",
        "/api/v1/capability-evidence",
        "/api/v1/capability-evidence/audit",
        "/api/v1/traffic-change-governance/status",
        "/api/v1/traffic-change-governance/audit",
        "/api/v1/probe-governance/status",
        "/api/v1/probe-governance/audit",
        "/api/v1/high-cost-test-governance/status",
        "/api/v1/high-cost-test-governance/audit",
        "/api/v1/safety-governance/timeline",
        "/api/v1/formal-agent-skills/discovery",
        "/api/v1/formal-agent-skills/status",
        "/api/v1/formal-agent-skills/audit-summary",
        "/api/v1/scheduler-attribution/chains",
        "/api/v1/scheduler-attribution/chains/{decision_id}",
    } <= paths


def test_empty_timeline_does_not_generate_demo_points(tmp_path, monkeypatch):
    monkeypatch.setattr(application, "STORE", application.Store(tmp_path / "empty.sqlite3"))
    result = client().get("/api/v1/safety-governance/timeline").json()
    assert result["status"] == "empty"
    assert result["series"] == [] and result["latest_event_at"] is None
    assert result["freshness_status"] == "unknown"
    assert result["generated_demo_points"] is False
    assert result["network_called"] is False
    assert result["scheduler_overhead"]["sample_count"] == 0
    assert result["scheduler_overhead"]["network_called"] is False
    assert result["permission_boundary"].startswith("read_only_monitoring")
