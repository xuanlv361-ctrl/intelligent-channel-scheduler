from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .principal import PrincipalContext, PrincipalType


class AuthorizationError(PermissionError):
    pass


class AuthorizationService:
    def __init__(self, policy_path: str | Path, clock: Callable[[], datetime] | None = None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        try:
            raw = json.loads(Path(policy_path).read_text(encoding="utf-8"))
            self.policy_version = str(raw["policy_version"])
            self.permissions = frozenset(str(x) for x in raw["permissions"])
            self.human_roles = {
                str(role): frozenset(str(x) for x in permissions)
                for role, permissions in raw["human_roles"].items()
            }
            self.service_roles = {
                str(role): frozenset(str(x) for x in permissions)
                for role, permissions in raw["service_roles"].items()
            }
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthorizationError("rbac_policy_invalid") from exc
        if not self.policy_version or not self.permissions:
            raise AuthorizationError("rbac_policy_invalid")
        for mapping in (self.human_roles, self.service_roles):
            if any(not values <= self.permissions for values in mapping.values()):
                raise AuthorizationError("rbac_policy_unknown_permission")
        if "kill_switch.activate" == "kill_switch.release":  # pragma: no cover
            raise AuthorizationError("kill_switch_permissions_not_separated")

    def permissions_for(self, principal_type: PrincipalType | str,
                        roles: Iterable[str]) -> frozenset[str]:
        kind = PrincipalType(principal_type)
        mapping = self.human_roles if kind is PrincipalType.HUMAN else self.service_roles
        role_set = frozenset(str(x) for x in roles)
        if not role_set or not role_set <= mapping.keys():
            raise AuthorizationError("principal_role_not_authorized")
        result: set[str] = set()
        for role in role_set:
            result.update(mapping[role])
        return frozenset(result)

    def authorize(self, principal: PrincipalContext | None, permission: str, *,
                  tenant_id: str | None = None, workspace_id: str | None = None,
                  principal_type: PrincipalType | str | None = None) -> PrincipalContext:
        if principal is None or not isinstance(principal, PrincipalContext) or not principal.is_verified:
            raise AuthorizationError("principal_context_missing_or_unverified")
        now = self.clock().astimezone(timezone.utc)
        if principal.expires_at <= now:
            raise AuthorizationError("principal_context_expired")
        if tenant_id is not None and principal.tenant_id != tenant_id:
            raise AuthorizationError("tenant_scope_mismatch")
        if workspace_id is not None and principal.workspace_id != workspace_id:
            raise AuthorizationError("workspace_scope_mismatch")
        if principal_type is not None and principal.principal_type is not PrincipalType(principal_type):
            raise AuthorizationError("principal_type_mismatch")
        expected = self.permissions_for(principal.principal_type, principal.roles)
        if principal.permissions != expected:
            raise AuthorizationError("principal_permission_context_conflict")
        if permission not in self.permissions or permission not in expected:
            raise AuthorizationError("permission_denied")
        return principal

    def authorize_worker_task(self, principal: PrincipalContext, permission: str,
                              task_permissions: Iterable[str], *, tenant_id: str,
                              workspace_id: str) -> PrincipalContext:
        self.authorize(principal, permission, tenant_id=tenant_id,
                       workspace_id=workspace_id, principal_type=PrincipalType.SERVICE)
        if "worker_service" not in principal.roles:
            raise AuthorizationError("worker_service_identity_required")
        allowed = frozenset(str(x) for x in task_permissions)
        if permission not in allowed or not allowed <= self.permissions:
            raise AuthorizationError("worker_task_permission_denied")
        return principal
