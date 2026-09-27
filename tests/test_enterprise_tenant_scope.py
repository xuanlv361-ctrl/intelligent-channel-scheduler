from __future__ import annotations

import pytest

from backend.tenant_security import (
    DEFAULT_DEVELOPMENT_TENANT_ID,
    DEFAULT_DEVELOPMENT_WORKSPACE_ID,
    TenantScope,
    TenantScopeError,
)


def test_stable_local_development_scope() -> None:
    scope = TenantScope.local_development()
    assert scope.tenant_id == DEFAULT_DEVELOPMENT_TENANT_ID
    assert scope.workspace_id == DEFAULT_DEVELOPMENT_WORKSPACE_ID
    assert scope.sql_parameters() == (
        "tenant_local_dev_v1",
        "workspace_local_dev_v1",
    )


def test_resource_and_cache_keys_include_every_scope_component() -> None:
    scope = TenantScope("tenant_a", "workspace_blue")
    key = scope.cache_key(
        resource_type="metric_snapshot",
        resource_id="snapshot_42",
        version="metric_v3",
    )
    assert "tenant=tenant_a" in key
    assert "workspace=workspace_blue" in key
    assert "type=metric_snapshot" in key
    assert "resource=snapshot_42" in key
    assert "version=metric_v3" in key


def test_same_external_idempotency_key_is_isolated_between_tenants() -> None:
    first = TenantScope("tenant_a", "workspace_main").idempotency_key(
        operation="collector_execute", external_key="operator-key-1", version="v1"
    )
    second = TenantScope("tenant_b", "workspace_main").idempotency_key(
        operation="collector_execute", external_key="operator-key-1", version="v1"
    )
    assert first != second
    assert "operator-key-1" not in first


def test_lease_key_is_scoped_by_tenant_workspace_task_and_version() -> None:
    key = TenantScope("tenant_a", "workspace_a").lease_key(
        task_type="collector", resource_scope="china_uat", version="lease_v2"
    )
    assert "tenant=tenant_a" in key
    assert "workspace=workspace_a" in key
    assert "type=task:collector" in key
    assert "resource=china_uat" in key
    assert "version=lease_v2" in key


@pytest.mark.parametrize(
    "tenant,workspace",
    [
        ("", "workspace"),
        (" tenant", "workspace"),
        ("tenant", "workspace/escape"),
        ("tenant|other", "workspace"),
        ("tenant", "工作区"),
        ("a" * 129, "workspace"),
    ],
)
def test_scope_rejects_unsafe_or_ambiguous_identifiers(
    tenant: str, workspace: str
) -> None:
    with pytest.raises(TenantScopeError):
        TenantScope(tenant, workspace)


def test_scoped_key_rejects_unsafe_resource_components() -> None:
    scope = TenantScope("tenant", "workspace")
    with pytest.raises(TenantScopeError, match="resource_id_invalid"):
        scope.cache_key(resource_type="snapshot", resource_id="../other", version="v1")


def test_assert_same_fails_closed_on_cross_tenant_or_non_scope() -> None:
    scope = TenantScope("tenant_a", "workspace")
    scope.assert_same(TenantScope("tenant_a", "workspace"))
    with pytest.raises(TenantScopeError, match="tenant_workspace_scope_conflict"):
        scope.assert_same(TenantScope("tenant_b", "workspace"))
    with pytest.raises(TenantScopeError, match="tenant_workspace_scope_conflict"):
        scope.assert_same(None)  # type: ignore[arg-type]
