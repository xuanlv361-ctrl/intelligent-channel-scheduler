"""Tenant/workspace scope primitives used by enterprise security controls.

This module deliberately contains no request parsing.  A :class:`TenantScope`
must be created by a verified identity adapter and then passed down to storage,
cache, idempotency and lease layers.  Keeping the key format here prevents each
service from inventing a subtly different tenant boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


DEFAULT_DEVELOPMENT_TENANT_ID = "tenant_local_dev_v1"
DEFAULT_DEVELOPMENT_WORKSPACE_ID = "workspace_local_dev_v1"
SCOPE_KEY_VERSION = "enterprise_scope_key_v1"

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class TenantScopeError(ValueError):
    """Raised when a scope or scoped key component is unsafe or incomplete."""


def _validated_identifier(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TenantScopeError(f"{field}_must_be_string")
    if value != value.strip() or not _SAFE_IDENTIFIER.fullmatch(value):
        raise TenantScopeError(f"{field}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class TenantScope:
    """Authoritative tenant/workspace identity for a server-side operation."""

    tenant_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "tenant_id", _validated_identifier(self.tenant_id, "tenant_id")
        )
        object.__setattr__(
            self,
            "workspace_id",
            _validated_identifier(self.workspace_id, "workspace_id"),
        )

    @classmethod
    def local_development(cls) -> "TenantScope":
        """Return the stable scope reserved for migration and local development."""

        return cls(
            DEFAULT_DEVELOPMENT_TENANT_ID,
            DEFAULT_DEVELOPMENT_WORKSPACE_ID,
        )

    def assert_same(self, other: "TenantScope") -> None:
        if not isinstance(other, TenantScope) or other != self:
            raise TenantScopeError("tenant_workspace_scope_conflict")

    def sql_parameters(self) -> tuple[str, str]:
        return self.tenant_id, self.workspace_id

    def resource_key(
        self, *, resource_type: str, resource_id: str, version: str
    ) -> str:
        """Build a collision-resistant, inspectable tenant-scoped resource key."""

        resource_type = _validated_identifier(resource_type, "resource_type")
        resource_id = _validated_identifier(resource_id, "resource_id")
        version = _validated_identifier(version, "version")
        return (
            f"{SCOPE_KEY_VERSION}|tenant={self.tenant_id}"
            f"|workspace={self.workspace_id}|type={resource_type}"
            f"|resource={resource_id}|version={version}"
        )

    def cache_key(
        self, *, resource_type: str, resource_id: str, version: str
    ) -> str:
        return "cache|" + self.resource_key(
            resource_type=resource_type, resource_id=resource_id, version=version
        )

    def idempotency_key(
        self, *, operation: str, external_key: str, version: str
    ) -> str:
        operation = _validated_identifier(operation, "operation")
        external_key = _validated_identifier(external_key, "external_key")
        # Do not echo an operator-supplied key into logs/cache diagnostics.
        digest = hashlib.sha256(external_key.encode("utf-8")).hexdigest()
        return "idempotency|" + self.resource_key(
            resource_type=f"operation:{operation}",
            resource_id=digest,
            version=version,
        )

    def lease_key(
        self, *, task_type: str, resource_scope: str, version: str
    ) -> str:
        task_type = _validated_identifier(task_type, "task_type")
        return "lease|" + self.resource_key(
            resource_type=f"task:{task_type}",
            resource_id=resource_scope,
            version=version,
        )


__all__ = [
    "DEFAULT_DEVELOPMENT_TENANT_ID",
    "DEFAULT_DEVELOPMENT_WORKSPACE_ID",
    "SCOPE_KEY_VERSION",
    "TenantScope",
    "TenantScopeError",
]
