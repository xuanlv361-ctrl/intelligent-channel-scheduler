from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.collector_run_service import CollectorRunError, CollectorRunService
from backend.encrypted_session_vault import EncryptedSessionVault, VaultError
from backend.persistent_session_service import PersistentSessionService
from backend.realtime_log_sync_service import RealtimeLogSyncService
from backend.realtime_sync_supervisor import RealtimeSyncSupervisor
from backend.security import AuthorizationService, EnterpriseIdentityConfig, PrincipalResolver
from backend.security.identity import VerifiedIdentity
from backend.security.principal import PrincipalType
from backend.security.service_identity import ServiceIdentityManager
from collector.collector_worker import resolve_worker_principal


ROOT = Path(__file__).resolve().parents[1]


def security():
    authz = AuthorizationService(ROOT / "config" / "rbac_permissions_v1.json")
    resolver = PrincipalResolver(
        EnterpriseIdentityConfig.load(ROOT / "config" / "enterprise_identity_v1.json"),
        authz,
    )
    return authz, resolver


def human(resolver, tenant: str, workspace: str = "workspace_main"):
    now = datetime.now(timezone.utc)
    return resolver.resolve_verified(VerifiedIdentity(
        principal_id=f"user:{tenant}", principal_type=PrincipalType.HUMAN,
        tenant_id=tenant, workspace_id=workspace,
        roles=("domestic_uat_operator",), authn_method="test_oidc",
        issued_at=now, expires_at=now + timedelta(hours=1),
        session_id=f"session:{tenant}", token_id=f"jti:{tenant}",
        is_development_identity=False, security_version=1,
    ))


def test_collector_runs_fail_closed_across_tenants(tmp_path):
    authz, resolver = security()
    first, second = human(resolver, "tenant_a"), human(resolver, "tenant_b")
    service = CollectorRunService(tmp_path / "collector.sqlite3", authorization=authz)
    kwargs = dict(environment_id="china_uat", date_from="2026-07-01",
                  date_to="2026-07-02", maximum_pages=1, maximum_records=10,
                  page_delay_ms=1000, operator_confirmation=True)
    one = service.create(**kwargs, browser_session="browser-a", principal=first)
    two = service.create(**kwargs, browser_session="browser-b", principal=second)
    assert one["tenant_id"] == "tenant_a" and two["tenant_id"] == "tenant_b"
    with pytest.raises(LookupError, match="collector_run_not_found"):
        service.get(one["run_id"], principal=second)
    with pytest.raises(LookupError, match="collector_run_not_found"):
        service.update(one["run_id"], principal=second, worker_pid=7)


def test_realtime_job_cursor_update_and_stop_are_tenant_scoped(tmp_path):
    authz, resolver = security()
    first, second = human(resolver, "tenant_a"), human(resolver, "tenant_b")
    service = RealtimeLogSyncService(
        tmp_path / "sync.sqlite3", tmp_path / "missing.jsonl", authorization=authz)
    args = ("china_uat", "2026-07-29T00:00:00Z",
            "2026-07-30T00:00:00Z", "Asia/Shanghai")
    one = service.create_job(*args, principal=first)
    two = service.create_job(*args, principal=second)
    assert one["tenant_id"] != two["tenant_id"]
    with pytest.raises(LookupError, match="log_sync_job_not_found"):
        service.get_job(one["sync_job_id"], principal=second)
    with pytest.raises(LookupError, match="log_sync_job_not_found"):
        service.update_job(one["sync_job_id"], principal=second,
                           permission="collector.oneshot.execute", worker_pid=9)
    with pytest.raises(LookupError, match="log_sync_job_not_found"):
        service.stop(one["sync_job_id"], principal=second)
    with service.connect() as db:
        for principal, job, value in ((first, one, "a"), (second, two, "b")):
            db.execute("""INSERT INTO realtime_log_sync_cursors(
              environment_id,cursor_kind,cursor_value,updated_at,sync_job_id,
              tenant_id,workspace_id) VALUES('china_uat','opaque',?,?,?,?,?)""",
              (value, datetime.now(timezone.utc).isoformat(), job["sync_job_id"],
               principal.tenant_id, principal.workspace_id))
    assert service.cursor("china_uat", principal=first)["cursor_value"] == "a"
    assert service.cursor("china_uat", principal=second)["cursor_value"] == "b"


class Protector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"p:" + plaintext

    def unprotect(self, ciphertext: bytes) -> bytes:
        return ciphertext[2:]


def test_vault_aad_rejects_other_tenant_and_legacy_defaults_local(tmp_path):
    vault = EncryptedSessionVault(tmp_path / "vault", Protector())
    now = datetime.now(timezone.utc)
    saved = vault.save(
        "china_uat", now.isoformat(), (now + timedelta(hours=1)).isoformat(),
        [{"name": "sid", "value": "not-logged", "domain": "uat.weimeta.cn"}],
        tenant_id="tenant_a", workspace_id="workspace_main")
    assert vault.load(saved["vault_reference_id"], "china_uat",
                      tenant_id="tenant_a", workspace_id="workspace_main")["tenant_id"] == "tenant_a"
    with pytest.raises(VaultError, match="tenant_scope_mismatch"):
        vault.load(saved["vault_reference_id"], "china_uat",
                   tenant_id="tenant_b", workspace_id="workspace_main")


class FakeProcess:
    pid = 411
    def poll(self): return None


class FakeIdentityManager:
    maximum_ttl_seconds = 3600
    def issue(self, service_name, tenant_id, workspace_id, audience, ttl_seconds):
        assert service_name == "worker" and audience == "worker-api"
        return "secret-child-token", {"credential_id": "SVC-REFERENCE"}


def test_supervisor_uses_authoritative_job_scope_and_persists_only_reference(tmp_path):
    service = RealtimeLogSyncService(tmp_path / "sync.sqlite3", tmp_path / "none")
    job = service.create_job(
        "china_uat", "2026-07-29T00:00:00Z", "2026-07-30T00:00:00Z",
        "Asia/Shanghai")
    observed = {}
    def popen(args, **kwargs):
        observed.update(kwargs["env"])
        return FakeProcess()
    supervisor = RealtimeSyncSupervisor(
        service.database_path, popen=popen, service_identities=FakeIdentityManager())
    assert supervisor.launch(job["sync_job_id"]) == 411
    with service.connect() as db:
        row = db.execute("SELECT service_credential_id FROM realtime_log_sync_jobs \
                         WHERE sync_job_id=?", (job["sync_job_id"],)).fetchone()
        dump = " ".join(str(value) for value in row)
    assert row[0] == "SVC-REFERENCE"
    assert "secret-child-token" not in dump
    assert observed["ROUTING_SERVICE_TOKEN"] == "secret-child-token"


def test_worker_rejects_valid_token_for_wrong_service_name(tmp_path, monkeypatch):
    authz, resolver = security()
    key = "stage1-test-signing-key-material-000000000000"
    manager = ServiceIdentityManager(
        tmp_path / "identity.sqlite3", ROOT / "config" / "service_identities_v1.json",
        signing_key=key.encode(), resolver=resolver)
    token, _ = manager.issue("worker", "tenant_a", "workspace_main", "worker-api",
                             ttl_seconds=60)
    monkeypatch.setenv("ROUTING_SERVICE_TOKEN", token)
    monkeypatch.setenv("ROUTING_SERVICE_REQUEST_NONCE", "nonce-one")
    monkeypatch.setenv("ROUTING_SERVICE_SIGNING_KEY", key)
    monkeypatch.setenv("ROUTING_SERVICE_AUDIENCE", "worker-api")
    monkeypatch.setenv("ROUTING_ENTERPRISE_SECURITY_REQUIRED", "1")
    with pytest.raises(PermissionError, match="worker_service_name_mismatch"):
        resolve_worker_principal(tmp_path / "identity.sqlite3", "collector")

