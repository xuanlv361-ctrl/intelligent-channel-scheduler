from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class PrincipalType(str, Enum):
    HUMAN = "human"
    SERVICE = "service"


_FACTORY_MARKER = object()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("principal_timestamp_timezone_required")
    return value.astimezone(timezone.utc)


def _required(value: str, code: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > 256 or any(ch in result for ch in "\r\n\x00"):
        raise ValueError(code)
    return result


class PrincipalContext:
    """Immutable, factory-issued server-side identity context.

    Calling ``PrincipalContext(...)`` is intentionally unsupported.  Contexts
    are issued only by validated identity/session/service adapters in this
    package and carry a process-local verification marker checked by RBAC.
    """

    __slots__ = (
        "_principal_id", "_principal_type", "_tenant_id", "_workspace_id",
        "_roles", "_permissions", "_authn_method", "_session_id",
        "_token_id", "_issued_at", "_expires_at", "_correlation_id",
        "_is_development_identity", "_security_version", "_marker",
        "_sealed",
    )

    def __new__(cls, *args, **kwargs):  # pragma: no cover - defensive API wall
        raise TypeError("principal_context_must_be_resolved")

    @classmethod
    def _issue(
        cls,
        *,
        principal_id: str,
        principal_type: PrincipalType | str,
        tenant_id: str,
        workspace_id: str,
        roles: Iterable[str],
        permissions: Iterable[str],
        authn_method: str,
        session_id: str | None,
        token_id: str,
        issued_at: datetime,
        expires_at: datetime,
        correlation_id: str,
        is_development_identity: bool,
        security_version: int,
        marker: object,
    ) -> "PrincipalContext":
        if marker is not _FACTORY_MARKER:
            raise TypeError("principal_context_untrusted_factory")
        instance = object.__new__(cls)
        principal_kind = PrincipalType(principal_type)
        issued, expires = _utc(issued_at), _utc(expires_at)
        if expires <= issued:
            raise ValueError("principal_expiry_invalid")
        values = {
            "_principal_id": _required(principal_id, "principal_id_required"),
            "_principal_type": principal_kind,
            "_tenant_id": _required(tenant_id, "tenant_id_required"),
            "_workspace_id": _required(workspace_id, "workspace_id_required"),
            "_roles": frozenset(_required(x, "principal_role_invalid") for x in roles),
            "_permissions": frozenset(
                _required(x, "principal_permission_invalid") for x in permissions),
            "_authn_method": _required(authn_method, "authn_method_required"),
            "_session_id": None if session_id is None else _required(
                session_id, "session_id_invalid"),
            "_token_id": _required(token_id, "token_id_required"),
            "_issued_at": issued,
            "_expires_at": expires,
            "_correlation_id": _required(correlation_id, "correlation_id_required"),
            "_is_development_identity": bool(is_development_identity),
            "_security_version": int(security_version),
            "_marker": _FACTORY_MARKER,
            "_sealed": False,
        }
        if values["_security_version"] < 1:
            raise ValueError("principal_security_version_invalid")
        for key, value in values.items():
            object.__setattr__(instance, key, value)
        object.__setattr__(instance, "_sealed", True)
        return instance

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("principal_context_is_immutable")
        object.__setattr__(self, name, value)

    @property
    def is_verified(self) -> bool:
        return self._marker is _FACTORY_MARKER

    principal_id = property(lambda self: self._principal_id)
    principal_type = property(lambda self: self._principal_type)
    tenant_id = property(lambda self: self._tenant_id)
    workspace_id = property(lambda self: self._workspace_id)
    roles = property(lambda self: self._roles)
    permissions = property(lambda self: self._permissions)
    authn_method = property(lambda self: self._authn_method)
    session_id = property(lambda self: self._session_id)
    token_id = property(lambda self: self._token_id)
    issued_at = property(lambda self: self._issued_at)
    expires_at = property(lambda self: self._expires_at)
    correlation_id = property(lambda self: self._correlation_id)
    is_development_identity = property(lambda self: self._is_development_identity)
    security_version = property(lambda self: self._security_version)

    def audit_fields(self) -> dict[str, object]:
        return {
            "principal_id": self.principal_id,
            "principal_type": self.principal_type.value,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "roles": sorted(self.roles),
            "authn_method": self.authn_method,
            "session_id": self.session_id,
            "token_id": self.token_id,
            "correlation_id": self.correlation_id,
            "is_development_identity": self.is_development_identity,
            "security_version": self.security_version,
        }


def issue_principal(**values) -> PrincipalContext:
    """Package-internal factory used only after an adapter validates identity."""
    return PrincipalContext._issue(marker=_FACTORY_MARKER, **values)
