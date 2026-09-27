from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.agent_skill_host_credential_vault import AgentSkillHostCredentialVault
from backend.agent_skill_host_service import (
    AgentSkillHostError,
    AgentSkillHostService,
    HostTransportResponse,
)
from backend.tenant_security import TenantScope


class MemoryProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        assert ciphertext.startswith(b"protected:")
        return ciphertext[len(b"protected:"):][::-1]


class FakeTransport:
    def __init__(self, responses=None, error: Exception | None = None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.responses.pop(0)


def public_dns(*_args, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", 443))]


def service(tmp_path: Path, *, transport=None, scope=None):
    scope = scope or TenantScope("tenant-a", "workspace-a")
    vault = AgentSkillHostCredentialVault(scope, tmp_path / (scope.tenant_id + "-vault"), MemoryProtector())
    return AgentSkillHostService(
        tmp_path / "hosts.db", scope=scope, vault=vault, transport=transport,
        allowed_hosts={"agent.example.com"}, dns_resolver=public_dns,
    )


def create(svc: AgentSkillHostService):
    return svc.create_host(host_name="Domestic Agent", host_type="formal_runtime",
                           base_url="https://agent.example.com", auth_type="bearer")


def connected_response(**overrides):
    payload = {"protocol_version": "1.0", "host_version": "2.3",
               "capabilities": ["skills.invoke"], "installed_skills": []}
    payload.update(overrides)
    return HostTransportResponse(200, payload, latency_ms=12)


def test_host_crud_is_persistent_and_schema_migration_is_idempotent(tmp_path):
    svc = service(tmp_path)
    host = create(svc)
    assert host["status"] == "pending_test" and not host["enabled"]
    changed = svc.update_host(host["host_id"], host_name="Renamed Agent")
    assert changed["host_name"] == "Renamed Agent"
    reopened = service(tmp_path)
    assert reopened.get_host(host["host_id"])["host_name"] == "Renamed Agent"
    assert reopened.list_hosts()["count"] == 1


def test_registry_migration_merges_duplicate_host_identity_without_losing_audit(tmp_path):
    svc = service(tmp_path)
    first = create(svc)
    second = create(svc)
    assert first["host_id"] != second["host_id"]
    assert svc.list_hosts()["count"] == 2

    reopened = service(tmp_path)
    hosts = reopened.list_hosts()["items"]
    assert len(hosts) == 1
    audits = reopened.audits(hosts[0]["host_id"])
    assert any(item["operation"] == "deduplicate" for item in audits)
    assert sum(item["operation"] == "create" for item in audits) == 2


def test_unconfigured_host_connection_failure_is_persisted_to_audit(tmp_path):
    svc = service(tmp_path)
    host = svc.create_host(host_name="Pending Host", host_type="remote_agent", base_url="")
    assert host["status"] == "not_configured"
    with pytest.raises(AgentSkillHostError, match="host_not_configured"):
        svc.test_connection(host["host_id"])
    audit = svc.audits(host["host_id"])[0]
    assert audit["operation"] == "test_connection"
    assert audit["result"] == "failed"
    assert audit["error_code"] == "host_not_configured"


def test_tenant_isolation_applies_to_hosts_credentials_and_audit(tmp_path):
    first = service(tmp_path, scope=TenantScope("tenant-a", "workspace-a"))
    host = create(first)
    first.set_credentials(host["host_id"], "only-tenant-a-secret")
    second = service(tmp_path, scope=TenantScope("tenant-b", "workspace-b"))
    assert second.list_hosts()["items"] == []
    with pytest.raises(AgentSkillHostError, match="host_not_configured"):
        second.get_host(host["host_id"])
    with pytest.raises(Exception):
        second.vault.load(host["host_id"], first.vault.reference(host["host_id"]))
    assert second.audits() == []


def test_credentials_are_encrypted_and_rotation_or_revoke_requires_retest(tmp_path):
    transport = FakeTransport([connected_response()])
    svc = service(tmp_path, transport=transport)
    host = create(svc)
    saved = svc.set_credentials(host["host_id"], "top-secret-first")
    assert saved["credential_fingerprint"].startswith("sha256:")
    svc.test_connection(host["host_id"])
    svc.enable_host(host["host_id"])
    rotated = svc.set_credentials(host["host_id"], "top-secret-second")
    assert rotated["status"] == "pending_test" and not rotated["enabled"]
    revoked = svc.revoke_credentials(host["host_id"])
    assert revoked["status"] == "pending_test"
    raw = (tmp_path / "hosts.db").read_bytes()
    vault_bytes = b"".join(path.read_bytes() for path in (tmp_path / "tenant-a-vault").glob("*"))
    assert b"top-secret-first" not in raw + vault_bytes
    assert b"top-secret-second" not in raw + vault_bytes
    with sqlite3.connect(tmp_path / "hosts.db") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(formal_agent_skill_hosts)")}
        assert "secret_ref" in columns and "credential_fingerprint" in columns


@pytest.mark.parametrize(("response","error_code","status"), [
    (HostTransportResponse(401, {}), "host_authentication_failed", "authentication_failed"),
    (HostTransportResponse(200, {"protocol_version":"9.9","capabilities":[],"installed_skills":[]}),
     "host_protocol_incompatible", "protocol_incompatible"),
])
def test_connection_failure_is_classified_and_audited(tmp_path,response,error_code,status):
    svc = service(tmp_path, transport=FakeTransport([response]))
    host = create(svc); svc.set_credentials(host["host_id"], "test-secret")
    with pytest.raises(AgentSkillHostError, match=error_code):
        svc.test_connection(host["host_id"])
    assert svc.get_host(host["host_id"])["status"] == status
    audit = svc.audits(host["host_id"])[0]
    assert audit["result"] == "failed" and audit["error_code"] == error_code
    assert "test-secret" not in str(audit)


def test_network_failure_and_redirect_are_not_connected(tmp_path):
    for transport in (FakeTransport(error=OSError("network down")),
                      FakeTransport([HostTransportResponse(302, {}, headers={"location":"https://evil.test"})])):
        svc = service(tmp_path / str(id(transport)), transport=transport)
        host = create(svc); svc.set_credentials(host["host_id"], "test-secret")
        with pytest.raises(AgentSkillHostError, match="host_connection_failed"):
            svc.test_connection(host["host_id"])
        assert svc.get_host(host["host_id"])["status"] == "network_failed"
        assert transport.calls[0]["follow_redirects"] is False


def test_url_policy_rejects_http_private_and_non_allowlisted_hosts(tmp_path):
    svc = service(tmp_path)
    for url in ("http://agent.example.com", "https://127.0.0.1", "https://evil.example.net"):
        with pytest.raises(AgentSkillHostError):
            svc.create_host(host_name="bad",host_type="formal_runtime",base_url=url)


def test_disabled_or_untested_host_cannot_be_invoked(tmp_path):
    svc = service(tmp_path,transport=FakeTransport())
    host = create(svc)
    with pytest.raises(AgentSkillHostError,match="host_not_enabled"):
        svc.invoke_external(host["host_id"],"sample","1.0",{})
    invocation = svc.invocations(host["host_id"])[0]
    assert invocation["result"] == "failed"
    assert svc.audits(host["host_id"])[0]["operation"] == "invoke"


def test_disabled_connected_host_can_be_reenabled_without_faking_a_test(tmp_path):
    svc = service(tmp_path,transport=FakeTransport([connected_response()]))
    host = create(svc); svc.set_credentials(host["host_id"],"host-secret")
    svc.test_connection(host["host_id"]); svc.enable_host(host["host_id"])
    assert svc.disable_host(host["host_id"])["status"] == "disabled"
    restored = svc.enable_host(host["host_id"])
    assert restored["enabled"] is True and restored["status"] == "connected"


def test_successful_external_invocation_has_ids_and_redacted_persistence(tmp_path):
    transport = FakeTransport([
        connected_response(),
        HostTransportResponse(200,{"status":"success","request_id":"REQ-1","decision_id":"DEC-1",
                                   "authorization":"must-not-persist","write_occurred":False,
                                   "network_called":False,"remote_invocation_id":"RIN-1",
                                   "remote_audit_id":"RAA-1","installation_id":"FSHI-1",
                                   "skill_id":"sample","skill_version":"1.0","latency_ms":8,
                                   "execution_mode":"local_compute","data_source":"independent_agent_host",
                                   "data":{"summary":"ok"}},latency_ms=9),
    ])
    svc=service(tmp_path,transport=transport); host=create(svc)
    svc.set_credentials(host["host_id"],"host-secret")
    svc.test_connection(host["host_id"]); svc.enable_host(host["host_id"])
    svc.install_skill(host["host_id"],"sample","1.0")
    result=svc.invoke_external(host["host_id"],"sample","1.0",{"prompt":"safe"})
    assert result["invocation_id"].startswith("FSI-")
    assert result["audit_id"].startswith("FSHA-")
    assert result["network_called"] is True
    stored=svc.invocations(host["host_id"])[0]
    assert stored["safe_result"]["authorization"] == "<redacted>"
    assert stored["request_id"] == "REQ-1" and stored["decision_id"] == "DEC-1"
    assert transport.calls[-1]["follow_redirects"] is False
    assert "host-secret" not in str(svc.audits(host["host_id"]))


def test_failed_external_invocation_also_persists_audit_and_ids(tmp_path):
    transport=FakeTransport([connected_response(),HostTransportResponse(500,{"token":"leak"})])
    svc=service(tmp_path,transport=transport); host=create(svc)
    svc.set_credentials(host["host_id"],"host-secret")
    svc.test_connection(host["host_id"]); svc.enable_host(host["host_id"])
    svc.install_skill(host["host_id"],"sample","1.0")
    with pytest.raises(AgentSkillHostError,match="host_connection_failed") as caught:
        svc.invoke_external(host["host_id"],"sample","1.0",{})
    assert caught.value.safe_context["invocation_id"].startswith("FSI-")
    assert caught.value.safe_context["audit_id"].startswith("FSHA-")
    invocation=svc.invocations(host["host_id"])[0]
    assert invocation["invocation_id"].startswith("FSI-") and invocation["audit_id"].startswith("FSHA-")
    assert invocation["result"] == "failed"
    assert svc.audits(host["host_id"])[0]["result"] == "failed"


def test_remote_permission_denial_is_not_misclassified_as_authentication(tmp_path):
    transport = FakeTransport([
        connected_response(),
        HostTransportResponse(403, {"code": "permission_denied", "message": "denied"}),
    ])
    svc = service(tmp_path, transport=transport); host = create(svc)
    svc.set_credentials(host["host_id"], "host-secret")
    svc.test_connection(host["host_id"]); svc.enable_host(host["host_id"])
    svc.install_skill(host["host_id"], "sample", "1.0")
    with pytest.raises(AgentSkillHostError, match="permission_denied"):
        svc.invoke_external(host["host_id"], "sample", "1.0", {"network_url": "blocked"})
    assert svc.invocations(host["host_id"])[0]["safe_result"]["error_code"] == "permission_denied"


def test_disabled_installation_is_distinct_from_missing_installation(tmp_path):
    svc = service(tmp_path, transport=FakeTransport([connected_response()]))
    host = create(svc); svc.set_credentials(host["host_id"], "host-secret")
    svc.test_connection(host["host_id"]); svc.enable_host(host["host_id"])
    installed = svc.install_skill(host["host_id"], "sample", "1.0")
    svc.set_installation_enabled(host["host_id"], installed["installation_id"], False)
    with pytest.raises(AgentSkillHostError, match="skill_not_enabled"):
        svc.invoke_external(host["host_id"], "sample", "1.0", {})


def test_invalid_success_schema_is_rejected_and_audited(tmp_path):
    transport = FakeTransport([connected_response(), HostTransportResponse(200, {"status": "success"})])
    svc = service(tmp_path, transport=transport); host = create(svc)
    svc.set_credentials(host["host_id"], "host-secret")
    svc.test_connection(host["host_id"]); svc.enable_host(host["host_id"])
    svc.install_skill(host["host_id"], "sample", "1.0")
    with pytest.raises(AgentSkillHostError, match="invalid_host_response"):
        svc.invoke_external(host["host_id"], "sample", "1.0", {})
    assert svc.audits(host["host_id"])[0]["error_code"] == "invalid_host_response"
