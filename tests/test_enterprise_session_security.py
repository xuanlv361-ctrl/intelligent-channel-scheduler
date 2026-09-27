from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from backend.security.authorization import AuthorizationService
from backend.security.config import EnterpriseIdentityConfig
from backend.security.identity import PrincipalResolver, WindowsDevelopmentIdentityProvider
from backend.security.session import EnterpriseSessionManager, SessionError


ROOT = Path(__file__).resolve().parents[1]
KEY = b"session-test-signing-material-is-injected-0001"


class Clock:
    def __init__(self): self.value = datetime(2026, 8, 2, tzinfo=timezone.utc)
    def __call__(self): return self.value
    def advance(self, seconds): self.value += timedelta(seconds=seconds)


def setup(tmp_path, clock, db_name="sessions.sqlite3"):
    config = EnterpriseIdentityConfig.load(ROOT / "config/enterprise_identity_v1.json")
    authz = AuthorizationService(ROOT / "config/rbac_permissions_v1.json", clock=clock)
    resolver = PrincipalResolver(config, authz)
    principal = resolver.resolve(WindowsDevelopmentIdentityProvider(
        config, username_loader=lambda: "Alice", clock=clock), correlation_id="corr-session")
    manager = EnterpriseSessionManager(tmp_path / db_name, signing_key=KEY,
        resolver=resolver, absolute_ttl_seconds=100, idle_ttl_seconds=20,
        clock=clock)
    return manager, principal, resolver


def test_session_creation_validation_and_no_raw_token_persistence(tmp_path):
    clock = Clock(); manager, principal, _ = setup(tmp_path, clock)
    token, created = manager.create(principal)
    validated = manager.validate(token, request_nonce="nonce-1")
    assert validated.session_id == created.session_id
    assert validated.principal.tenant_id == principal.tenant_id
    raw = (tmp_path / "sessions.sqlite3").read_bytes()
    assert token.encode() not in raw


def test_revocation_and_restart_persistence(tmp_path):
    clock = Clock(); manager, principal, resolver = setup(tmp_path, clock)
    token, created = manager.create(principal)
    assert manager.revoke_session(created.session_id, "operator_logout")
    restarted = EnterpriseSessionManager(tmp_path / "sessions.sqlite3", signing_key=KEY,
        resolver=resolver, absolute_ttl_seconds=100, idle_ttl_seconds=20, clock=clock)
    with pytest.raises(SessionError, match="session_revoked"):
        restarted.validate(token)


def test_idle_and_absolute_expiry_fail_closed(tmp_path):
    clock = Clock(); manager, principal, _ = setup(tmp_path, clock, "idle.sqlite3")
    token, _ = manager.create(principal)
    clock.advance(21)
    with pytest.raises(SessionError, match="session_idle_expired"):
        manager.validate(token)
    clock = Clock(); manager, principal, _ = setup(tmp_path, clock, "absolute.sqlite3")
    token, _ = manager.create(principal, idle_ttl_seconds=100)
    clock.advance(101)
    with pytest.raises(SessionError, match="session_absolute_expired"):
        manager.validate(token)


def test_request_nonce_replay_is_rejected(tmp_path):
    clock = Clock(); manager, principal, _ = setup(tmp_path, clock)
    token, _ = manager.create(principal)
    manager.validate(token, request_nonce="same-nonce", nonce_purpose="mutation")
    with pytest.raises(SessionError, match="session_replay_detected"):
        manager.validate(token, request_nonce="same-nonce", nonce_purpose="mutation")


def test_role_and_tenant_security_version_invalidate_old_session(tmp_path):
    clock = Clock(); manager, principal, _ = setup(tmp_path, clock, "role.sqlite3")
    token, _ = manager.create(principal)
    manager.bump_principal_security_version(principal.principal_id)
    with pytest.raises(SessionError, match="session_roles_changed"):
        manager.validate(token)
    clock = Clock(); manager, principal, _ = setup(tmp_path, clock, "tenant.sqlite3")
    token, _ = manager.create(principal)
    assert manager.revoke_tenant(principal.tenant_id, "security_incident") == 1
    with pytest.raises(SessionError, match="session_revoked"):
        manager.validate(token)


def test_all_principal_sessions_are_revoked(tmp_path):
    clock = Clock(); manager, principal, _ = setup(tmp_path, clock)
    first, _ = manager.create(principal); second, _ = manager.create(principal)
    assert manager.revoke_all_for_principal(principal.principal_id, "roles_changed") == 2
    for token in (first, second):
        with pytest.raises(SessionError, match="session_revoked"):
            manager.validate(token)


def test_session_creation_after_tenant_scope_columns_are_migrated(tmp_path):
    clock = Clock(); manager, principal, _ = setup(tmp_path, clock, "migrated.sqlite3")
    with sqlite3.connect(tmp_path / "migrated.sqlite3") as db:
        db.execute("ALTER TABLE enterprise_security_versions ADD COLUMN tenant_id "
                   "TEXT NOT NULL DEFAULT 'tenant_local_dev_v1'")
        db.execute("ALTER TABLE enterprise_security_versions ADD COLUMN workspace_id "
                   "TEXT NOT NULL DEFAULT 'workspace_local_dev_v1'")
        db.execute("ALTER TABLE enterprise_session_replay_nonces ADD COLUMN tenant_id "
                   "TEXT NOT NULL DEFAULT 'tenant_local_dev_v1'")
        db.execute("ALTER TABLE enterprise_session_replay_nonces ADD COLUMN workspace_id "
                   "TEXT NOT NULL DEFAULT 'workspace_local_dev_v1'")
    token, created = manager.create(principal)
    assert manager.validate(token, request_nonce="migrated-nonce").session_id == created.session_id
    with pytest.raises(SessionError, match="session_replay_detected"):
        manager.validate(token, request_nonce="migrated-nonce")
    with sqlite3.connect(tmp_path / "migrated.sqlite3") as db:
        scopes = db.execute("""SELECT DISTINCT tenant_id,workspace_id
          FROM enterprise_security_versions""").fetchall()
    assert scopes == [(principal.tenant_id, principal.workspace_id)]
