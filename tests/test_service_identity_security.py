from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.security.authorization import AuthorizationError, AuthorizationService
from backend.security.config import EnterpriseIdentityConfig
from backend.security.identity import PrincipalResolver
from backend.security.principal import PrincipalType
from backend.security.service_identity import ServiceIdentityError, ServiceIdentityManager


ROOT = Path(__file__).resolve().parents[1]
KEY = b"service-test-signing-material-is-injected-0001"


class Clock:
    def __init__(self): self.value = datetime(2026, 8, 2, tzinfo=timezone.utc)
    def __call__(self): return self.value
    def advance(self, seconds): self.value += timedelta(seconds=seconds)


def setup(tmp_path, clock):
    config = EnterpriseIdentityConfig.load(ROOT / "config/enterprise_identity_v1.json")
    authz = AuthorizationService(ROOT / "config/rbac_permissions_v1.json", clock=clock)
    resolver = PrincipalResolver(config, authz)
    manager = ServiceIdentityManager(tmp_path / "service.sqlite3",
        ROOT / "config/service_identities_v1.json", signing_key=KEY,
        resolver=resolver, clock=clock)
    return manager, authz


def test_distinct_service_identity_and_least_privilege(tmp_path):
    clock = Clock(); manager, authz = setup(tmp_path, clock)
    token, metadata = manager.issue("collector", "tenant-a", "workspace-a",
                                    "collector-api", ttl_seconds=60)
    principal = manager.validate(token, "collector-api", request_nonce="n-1")
    assert principal.principal_type is PrincipalType.SERVICE
    assert principal.session_id is None
    assert principal.roles == {"collector_service"}
    authz.authorize(principal, "evidence.import", tenant_id="tenant-a")
    with pytest.raises(AuthorizationError, match="permission_denied"):
        authz.authorize(principal, "tenant.manage")
    assert token.encode() not in (tmp_path / "service.sqlite3").read_bytes()
    assert "token" not in metadata


def test_audience_replay_expiry_and_revocation_fail_closed(tmp_path):
    clock = Clock(); manager, _ = setup(tmp_path, clock)
    token, metadata = manager.issue("worker", "tenant-a", "workspace-a",
                                    "worker-api", ttl_seconds=10)
    with pytest.raises(ServiceIdentityError, match="service_audience_mismatch"):
        manager.validate(token, "metrics-api", request_nonce="wrong-aud")
    manager.validate(token, "worker-api", request_nonce="same")
    with pytest.raises(ServiceIdentityError, match="service_token_replay_detected"):
        manager.validate(token, "worker-api", request_nonce="same")
    assert manager.revoke(metadata["credential_id"], "rotation")
    with pytest.raises(ServiceIdentityError, match="service_credential_revoked"):
        manager.validate(token, "worker-api", request_nonce="after-revoke")
    token, _ = manager.issue("worker", "tenant-a", "workspace-a",
                             "worker-api", ttl_seconds=10)
    clock.advance(11)
    with pytest.raises(ServiceIdentityError, match="service_credential_expired"):
        manager.validate(token, "worker-api", request_nonce="expired")


def test_rotation_revokes_old_credential(tmp_path):
    clock = Clock(); manager, _ = setup(tmp_path, clock)
    old, old_meta = manager.issue("metrics", "tenant-a", "workspace-a",
                                  "metrics-api", ttl_seconds=60)
    new, _ = manager.issue("metrics", "tenant-a", "workspace-a",
                           "metrics-api", ttl_seconds=60,
                           rotate_credential_id=old_meta["credential_id"])
    with pytest.raises(ServiceIdentityError, match="service_credential_revoked"):
        manager.validate(old, "metrics-api", request_nonce="old")
    assert manager.validate(new, "metrics-api", request_nonce="new").roles == {"metrics_service"}


def test_unknown_service_and_overlong_credentials_are_rejected(tmp_path):
    clock = Clock(); manager, _ = setup(tmp_path, clock)
    with pytest.raises(ServiceIdentityError, match="service_identity_not_authorized"):
        manager.issue("unknown", "tenant-a", "workspace-a", "worker-api", ttl_seconds=60)
    with pytest.raises(ServiceIdentityError, match="service_credential_ttl_invalid"):
        manager.issue("worker", "tenant-a", "workspace-a", "worker-api", ttl_seconds=3601)

