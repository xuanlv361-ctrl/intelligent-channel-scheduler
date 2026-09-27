from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi.testclient import TestClient

from backend.security.csrf import SAFE_METHODS
from backend.security.http import CSRF_HEADER, SESSION_COOKIE, EnterpriseHTTPRuntime
from backend.security.identity import VerifiedIdentity
from backend.security.principal import PrincipalType


DEFAULT_TEST_ORIGIN = "http://127.0.0.1:5174"


def create_test_principal(runtime: EnterpriseHTTPRuntime, *, roles: tuple[str, ...]):
    """Issue a server-verified development principal with only requested roles."""
    now = datetime.now(timezone.utc)
    identity = VerifiedIdentity(
        principal_id=f"windows-dev:pytest-{uuid.uuid4().hex}",
        principal_type=PrincipalType.HUMAN,
        tenant_id=runtime.identity_config.default_development_tenant_id,
        workspace_id=runtime.identity_config.default_development_workspace_id,
        roles=roles,
        authn_method="windows_current_user_development_v1",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        session_id=None,
        token_id=f"pytest-{uuid.uuid4().hex}",
        is_development_identity=True,
        security_version=1,
    )
    return runtime.resolver.resolve_verified(identity)


class AuthenticatedTestClient(TestClient):
    """Test-only cookie session client that rotates CSRF for unsafe requests."""

    def __init__(self, app, *, runtime: EnterpriseHTTPRuntime,
                 roles: tuple[str, ...], origin: str = DEFAULT_TEST_ORIGIN,
                 **kwargs):
        super().__init__(app, **kwargs)
        if runtime.sessions is None or runtime.csrf is None:
            raise RuntimeError("enterprise_test_security_runtime_unavailable")
        principal = create_test_principal(runtime, roles=roles)
        token, validation = runtime.sessions.create(principal)
        self.cookies.set(SESSION_COOKIE, token, path="/api/v1")
        self._enterprise_runtime = runtime
        self._enterprise_token = token
        self._enterprise_origin = origin
        self._enterprise_validation = validation

    def _security_headers(self, method: str) -> dict[str, str]:
        if method.upper() in SAFE_METHODS:
            return {}
        validation = self._enterprise_runtime.sessions.validate(
            self._enterprise_token, touch=False)
        return {
            "Origin": self._enterprise_origin,
            CSRF_HEADER: self._enterprise_runtime.csrf.issue(validation),
        }

    def request(self, method: str, url, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        defaults = self._security_headers(method)
        for key, value in defaults.items():
            headers.setdefault(key, value)
        return super().request(method, url, headers=headers, **kwargs)

    def raw_request(self, method: str, url, **kwargs):
        """Bypass helper defaults so negative auth/CSRF cases stay explicit."""
        return super().request(method, url, **kwargs)


def authenticated_test_client(app, *, runtime: EnterpriseHTTPRuntime,
                              roles: tuple[str, ...], **kwargs) -> AuthenticatedTestClient:
    return AuthenticatedTestClient(app, runtime=runtime, roles=roles, **kwargs)
