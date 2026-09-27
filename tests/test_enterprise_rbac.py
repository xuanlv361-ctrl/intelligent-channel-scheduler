from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.security.authorization import AuthorizationError, AuthorizationService
from backend.security.config import EnterpriseIdentityConfig
from backend.security.identity import PrincipalResolver, VerifiedIdentity
from backend.security.principal import PrincipalType


ROOT = Path(__file__).resolve().parents[1]


def components():
    config = EnterpriseIdentityConfig.load(ROOT / "config/enterprise_identity_v1.json")
    authz = AuthorizationService(ROOT / "config/rbac_permissions_v1.json")
    return authz, PrincipalResolver(config, authz)


def context(resolver, role="product_viewer", kind=PrincipalType.HUMAN):
    now = datetime.now(timezone.utc)
    return resolver.resolve_verified(VerifiedIdentity(
        principal_id="user-1" if kind is PrincipalType.HUMAN else "service:worker",
        principal_type=kind, tenant_id="tenant-a", workspace_id="workspace-a",
        roles=(role,), authn_method="test_verified_adapter",
        issued_at=now, expires_at=now + timedelta(minutes=5),
        session_id="SES-test" if kind is PrincipalType.HUMAN else None,
        token_id="JTI-test", is_development_identity=False, security_version=1))


def test_permission_matrix_has_required_roles_and_distinct_kill_switch_permissions():
    authz, _ = components()
    assert set(authz.human_roles) == {
        "domestic_uat_operator", "qa_auditor", "product_viewer",
        "operations_admin", "security_engineering_owner",
    }
    assert set(authz.service_roles) == {
        "scheduler_service", "collector_service", "metrics_service",
        "price_sync_service", "worker_service", "monitoring_service",
    }
    assert "kill_switch.activate" in authz.human_roles["operations_admin"]
    assert "kill_switch.release" not in authz.human_roles["operations_admin"]
    assert "kill_switch.release" in authz.human_roles["security_engineering_owner"]


def test_read_only_role_cannot_execute_write_and_scope_is_enforced():
    authz, resolver = components()
    principal = context(resolver)
    authz.authorize(principal, "snapshot.read", tenant_id="tenant-a",
                    workspace_id="workspace-a")
    with pytest.raises(AuthorizationError, match="permission_denied"):
        authz.authorize(principal, "scheduler.execute")
    with pytest.raises(AuthorizationError, match="tenant_scope_mismatch"):
        authz.authorize(principal, "snapshot.read", tenant_id="tenant-b")
    with pytest.raises(AuthorizationError, match="workspace_scope_mismatch"):
        authz.authorize(principal, "snapshot.read", workspace_id="workspace-b")


def test_service_and_human_types_cannot_be_interchanged():
    authz, resolver = components()
    human = context(resolver)
    service = context(resolver, "worker_service", PrincipalType.SERVICE)
    with pytest.raises(AuthorizationError, match="principal_type_mismatch"):
        authz.authorize(human, "snapshot.read", principal_type=PrincipalType.SERVICE)
    with pytest.raises(AuthorizationError, match="principal_type_mismatch"):
        authz.authorize(service, "snapshot.generate", principal_type=PrincipalType.HUMAN)


def test_worker_permissions_are_intersected_with_task_declaration():
    authz, resolver = components()
    worker = context(resolver, "worker_service", PrincipalType.SERVICE)
    authz.authorize_worker_task(worker, "snapshot.generate", ["snapshot.generate"],
                                tenant_id="tenant-a", workspace_id="workspace-a")
    with pytest.raises(AuthorizationError, match="worker_task_permission_denied"):
        authz.authorize_worker_task(worker, "price.sync", ["snapshot.generate"],
                                    tenant_id="tenant-a", workspace_id="workspace-a")


def test_context_permission_conflict_fails_closed():
    authz, resolver = components()
    principal = context(resolver)
    object.__setattr__(principal, "_permissions", frozenset({"scheduler.execute"}))
    with pytest.raises(AuthorizationError, match="principal_permission_context_conflict"):
        authz.authorize(principal, "scheduler.execute")

