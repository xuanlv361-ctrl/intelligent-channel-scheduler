from __future__ import annotations

import getpass
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from .authorization import AuthorizationService
from .config import EnterpriseIdentityConfig
from .principal import PrincipalContext, PrincipalType, issue_principal


class IdentityError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    principal_id: str
    principal_type: PrincipalType
    tenant_id: str
    workspace_id: str
    roles: tuple[str, ...]
    authn_method: str
    issued_at: datetime
    expires_at: datetime
    session_id: str | None
    token_id: str
    is_development_identity: bool
    security_version: int


class IdentityProvider(Protocol):
    provider_id: str
    def authenticate(self, credential: object | None = None) -> VerifiedIdentity: ...


class WindowsDevelopmentIdentityProvider:
    provider_id = "windows_current_user_development_v1"

    def __init__(self, config: EnterpriseIdentityConfig, *,
                 username_loader: Callable[[], str] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.config = config
        self.username_loader = username_loader or getpass.getuser
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def authenticate(self, credential: object | None = None) -> VerifiedIdentity:
        if credential is not None:
            raise IdentityError("development_identity_does_not_accept_credentials")
        if self.config.deployment_mode != "development" or not self.config.development_identity_enabled:
            raise IdentityError("development_identity_forbidden")
        username = str(self.username_loader() or "").strip()
        if not username or any(ch in username for ch in "\r\n\x00"):
            raise IdentityError("windows_development_identity_unavailable")
        now = self.clock().astimezone(timezone.utc)
        return VerifiedIdentity(
            principal_id=f"windows-dev:{username.casefold()}",
            principal_type=PrincipalType.HUMAN,
            tenant_id=self.config.default_development_tenant_id,
            workspace_id=self.config.default_development_workspace_id,
            roles=self.config.development_roles,
            authn_method=self.provider_id,
            issued_at=now, expires_at=now + timedelta(hours=8),
            session_id=None, token_id=f"dev-{uuid.uuid4().hex}",
            is_development_identity=True, security_version=1,
        )


class PrincipalResolver:
    def __init__(self, config: EnterpriseIdentityConfig,
                 authorization: AuthorizationService):
        self.config = config
        self.authorization = authorization

    def resolve(self, provider: IdentityProvider, credential: object | None = None,
                *, correlation_id: str | None = None) -> PrincipalContext:
        identity = provider.authenticate(credential)
        return self.resolve_verified(identity, correlation_id=correlation_id)

    def resolve_verified(self, identity: VerifiedIdentity, *,
                         correlation_id: str | None = None) -> PrincipalContext:
        if identity.is_development_identity and self.config.deployment_mode != "development":
            raise IdentityError("development_identity_forbidden_in_production")
        if identity.principal_type is PrincipalType.SERVICE and identity.session_id is not None:
            raise IdentityError("service_identity_cannot_use_human_session")
        if identity.principal_type is PrincipalType.HUMAN and not identity.session_id and \
                not identity.is_development_identity:
            raise IdentityError("human_session_required")
        permissions = self.authorization.permissions_for(
            identity.principal_type, identity.roles)
        return issue_principal(
            principal_id=identity.principal_id,
            principal_type=identity.principal_type,
            tenant_id=identity.tenant_id,
            workspace_id=identity.workspace_id,
            roles=identity.roles,
            permissions=permissions,
            authn_method=identity.authn_method,
            session_id=identity.session_id,
            token_id=identity.token_id,
            issued_at=identity.issued_at,
            expires_at=identity.expires_at,
            correlation_id=correlation_id or f"corr-{uuid.uuid4().hex}",
            is_development_identity=identity.is_development_identity,
            security_version=identity.security_version,
        )

    def resolve_oidc(self, token: str) -> PrincipalContext:
        # Schema and adapter boundary exist, but no network/JWKS verifier is
        # fabricated when the enterprise IdP is not configured.
        del token
        if not self.config.oidc.configured:
            raise IdentityError("oidc_pending_external_configuration")
        raise IdentityError("oidc_verifier_not_installed")
