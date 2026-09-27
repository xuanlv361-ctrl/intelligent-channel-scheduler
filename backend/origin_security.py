from __future__ import annotations

import os
import ipaddress
from urllib.parse import urlsplit

DEFAULT_DEVELOPMENT_ORIGINS = (
    "http://127.0.0.1:5174",
    "http://localhost:5174",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)
DEFAULT_LOCAL_API_HOSTS = (
    "127.0.0.1:5174",
    "localhost:5174",
    "127.0.0.1:8000",
    "localhost:8000",
)


def normalize_origin(value: str | None, *, referer: bool = False) -> str | None:
    if not value or value.strip().casefold() == "null":
        return None
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        if not referer and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname.casefold()
    default_port = 80 if parsed.scheme == "http" else 443
    authority = host if port in {None, default_port} else f"{host}:{port}"
    return f"{parsed.scheme}://{authority}"


def load_frontend_origins(raw: str | None = None) -> frozenset[str]:
    configured = raw if raw is not None else os.getenv(
        "UAT_FRONTEND_ORIGINS", ",".join(DEFAULT_DEVELOPMENT_ORIGINS))
    origins = set()
    for candidate in configured.split(","):
        normalized = normalize_origin(candidate)
        if normalized is None:
            raise ValueError("invalid_frontend_origin_configuration")
        origins.add(normalized)
    if not origins:
        raise ValueError("empty_frontend_origin_configuration")
    return frozenset(origins)


def normalize_host(value: str | None) -> str | None:
    if not value or any(character in value for character in "/?#@\\"):
        return None
    candidate = value.strip().casefold()
    if not candidate:
        return None
    try:
        parsed = urlsplit(f"http://{candidate}")
        port = parsed.port
    except ValueError:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost"} or port is None:
        return None
    return f"{parsed.hostname}:{port}"


def load_local_api_hosts(raw: str | None = None) -> frozenset[str]:
    configured = raw if raw is not None else os.getenv(
        "UAT_LOCAL_API_HOSTS", ",".join(DEFAULT_LOCAL_API_HOSTS))
    hosts = set()
    for candidate in configured.split(","):
        normalized = normalize_host(candidate)
        if normalized is None:
            raise ValueError("invalid_local_api_host_configuration")
        hosts.add(normalized)
    if not hosts:
        raise ValueError("empty_local_api_host_configuration")
    return frozenset(hosts)


def authorize_read_only_origin(
    origin: str | None,
    host: str | None,
    *,
    peer_host: str | None,
    referer: str | None = None,
    sec_fetch_site: str | None = None,
    allowed_origins: frozenset[str],
    allowed_local_hosts: frozenset[str],
) -> str | None:
    """Return the safe audit classification or ``None`` when rejected."""
    normalized_host = normalize_host(host)
    if normalized_host not in allowed_local_hosts:
        return None
    try:
        if not peer_host or not ipaddress.ip_address(peer_host).is_loopback:
            return None
    except ValueError:
        return None
    if origin is None:
        if (sec_fetch_site or "").strip().casefold() == "cross-site":
            return None
        if referer is not None and normalize_origin(
                referer, referer=True) not in allowed_origins:
            return None
        return "origin_absent_read_only"
    if not origin.strip():
        return None
    normalized_origin = normalize_origin(origin)
    if normalized_origin not in allowed_origins:
        return None
    return "approved_origin_read_only"


def detected_frontend_origin(origin: str | None, referer: str | None) -> str:
    normalized_origin = normalize_origin(origin)
    if normalized_origin:
        return normalized_origin
    normalized_referer = normalize_origin(referer, referer=True)
    if normalized_referer:
        return f"{normalized_referer}（仅由 Referer 检测，不能替代 Origin）"
    return "未检测到有效 Origin"
