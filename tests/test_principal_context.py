from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.security.authorization import AuthorizationService
from backend.security.config import EnterpriseIdentityConfig
from backend.security.identity import (
    IdentityError, PrincipalResolver, WindowsDevelopmentIdentityProvider,
)
from backend.security.principal import PrincipalContext, PrincipalType


ROOT = Path(__file__).resolve().parents[1]


def stack():
    config = EnterpriseIdentityConfig.load(ROOT / "config/enterprise_identity_v1.json")
    authz = AuthorizationService(ROOT / "config/rbac_permissions_v1.json")
    return config, authz, PrincipalResolver(config, authz)


def test_principal_context_cannot_be_constructed_or_mutated():
    with pytest.raises(TypeError, match="principal_context_must_be_resolved"):
        PrincipalContext()
    config, authz, resolver = stack()
    context = resolver.resolve(WindowsDevelopmentIdentityProvider(
        config, username_loader=lambda: "Alice"), correlation_id="corr-1")
    assert context.principal_id == "windows-dev:alice"
    assert context.principal_type is PrincipalType.HUMAN
    assert context.tenant_id == "tenant_local_dev_v1"
    assert context.workspace_id == "workspace_local_dev_v1"
    assert context.roles == frozenset({
        "security_engineering_owner", "domestic_uat_operator",
        "operations_admin"})
    assert context.permissions == authz.permissions
    assert context.is_verified and context.is_development_identity
    with pytest.raises(AttributeError, match="principal_context_is_immutable"):
        context._tenant_id = "forged"


def test_development_identity_is_rejected_outside_development():
    config, authz, _ = stack()
    production = replace(config, deployment_mode="production",
                         development_identity_enabled=False)
    provider = WindowsDevelopmentIdentityProvider(
        production, username_loader=lambda: "Alice")
    with pytest.raises(IdentityError, match="development_identity_forbidden"):
        PrincipalResolver(production, authz).resolve(provider)


def test_windows_adapter_accepts_no_client_credential():
    config, _, resolver = stack()
    provider = WindowsDevelopmentIdentityProvider(config, username_loader=lambda: "Alice")
    with pytest.raises(IdentityError, match="does_not_accept_credentials"):
        resolver.resolve(provider, credential={"roles": ["operations_admin"]})


def test_oidc_contract_does_not_fake_success_when_unconfigured():
    config, _, resolver = stack()
    assert config.oidc.status == "pending_external_configuration"
    assert not config.oidc.configured
    with pytest.raises(IdentityError, match="oidc_pending_external_configuration"):
        resolver.resolve_oidc("not-a-real-token")


def test_principal_audit_fields_exclude_credentials():
    config, _, resolver = stack()
    context = resolver.resolve(WindowsDevelopmentIdentityProvider(
        config, username_loader=lambda: "Alice",
        clock=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc)))
    serialized = str(context.audit_fields()).casefold()
    assert "password" not in serialized
    assert "authorization" not in serialized
    assert "cookie" not in serialized
