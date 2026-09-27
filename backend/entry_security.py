"""Enterprise ingress policy and pure ASGI enforcement middleware."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, MutableMapping
from urllib.parse import urlsplit

from backend.rate_limiting import (
    ConcurrencyLimiter, RateLimitScope, SlidingWindowRateLimiter,
)
from backend.trusted_proxy import (
    ForwardedContext, TrustedProxyError, normalize_host,
    parse_trusted_cidrs, resolve_forwarded_context,
)


POLICY_SCHEMA_VERSION = "enterprise_ingress_policy_v1"
DEPLOYMENT_PROFILES = frozenset({"development", "lan", "production"})
TLS_MODES = frozenset({"disabled", "direct", "trusted_proxy"})
_ORIGIN = re.compile(r"^https?://[^/?#]+$")


class EntrySecurityError(ValueError):
    def __init__(self, code: str, *, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class IngressLimits:
    maximum_request_body_bytes: int
    maximum_header_count: int
    maximum_header_bytes: int
    request_timeout_seconds: float
    global_concurrency: int
    scoped_concurrency: int
    global_requests_per_window: int
    scoped_requests_per_window: int
    sensitive_requests_per_window: int
    rate_window_seconds: float


@dataclass(frozen=True)
class IngressProfile:
    name: str
    bind_host: str
    tls_mode: str
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    trusted_proxy_cidrs: tuple[ipaddress._BaseNetwork, ...]
    reject_untrusted_forwarding: bool
    limits: IngressLimits

    @property
    def external_configuration_status(self) -> str:
        return "pending_external_configuration" if self.name in {"lan", "production"} else "local_verified"


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _origin(value: str, *, production: bool) -> str:
    normalized = value.strip().casefold().rstrip("/")
    if (not _ORIGIN.fullmatch(normalized) or "*" in normalized
            or "@" in normalized or normalized == "null"):
        raise EntrySecurityError("invalid_allowed_origin_configuration")
    if production and not normalized.startswith("https://"):
        raise EntrySecurityError("production_origin_requires_https")
    return normalized


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool):
        raise EntrySecurityError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise EntrySecurityError(code) from exc
    if parsed < 1:
        raise EntrySecurityError(code)
    return parsed


def _positive_float(value: Any, code: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise EntrySecurityError(code) from exc
    if parsed <= 0:
        raise EntrySecurityError(code)
    return parsed


def build_ingress_profile(name: str, raw: Mapping[str, Any]) -> IngressProfile:
    if name not in DEPLOYMENT_PROFILES:
        raise EntrySecurityError("invalid_deployment_profile")
    tls_mode = str(raw.get("tls_mode") or "").strip().casefold()
    if tls_mode not in TLS_MODES:
        raise EntrySecurityError("invalid_tls_mode")
    bind_host = str(raw.get("bind_host") or "").strip()
    try:
        bind_ip = ipaddress.ip_address(bind_host)
    except ValueError:
        bind_ip = None
        if not bind_host or any(char in bind_host for char in "/?#@"):
            raise EntrySecurityError("invalid_bind_host")

    raw_hosts = raw.get("allowed_hosts") or []
    hosts = frozenset(normalize_host(str(item)) for item in raw_hosts)
    origins = frozenset(
        _origin(str(item), production=name in {"lan", "production"})
        for item in (raw.get("allowed_origins") or []))
    try:
        trusted = parse_trusted_cidrs(raw.get("trusted_proxy_cidrs") or [])
    except TrustedProxyError as exc:
        raise EntrySecurityError(str(exc)) from exc
    limits_raw = raw.get("limits")
    if not isinstance(limits_raw, Mapping):
        raise EntrySecurityError("ingress_limits_required")
    limits = IngressLimits(
        maximum_request_body_bytes=_positive_int(
            limits_raw.get("maximum_request_body_bytes"), "invalid_body_limit"),
        maximum_header_count=_positive_int(
            limits_raw.get("maximum_header_count"), "invalid_header_count_limit"),
        maximum_header_bytes=_positive_int(
            limits_raw.get("maximum_header_bytes"), "invalid_header_size_limit"),
        request_timeout_seconds=_positive_float(
            limits_raw.get("request_timeout_seconds"), "invalid_request_timeout"),
        global_concurrency=_positive_int(
            limits_raw.get("global_concurrency"), "invalid_global_concurrency"),
        scoped_concurrency=_positive_int(
            limits_raw.get("scoped_concurrency"), "invalid_scoped_concurrency"),
        global_requests_per_window=_positive_int(
            limits_raw.get("global_requests_per_window"), "invalid_global_rate_limit"),
        scoped_requests_per_window=_positive_int(
            limits_raw.get("scoped_requests_per_window"), "invalid_scoped_rate_limit"),
        sensitive_requests_per_window=_positive_int(
            limits_raw.get("sensitive_requests_per_window"), "invalid_sensitive_rate_limit"),
        rate_window_seconds=_positive_float(
            limits_raw.get("rate_window_seconds"), "invalid_rate_window"),
    )
    if limits.scoped_concurrency > limits.global_concurrency:
        raise EntrySecurityError("scoped_concurrency_exceeds_global")
    if limits.sensitive_requests_per_window > limits.scoped_requests_per_window:
        raise EntrySecurityError("sensitive_rate_exceeds_scoped_rate")

    reject = raw.get("reject_untrusted_forwarding", True)
    if not isinstance(reject, bool):
        raise EntrySecurityError("invalid_forwarding_rejection_policy")
    if name == "development":
        if bind_ip is None or not bind_ip.is_loopback:
            raise EntrySecurityError("development_bind_must_be_loopback")
    if name in {"lan", "production"}:
        if tls_mode == "disabled":
            raise EntrySecurityError("external_tls_required")
        if not hosts or not origins:
            raise EntrySecurityError("external_host_and_origin_allowlists_required")
        if tls_mode == "trusted_proxy" and not trusted:
            raise EntrySecurityError("external_trusted_proxy_required")
        if not reject:
            raise EntrySecurityError("external_untrusted_forwarding_must_fail_closed")
    if tls_mode == "trusted_proxy" and not trusted:
        raise EntrySecurityError("trusted_proxy_cidr_required")
    return IngressProfile(
        name=name, bind_host=bind_host, tls_mode=tls_mode,
        allowed_hosts=hosts, allowed_origins=origins,
        trusted_proxy_cidrs=trusted, reject_untrusted_forwarding=reject,
        limits=limits,
    )


def load_ingress_profile(
    policy_path: Path | str = Path(__file__).resolve().parents[1]
    / "config" / "enterprise_ingress_policy_v1.json",
    *, environ: Mapping[str, str] | None = None,
) -> IngressProfile:
    environment = os.environ if environ is None else environ
    raw_policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    if raw_policy.get("schema_version") != POLICY_SCHEMA_VERSION:
        raise EntrySecurityError("unsupported_ingress_policy_schema")
    selected = environment.get(
        "ROUTING_CONSOLE_DEPLOYMENT_PROFILE",
        str(raw_policy.get("default_profile") or "development"),
    ).strip().casefold()
    profiles = raw_policy.get("profiles")
    if not isinstance(profiles, Mapping) or not isinstance(profiles.get(selected), Mapping):
        raise EntrySecurityError("deployment_profile_not_configured")
    profile = dict(profiles[selected])
    overrides: tuple[tuple[str, str, Callable[[str], Any]], ...] = (
        ("ROUTING_CONSOLE_BIND_HOST", "bind_host", str),
        ("ROUTING_CONSOLE_TLS_MODE", "tls_mode", str),
        ("ROUTING_CONSOLE_ALLOWED_HOSTS", "allowed_hosts", _csv),
        ("ROUTING_CONSOLE_ALLOWED_ORIGINS", "allowed_origins", _csv),
        ("ROUTING_CONSOLE_TRUSTED_PROXY_CIDRS", "trusted_proxy_cidrs", _csv),
    )
    for variable, field, converter in overrides:
        if variable in environment:
            profile[field] = converter(environment[variable])
    return build_ingress_profile(selected, profile)


def readiness(profile: IngressProfile) -> dict[str, Any]:
    return {
        "status": "ready",
        "profile": profile.name,
        "tls_mode": profile.tls_mode,
        "trusted_proxy_configured": bool(profile.trusted_proxy_cidrs),
        "allowed_host_count": len(profile.allowed_hosts),
        "allowed_origin_count": len(profile.allowed_origins),
        "external_dns": "pending_external_configuration",
        "production_certificate": "pending_external_configuration",
        "production_firewall": "pending_external_configuration",
        "external_secret_manager": "pending_external_configuration",
    }


def _scope_state(scope: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    state = scope.setdefault("state", {})
    return state if isinstance(state, MutableMapping) else {}


def _principal_value(principal: Any, field: str, default: str = "") -> str:
    if isinstance(principal, Mapping):
        return str(principal.get(field) or default)
    return str(getattr(principal, field, default) or default)


def rate_limit_scope(scope: MutableMapping[str, Any], client_ip: str,
                     action: str) -> RateLimitScope:
    principal = _scope_state(scope).get("principal_context")
    principal_type = _principal_value(principal, "principal_type")
    if principal_type not in {"human", "service"}:
        return RateLimitScope("", "", "", "anonymous", action,
                              anonymous_ip=client_ip)
    return RateLimitScope(
        tenant_id=_principal_value(principal, "tenant_id"),
        workspace_id=_principal_value(principal, "workspace_id"),
        principal_id=_principal_value(principal, "principal_id"),
        principal_type=principal_type,
        service_id=(_principal_value(principal, "principal_id")
                    if principal_type == "service" else ""),
        action=action,
    )


def default_action(scope: Mapping[str, Any]) -> str:
    method = str(scope.get("method") or "GET").upper()
    path = str(scope.get("path") or "/")
    return f"{method}:{path}"


class _BodyTooLarge(Exception):
    pass


class EnterpriseIngressMiddleware:
    """ASGI middleware for ingress validation, limits and safe headers."""

    def __init__(self, app: Callable[..., Any], profile: IngressProfile, *,
                 clock: Callable[[], float] = time.monotonic,
                 action_resolver: Callable[[Mapping[str, Any]], str] = default_action,
                 sensitive_actions: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.profile = profile
        self.action_resolver = action_resolver
        self.sensitive_actions = sensitive_actions
        limits = profile.limits
        self.global_rate = SlidingWindowRateLimiter(
            capacity=limits.global_requests_per_window,
            window_seconds=limits.rate_window_seconds, clock=clock)
        self.scoped_rate = SlidingWindowRateLimiter(
            capacity=limits.scoped_requests_per_window,
            window_seconds=limits.rate_window_seconds, clock=clock)
        self.sensitive_rate = SlidingWindowRateLimiter(
            capacity=limits.sensitive_requests_per_window,
            window_seconds=limits.rate_window_seconds, clock=clock)
        self.concurrency = ConcurrencyLimiter(
            global_limit=limits.global_concurrency,
            per_scope_limit=limits.scoped_concurrency)

    @staticmethod
    def _headers(scope: Mapping[str, Any]) -> list[tuple[str, str]]:
        return [(name.decode("latin-1").casefold(), value.decode("latin-1"))
                for name, value in scope.get("headers", [])]

    async def _error(self, send: Callable[..., Any], status: int, code: str,
                     *, retry_after: float | None = None,
                     detail: Mapping[str, Any] | None = None) -> None:
        safe_detail: dict[str, Any] = {"code": code}
        if detail:
            safe_detail.update(detail)
        body = json.dumps(
            {"detail": safe_detail}, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode()),
                   (b"cache-control", b"no-store"),
                   (b"x-content-type-options", b"nosniff"),
                   (b"x-frame-options", b"DENY"),
                   (b"referrer-policy", b"no-referrer"),
                   (b"content-security-policy",
                    b"default-src 'none'; frame-ancestors 'none'")]
        if retry_after is not None:
            headers.append((b"retry-after", str(max(1, int(retry_after + .999))).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: MutableMapping[str, Any], receive: Callable[..., Any],
                       send: Callable[..., Any]) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers_list = self._headers(scope)
        limits = self.profile.limits
        if len(headers_list) > limits.maximum_header_count:
            await self._error(send, 431, "request_header_count_exceeded")
            return
        if sum(len(name) + len(value) for name, value in headers_list) > limits.maximum_header_bytes:
            await self._error(send, 431, "request_header_size_exceeded")
            return
        headers: dict[str, str] = {}
        for name, value in headers_list:
            if name in headers and name in {"host", "origin"}:
                await self._error(send, 400, "duplicate_security_header")
                return
            headers[name] = value
        peer = str((scope.get("client") or ("", 0))[0])
        in_process_development_client = (
            self.profile.name == "development" and peer == "testclient")
        direct_host = headers.get("host", "")
        # Starlette's in-process TestClient uses the non-address sentinel
        # ``testclient``.  Treat it as loopback only in the explicit
        # development profile; LAN/production continue to reject every
        # non-IP peer fail-closed.
        if in_process_development_client:
            peer = "127.0.0.1"
            if direct_host.casefold() in {"testserver", "testclient"}:
                direct_host = "127.0.0.1:8000"
        try:
            context = resolve_forwarded_context(
                peer_ip=peer, direct_scheme=str(scope.get("scheme") or "http"),
                direct_host=direct_host, headers=headers_list,
                trusted_cidrs=self.profile.trusted_proxy_cidrs,
                reject_untrusted_forwarding=self.profile.reject_untrusted_forwarding)
        except TrustedProxyError as exc:
            await self._error(send, 400, str(exc))
            return
        if context.host not in self.profile.allowed_hosts:
            await self._error(send, 400, "host_not_allowed")
            return
        origin = headers.get("origin")
        if origin is not None and origin.strip().casefold().rstrip("/") not in self.profile.allowed_origins:
            try:
                normalized_origin = _origin(origin, production=False)
                parsed_origin = urlsplit(normalized_origin)
                detected = (
                    normalized_origin
                    if parsed_origin.scheme == "http"
                    and parsed_origin.hostname in {"127.0.0.1", "localhost"}
                    else "非本地或未允许的 Origin"
                )
            except EntrySecurityError:
                detected = "无效或未允许的 Origin"
            allowed = sorted(self.profile.allowed_origins)
            await self._error(
                send, 403, "ORIGIN_REJECTED",
                detail={
                    "message": (
                        f"前端来源不受允许。当前检测来源：{detected}。"
                        f"允许的本地开发来源：{'、'.join(allowed)}。"
                    ),
                    "detected_origin": detected,
                    "allowed_origins": allowed,
                })
            return
        if self.profile.tls_mode != "disabled" and context.scheme != "https":
            await self._error(send, 400, "tls_required")
            return
        if self.profile.tls_mode == "trusted_proxy" and not context.forwarded:
            await self._error(send, 400, "trusted_proxy_tls_evidence_required")
            return

        action = self.action_resolver(scope)
        is_api_request = str(scope.get("path") or "").startswith("/api/")
        try:
            scoped = rate_limit_scope(scope, context.client_ip, action)
        except ValueError:
            await self._error(send, 403, "rate_limit_identity_scope_invalid")
            return
        global_decision = self.global_rate.check("global")
        # Browser documents and immutable static assets remain covered by the
        # global limiter and concurrency gate.  The tenant/principal quota is
        # reserved for API traffic so ordinary SPA navigation cannot consume
        # the caller's protected-operation budget.
        scoped_decision = self.scoped_rate.consume(scoped) if is_api_request else None
        sensitive_decision = (self.sensitive_rate.consume(scoped)
                              if is_api_request and action in self.sensitive_actions else None)
        denied = next((item for item in (
            global_decision, scoped_decision, sensitive_decision)
            if item is not None and not item.allowed), None)
        if denied is not None:
            await self._error(send, 429, "rate_limit_exceeded",
                              retry_after=denied.retry_after_seconds)
            return
        lease = self.concurrency.acquire(scoped.key)
        if lease is None:
            await self._error(send, 503, "concurrency_limit_exceeded", retry_after=1)
            return
        state = _scope_state(scope)
        state["enterprise_ingress"] = {
            "scheme": context.scheme, "host": context.host,
            "forwarded": context.forwarded,
            # Ephemeral only.  The ingress module has no persistence/log sink.
            "client_ip": context.client_ip,
        }
        response_started = False
        async def secure_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
                response_headers = list(message.get("headers", []))
                existing = {name.decode("latin-1").casefold() for name, _ in response_headers}
                response_content_type = next((
                    value.decode("latin-1").casefold()
                    for name, value in response_headers
                    if name.decode("latin-1").casefold() == "content-type"
                ), "")
                is_frontend_document = response_content_type.startswith("text/html")
                additions = {
                    "x-content-type-options": "nosniff",
                    "x-frame-options": "DENY",
                    "referrer-policy": "no-referrer",
                    "permissions-policy": "camera=(), microphone=(), geolocation=()",
                    "content-security-policy": (
                        "default-src 'none'; frame-ancestors 'none'"
                        if not is_frontend_document and (
                            str(scope.get("path") or "").startswith("/api/")
                            or str(scope.get("path") or "") in {"/health","/ready","/runtime-config.json"}
                        )
                        else "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
                             "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                             "form-action 'self'"
                    ),
                    "cache-control": "no-store",
                }
                if context.scheme == "https":
                    additions["strict-transport-security"] = "max-age=31536000; includeSubDomains"
                response_headers.extend(
                    (name.encode(), value.encode()) for name, value in additions.items()
                    if name not in existing)
                message = {**message, "headers": response_headers}
            await send(message)

        async def run_request() -> None:
            buffered: list[dict[str, Any]] = []
            total = 0
            while True:
                message = await receive()
                buffered.append(message)
                if message.get("type") == "http.disconnect":
                    return
                if message.get("type") != "http.request":
                    continue
                total += len(message.get("body", b""))
                if total > limits.maximum_request_body_bytes:
                    raise _BodyTooLarge
                if not message.get("more_body", False):
                    break
            index = 0

            async def replay_receive() -> dict[str, Any]:
                nonlocal index
                if index < len(buffered):
                    message = buffered[index]
                    index += 1
                    return message
                # StreamingResponse keeps listening for a real client disconnect
                # after the request body has been replayed. Returning a synthetic
                # disconnect here cancelled every streamed response immediately.
                return await receive()

            await self.app(scope, replay_receive, secure_send)

        try:
            await asyncio.wait_for(
                run_request(),
                timeout=limits.request_timeout_seconds)
        except _BodyTooLarge:
            if not response_started:
                await self._error(send, 413, "request_body_too_large")
        except asyncio.TimeoutError:
            if not response_started:
                await self._error(send, 504, "request_timeout")
        finally:
            lease.release()
