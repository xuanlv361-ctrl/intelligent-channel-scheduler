"""FastAPI/ASGI integration for enterprise identity, RBAC, sessions and CSRF."""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3
import sys
from typing import Any, Callable, Iterable, Mapping, MutableMapping
import uuid

from fastapi import FastAPI, Request, Response
from fastapi.routing import APIRoute
from starlette.routing import Match

from backend.entry_security import IngressProfile
from .authorization import AuthorizationError, AuthorizationService
from .config import EnterpriseIdentityConfig
from .csrf import CSRFError, CSRFProtector, SAFE_METHODS
from .identity import (IdentityError, PrincipalResolver,
                       WindowsDevelopmentIdentityProvider)
from .key_material import KeyMaterialError, derive_key, load_http_master_key
from .principal import PrincipalContext
from .service_identity import ServiceIdentityError, ServiceIdentityManager
from .session import EnterpriseSessionManager, SessionError, SessionValidation


SESSION_COOKIE = "rqc_enterprise_session"
CSRF_HEADER = "X-CSRF-Token"
CORRELATION_HEADER = "X-Correlation-ID"
PUBLIC_API_PATHS = frozenset({
    "/api/v1/system/status",
    "/api/v1/system/readiness",
    "/api/v1/security/session/bootstrap",
})
SECURITY_API_PATHS = frozenset({
    "/api/v1/security/session",
    "/api/v1/security/session/csrf",
    "/api/v1/security/session/logout",
})
SENSITIVE_PERMISSIONS = frozenset({
    "collector.session.reauthenticate", "collector.oneshot.execute",
    "evidence.import", "snapshot.generate", "scheduler.execute",
    "price.sync", "circuit.manage", "exploration.manage",
    "kill_switch.activate", "kill_switch.release", "tenant.manage",
    "service_identity.manage",
    "uat.execution.manage", "uat.execute", "uat.capability.review",
})


class EnterpriseHTTPError(PermissionError):
    def __init__(self, code: str, status_code: int = 403):
        super().__init__(code)
        self.code, self.status_code = code, status_code


def _json_error(status: int, code: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    body = json.dumps({"detail": {"code": code}}, separators=(",", ":")).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
    ]
    return status, headers, body


def _header(scope: Mapping[str, Any], name: str) -> str | None:
    target = name.casefold().encode("latin-1")
    values = [value.decode("latin-1") for key, value in scope.get("headers", [])
              if key.lower() == target]
    if len(values) > 1:
        raise EnterpriseHTTPError("duplicate_security_header", 400)
    return values[0] if values else None


def _cookie(scope: Mapping[str, Any], name: str) -> str | None:
    raw = _header(scope, "cookie") or ""
    values: list[str] = []
    for item in raw.split(";"):
        key, separator, value = item.strip().partition("=")
        if separator and key == name:
            values.append(value)
    if len(values) > 1:
        raise EnterpriseHTTPError("duplicate_session_cookie", 400)
    return values[0] if values else None


def _state(scope: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    state = scope.setdefault("state", {})
    if not isinstance(state, MutableMapping):
        raise EnterpriseHTTPError("request_state_unavailable", 500)
    return state


def _normalized_origin(value: str | None) -> str | None:
    return None if value is None else value.strip().casefold().rstrip("/")


def _permission_for(method: str, path: str) -> str:
    """Concrete action mapping for every non-public API route."""
    method, path = method.upper(), path.casefold()
    if path.startswith("/api/v1/security/session"):
        return ""
    if "kill-switch" in path:
        return "kill_switch.release" if "release" in path else "kill_switch.activate"
    if "circuit-breaker" in path:
        return "circuit.manage" if method not in SAFE_METHODS else "circuit.read"
    if ("exploration" in path or "probe-governance" in path or
            "high-cost-test" in path or path.startswith("/api/v1/probes")):
        return "exploration.manage" if method not in SAFE_METHODS else "exploration.read"
    if "price" in path:
        return "price.sync" if method not in SAFE_METHODS else "price.read"
    if "metrics/refresh" in path:
        return "snapshot.generate"
    if "metrics" in path or "statistical-confidence" in path:
        return "snapshot.read"
    if "scheduler" in path or "replay" in path or "config-review" in path:
        return "scheduler.decide" if method not in SAFE_METHODS else "decision.read"
    if "/uat/execution-control" in path:
        return "uat.execution.manage" if method not in SAFE_METHODS else "decision.read"
    if path.startswith("/api/v1/environments/") and path.endswith("/execution-state"):
        return "uat.execution.manage" if method not in SAFE_METHODS else "decision.read"
    if "/uat/output-capabilities" in path:
        return "uat.capability.review" if method not in SAFE_METHODS else "evidence.read"
    if "/request-workbench/" in path:
        # Workbench validation is part of the UAT execution workflow, not an
        # evidence import.  The service still performs its own fail-closed
        # environment, credential, target and capability checks.
        return "uat.execute"
    if method not in SAFE_METHODS and (
            path == "/api/v1/uat/execute" or
            (path.startswith("/api/v1/environments/") and path.endswith("/execute"))):
        return "uat.execute"
    if "collector" in path or "log-sync" in path:
        if method in SAFE_METHODS:
            return "collector.session.read"
        if any(part in path for part in (
                "reauth", "pairing", "credential", "confirm-login",
                "/persistent/browser")):
            return "collector.session.reauthenticate"
        return "collector.oneshot.execute"
    if "credential" in path or path.endswith("/models") or "/models?" in path:
        return ("collector.session.reauthenticate" if method not in SAFE_METHODS
                else "collector.session.read")
    if "audit" in path or "dependencies" in path or "formal-agent-skills" in path:
        return "audit.read"
    if "import" in path:
        return "evidence.import" if method not in SAFE_METHODS else "evidence.read"
    if "export" in path:
        return "evidence.read"
    if method not in SAFE_METHODS:
        return "evidence.import"
    return "evidence.read"


def permission_for_scope(scope: Mapping[str, Any]) -> str:
    method = str(scope.get("method") or "GET")
    path = str(scope.get("path") or "/")
    return _permission_for(method, path) or f"security:{method.upper()}:{path}"


def _iter_api_routes(container: Any):
    """Yield concrete routes through FastAPI's lazy included-router wrappers."""
    for route in getattr(container, "routes", ()):
        if isinstance(route, APIRoute):
            yield route
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_api_routes(original)


def annotate_route_permissions(app: FastAPI) -> None:
    """Attach auditable permission metadata and reject unmapped API routes."""
    for route in _iter_api_routes(app):
        if not route.path.startswith("/api/v1/"):
            continue
        if route.path in PUBLIC_API_PATHS or route.path in SECURITY_API_PATHS:
            setattr(route.endpoint, "__enterprise_public__", True)
            route.openapi_extra = {**(route.openapi_extra or {}), "x-enterprise-public": True}
            continue
        permissions = {
            _permission_for(method, route.path) for method in route.methods or {"GET"}
        }
        permissions.discard("")
        if len(permissions) != 1:
            raise EnterpriseHTTPError("route_permission_mapping_ambiguous", 500)
        permission = permissions.pop()
        setattr(route.endpoint, "__enterprise_permission__", permission)
        route.openapi_extra = {**(route.openapi_extra or {}),
                               "x-required-permission": permission}


@dataclass(slots=True)
class EnterpriseHTTPRuntime:
    profile: IngressProfile
    identity_config: EnterpriseIdentityConfig
    authorization: AuthorizationService
    resolver: PrincipalResolver
    sessions: EnterpriseSessionManager | None
    csrf: CSRFProtector | None
    service_identities: ServiceIdentityManager | None
    database_path: Path
    unavailable_reason: str | None = None

    @classmethod
    def build(cls, *, profile: IngressProfile, database_path: str | Path,
              root: str | Path, environ: dict[str, str] | None = None) -> "EnterpriseHTTPRuntime":
        root_path = Path(root)
        config = EnterpriseIdentityConfig.load(root_path / "config/enterprise_identity_v1.json")
        # Ingress deployment selection is authoritative for the HTTP boundary.
        if config.deployment_mode != profile.name:
            config = replace(
                config, deployment_mode=profile.name,
                development_identity_enabled=(profile.name == "development" and
                                              config.development_identity_enabled))
        authorization = AuthorizationService(root_path / "config/rbac_permissions_v1.json")
        resolver = PrincipalResolver(config, authorization)
        try:
            master = load_http_master_key(
                deployment_mode=profile.name,
                protected_path=root_path / ".runtime/enterprise_http_key.dpapi",
                environ=environ)
            sessions = EnterpriseSessionManager(
                database_path, signing_key=derive_key(master, "session-v1"),
                resolver=resolver, clock_skew_seconds=config.oidc.clock_skew_seconds)
            csrf = CSRFProtector(derive_key(master, "csrf-v1"), sessions)
            service_identities = ServiceIdentityManager(
                database_path, root_path / "config/service_identities_v1.json",
                signing_key=derive_key(master, "service-v1"), resolver=resolver)
            unavailable = None
        except KeyMaterialError as exc:
            sessions = csrf = service_identities = None
            unavailable = str(exc)
        return cls(profile, config, authorization, resolver, sessions, csrf,
                   service_identities, Path(database_path), unavailable)

    def audit_action(self, principal: PrincipalContext, action: str, result: str,
                     reason: str) -> None:
        if action not in SENSITIVE_PERMISSIONS:
            return
        details = json.dumps({
            "action": action, "result": str(result)[:64],
            "reason": str(reason)[:256],
            "correlation_id": principal.correlation_id,
            "principal_type": principal.principal_type.value,
        }, sort_keys=True, separators=(",", ":"))
        try:
            with sqlite3.connect(self.database_path) as db:
                db.execute("""INSERT INTO enterprise_session_audit(
                  audit_id,session_id,tenant_id,workspace_id,principal_id,
                  event_type,created_at,details_json) VALUES(?,?,?,?,?,?,?,?)""", (
                    f"HSA-{uuid.uuid4().hex}", principal.session_id,
                    principal.tenant_id, principal.workspace_id,
                    principal.principal_id, "high_risk_http_action",
                    datetime.now(timezone.utc).isoformat(), details))
        except sqlite3.Error as exc:
            raise EnterpriseHTTPError("security_audit_unavailable", 503) from exc

    @property
    def ready(self) -> bool:
        return self.sessions is not None and self.csrf is not None

    def bootstrap_development(self, request: Request) -> tuple[str, SessionValidation, str]:
        if not self.ready:
            raise EnterpriseHTTPError(self.unavailable_reason or "security_runtime_unavailable", 503)
        ingress = getattr(request.state, "enterprise_ingress", None) or {}
        client_ip = str(ingress.get("client_ip") or
                        (request.client.host if request.client else ""))
        try:
            loopback = ipaddress.ip_address(client_ip).is_loopback
        except ValueError:
            loopback = False
        origin = _normalized_origin(request.headers.get("origin"))
        if (self.profile.name != "development" or not loopback or
                origin not in self.profile.allowed_origins or sys.platform != "win32"):
            raise EnterpriseHTTPError("development_identity_loopback_origin_required", 403)
        provider = WindowsDevelopmentIdentityProvider(self.identity_config)
        try:
            principal = self.resolver.resolve(provider)
            token, validation = self.sessions.create(principal)  # type: ignore[union-attr]
            return token, validation, self.csrf.issue(validation)  # type: ignore[union-attr]
        except (IdentityError, SessionError) as exc:
            raise EnterpriseHTTPError(str(exc), 403) from exc

    def service_principal(self, human: PrincipalContext, service_name: str,
                          audience: str) -> PrincipalContext:
        """Mint and validate a short-lived, same-scope internal service identity."""
        if self.service_identities is None:
            raise EnterpriseHTTPError(
                self.unavailable_reason or "service_identity_pending_external_configuration", 503)
        token, _metadata = self.service_identities.issue(
            service_name, human.tenant_id, human.workspace_id, audience,
            ttl_seconds=60)
        try:
            return self.service_identities.validate(
                token, audience, request_nonce=secrets.token_urlsafe(24),
                correlation_id=human.correlation_id)
        except ServiceIdentityError as exc:
            raise EnterpriseHTTPError(str(exc), 403) from exc


class PrincipalPreResolutionMiddleware:
    """Resolve only opaque server-side session state before scoped ingress limits."""
    def __init__(self, app: Callable[..., Any], runtime: EnterpriseHTTPRuntime):
        self.app, self.runtime = app, runtime

    async def __call__(self, scope: MutableMapping[str, Any], receive, send) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return
        permission: str | None = None
        try:
            token = _cookie(scope, SESSION_COOKIE)
            if token and self.runtime.sessions is not None:
                correlation = _header(scope, CORRELATION_HEADER)
                validation = self.runtime.sessions.validate(
                    token, correlation_id=correlation, touch=False)
                state = _state(scope)
                state["principal_context"] = validation.principal
                state["enterprise_session"] = validation
        except (EnterpriseHTTPError, SessionError, IdentityError, AuthorizationError) as exc:
            _state(scope)["enterprise_authentication_error"] = str(exc)
        await self.app(scope, receive, send)


class EnterpriseHTTPAuthorizationMiddleware:
    """Default-deny authentication, route RBAC and session-bound CSRF."""
    def __init__(self, app: Callable[..., Any], *, runtime: EnterpriseHTTPRuntime,
                 route_app: FastAPI):
        self.app, self.runtime, self.route_app = app, runtime, route_app

    def _route(self, scope: MutableMapping[str, Any]) -> APIRoute | None:
        for route in _iter_api_routes(self.route_app):
            if route.path.startswith("/api/v1/") and route.matches(scope)[0] is Match.FULL:
                return route
        return None

    async def _deny(self, scope: Mapping[str, Any], send,
                    status: int, code: str) -> None:
        status, headers, body = _json_error(status, code)
        origin = _normalized_origin(_header(scope, "origin"))
        if origin is not None and origin in self.runtime.profile.allowed_origins:
            headers.extend([
                (b"access-control-allow-origin", origin.encode("latin-1")),
                (b"access-control-allow-credentials", b"true"),
                (b"vary", b"Origin"),
            ])
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: MutableMapping[str, Any], receive, send) -> None:
        path = str(scope.get("path") or "")
        if scope.get("type") != "http" or not path.startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return
        route = self._route(scope)
        if route is None:
            await self.app(scope, receive, send)
            return
        if path in PUBLIC_API_PATHS:
            await self.app(scope, receive, send)
            return
        state = _state(scope)
        authentication_error = state.get("enterprise_authentication_error")
        validation = state.get("enterprise_session")
        if authentication_error or not isinstance(validation, SessionValidation):
            await self._deny(
                scope, send, 401,
                str(authentication_error or "authentication_required"))
            return
        try:
            # Touch after ingress validation and revalidate revocation/version state.
            token = _cookie(scope, SESSION_COOKIE)
            validation = self.runtime.sessions.validate(  # type: ignore[union-attr]
                token or "", correlation_id=validation.principal.correlation_id)
            state["principal_context"] = validation.principal
            state["enterprise_session"] = validation
            permission = getattr(route.endpoint, "__enterprise_permission__", None)
            if path not in SECURITY_API_PATHS:
                if not permission:
                    raise EnterpriseHTTPError("route_permission_metadata_missing", 500)
                self.runtime.authorization.authorize(validation.principal, permission)
            if str(scope.get("method") or "GET").upper() not in SAFE_METHODS:
                origin = _normalized_origin(_header(scope, "origin"))
                csrf_token = _header(scope, CSRF_HEADER)
                self.runtime.csrf.verify(  # type: ignore[union-attr]
                    csrf_token, validation, method=str(scope.get("method")),
                    origin=origin, allowed_origins=self.runtime.profile.allowed_origins)
            if permission in SENSITIVE_PERMISSIONS:
                # Durable preflight attribution is a gate: a high-risk action
                # never reaches the handler if its audit boundary is unavailable.
                self.runtime.audit_action(
                    validation.principal, permission, "authorized",
                    "security_gates_passed")
        except (EnterpriseHTTPError, SessionError, IdentityError,
                AuthorizationError, CSRFError) as exc:
            if (isinstance(validation, SessionValidation) and permission in
                    SENSITIVE_PERMISSIONS and
                    str(exc) != "security_audit_unavailable"):
                try:
                    self.runtime.audit_action(
                        validation.principal, permission, "denied", str(exc))
                except EnterpriseHTTPError:
                    exc = EnterpriseHTTPError("security_audit_unavailable", 503)
            status = getattr(exc, "status_code", 403)
            if isinstance(exc, SessionError):
                status = 401
            await self._deny(scope, send, status, str(exc))
            return

        next_csrf = (self.runtime.csrf.issue(validation)
                     if str(scope.get("method") or "GET").upper() not in SAFE_METHODS
                     else None)  # type: ignore[union-attr]

        audited = False
        async def send_with_security(message: MutableMapping[str, Any]) -> None:
            nonlocal audited
            if message.get("type") == "http.response.start" and next_csrf:
                headers = list(message.get("headers", []))
                headers.append((CSRF_HEADER.casefold().encode(), next_csrf.encode()))
                headers.append((b"access-control-expose-headers", CSRF_HEADER.encode()))
                message["headers"] = headers
            if (message.get("type") == "http.response.start" and not audited and
                    permission in SENSITIVE_PERMISSIONS):
                audited = True
                status = int(message.get("status") or 500)
                self.runtime.audit_action(
                    validation.principal, permission,
                    "succeeded" if status < 400 else "failed",
                    f"http_status_{status}")
            await send(message)

        await self.app(scope, receive, send_with_security)


def set_session_cookie(response: Response, token: str, profile: IngressProfile) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True,
        secure=profile.name in {"lan", "production"}, samesite="strict",
        path="/api/v1", max_age=8 * 60 * 60)


def clear_session_cookie(response: Response, profile: IngressProfile) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/api/v1",
                           secure=profile.name in {"lan", "production"},
                           httponly=True, samesite="strict")


def request_principal(request: Request) -> PrincipalContext:
    principal = getattr(request.state, "principal_context", None)
    if not isinstance(principal, PrincipalContext) or not principal.is_verified:
        raise EnterpriseHTTPError("principal_context_missing_or_unverified", 403)
    return principal
