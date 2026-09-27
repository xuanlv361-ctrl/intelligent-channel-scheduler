from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.formal_agent_skill_audit_service import FormalAgentSkillAuditService
from backend.formal_agent_skill_routes import build_formal_agent_skill_router
from formal_agent_skill_registry import FormalAgentSkillRegistry


APPROVED_ORIGIN = "http://127.0.0.1:5174"


def _client(tmp_path, *, status=None):
    registry = FormalAgentSkillRegistry()
    audit = FormalAgentSkillAuditService(tmp_path / "formal-skill-routes.sqlite3")

    def secure_read_only_request(request: Request) -> None:
        if request.headers.get("origin") != APPROVED_ORIGIN:
            raise HTTPException(status_code=403, detail="origin_forbidden")

    runtime_status = status or {
        "status": "ready",
        "api_key": "must-not-leak",
        "parameters": {"credential": "must-not-leak-either"},
    }
    app = FastAPI()
    app.include_router(build_formal_agent_skill_router(
        registry, audit, runtime_status, secure_read_only_request))
    return TestClient(app), audit


def _get(client: TestClient, path: str):
    return client.get(path, headers={"Origin": APPROVED_ORIGIN})


def test_hostile_origin_is_rejected_for_every_route(tmp_path):
    client, _ = _client(tmp_path)
    hostile = {"Origin": f"{APPROVED_ORIGIN}.attacker.example"}
    for suffix in ("discovery", "status", "audit-summary"):
        response = client.get(f"/api/v1/formal-agent-skills/{suffix}", headers=hostile)
        assert response.status_code == 403


def test_discovery_exposes_only_pinned_read_only_metadata(tmp_path):
    client, _ = _client(tmp_path)
    response = _get(client, "/api/v1/formal-agent-skills/discovery")
    assert response.status_code == 200
    body = response.json()
    assert body["skill_count"] == 8
    assert body["host_deployment_status"] == "repository_harness_only"
    assert body["network_called"] is False
    assert all(set(item) == {
        "skill_id", "version", "binding", "read_only", "artifact_sha256"
    } for item in body["skills"])
    assert all(item["read_only"] is True and item["binding"].endswith(".read")
               and len(item["artifact_sha256"]) == 64 for item in body["skills"])
    serialized = response.text.casefold()
    for forbidden in ("input_schema", "triggers", "artifact\"", "credential", "api_key"):
        assert forbidden not in serialized


def test_status_allowlist_drops_injected_secrets_and_parameters(tmp_path):
    client, _ = _client(tmp_path)
    response = _get(client, "/api/v1/formal-agent-skills/status")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "registered_skill_count": 8,
        "browser_invocation_allowed": False,
        "grant_issuance_allowed": False,
        "host_deployment_status": "repository_harness_only",
        "network_called": False,
    }
    assert "must-not-leak" not in response.text
    assert "parameter" not in response.text.casefold()


def test_empty_and_blocked_audit_summaries_are_aggregate_only(tmp_path):
    client, audit = _client(tmp_path)
    empty = _get(client, "/api/v1/formal-agent-skills/audit-summary")
    assert empty.json()["total_events"] == 0
    assert empty.json()["latest_event_type"] is None

    audit.deny({
        "invocation_id": "INV-secret-token-1234567890",
        "skill_id": "inspect-channel-health",
        "decision_id": "DEC-1",
        "evidence_ids": ["EVID-1"],
        "api_key": "must-not-leak",
    }, "authorization bearer must-not-leak")
    summary = _get(client, "/api/v1/formal-agent-skills/audit-summary")
    assert summary.json() == {
        "total_events": 1,
        "latest_event_type": "formal_skill_invocation_blocked",
        "host_deployment_status": "repository_harness_only",
        "network_called": False,
    }
    assert "must-not-leak" not in summary.text
    assert "decision" not in summary.text.casefold()
    assert "evidence" not in summary.text.casefold()


def test_audit_provider_is_resolved_on_each_request(tmp_path):
    registry = FormalAgentSkillRegistry()
    first = FormalAgentSkillAuditService(tmp_path / "first.sqlite3")
    second = FormalAgentSkillAuditService(tmp_path / "second.sqlite3")
    selected = {"service": first}

    def secure(request: Request) -> None:
        if request.headers.get("origin") != APPROVED_ORIGIN:
            raise HTTPException(403)

    app = FastAPI()
    app.include_router(build_formal_agent_skill_router(
        registry, lambda: selected["service"], {"status": "ready"}, secure))
    client = TestClient(app)
    assert _get(client, "/api/v1/formal-agent-skills/audit-summary").json()["total_events"] == 0

    second.deny({}, "blocked")
    selected["service"] = second
    assert _get(client, "/api/v1/formal-agent-skills/audit-summary").json()["total_events"] == 1


def test_openapi_has_get_metadata_routes_and_no_mutation_surface(tmp_path):
    client, _ = _client(tmp_path)
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/api/v1/formal-agent-skills/discovery",
        "/api/v1/formal-agent-skills/status",
        "/api/v1/formal-agent-skills/audit-summary",
    }
    assert set(paths) == expected
    assert all(set(paths[path]) == {"get"} for path in expected)
    assert client.post(
        "/api/v1/formal-agent-skills/invoke",
        headers={"Origin": APPROVED_ORIGIN}, json={"arguments": {}},
    ).status_code == 404
    assert client.post(
        "/api/v1/formal-agent-skills/grants",
        headers={"Origin": APPROVED_ORIGIN}, json={},
    ).status_code == 404
