"""Strict trusted-proxy parsing for enterprise ingress.

Forwarding headers are security-sensitive inputs.  This module deliberately
does not consult environment variables and never logs or persists client IPs.
Callers must supply the reviewed CIDR set from the ingress policy.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit


FORWARDING_HEADERS = frozenset({
    "forwarded", "x-forwarded-for", "x-forwarded-host",
    "x-forwarded-proto", "x-forwarded-port",
})
_TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


class TrustedProxyError(ValueError):
    """A forwarding header or trusted-proxy configuration is unsafe."""


@dataclass(frozen=True)
class ForwardedContext:
    client_ip: str
    scheme: str
    host: str
    peer_ip: str
    forwarded: bool


def parse_trusted_cidrs(values: Iterable[str]) -> tuple[ipaddress._BaseNetwork, ...]:
    networks: list[ipaddress._BaseNetwork] = []
    for raw in values:
        candidate = str(raw).strip()
        if not candidate:
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=True)
        except ValueError as exc:
            raise TrustedProxyError("invalid_trusted_proxy_cidr") from exc
        networks.append(network)
    return tuple(networks)


def is_trusted_proxy(peer_ip: str, networks: Sequence[ipaddress._BaseNetwork]) -> bool:
    try:
        address = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(address.version == network.version and address in network
               for network in networks)


def normalize_host(value: str) -> str:
    candidate = value.strip().casefold()
    if (not candidate or any(char in candidate for char in "/?#@\\")
            or any(char.isspace() for char in candidate)):
        raise TrustedProxyError("invalid_forwarded_host")
    try:
        parsed = urlsplit(f"//{candidate}")
        port = parsed.port
    except ValueError as exc:
        raise TrustedProxyError("invalid_forwarded_host") from exc
    if not parsed.hostname or parsed.username or parsed.password:
        raise TrustedProxyError("invalid_forwarded_host")
    hostname = parsed.hostname.casefold()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    return hostname if port is None else f"{hostname}:{port}"


def _header_map(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    source = headers.items() if isinstance(headers, Mapping) else headers
    result: dict[str, str] = {}
    for name, value in source:
        key = str(name).strip().casefold()
        if key in result and key in FORWARDING_HEADERS:
            raise TrustedProxyError("duplicate_forwarding_header")
        result[key] = str(value).strip()
    return result


def _parse_ip(value: str) -> str:
    candidate = value.strip().strip('"')
    if candidate.startswith("["):
        end = candidate.find("]")
        if end < 0:
            raise TrustedProxyError("invalid_forwarded_for")
        candidate = candidate[1:end]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError as exc:
        raise TrustedProxyError("invalid_forwarded_for") from exc


def _forwarded_first(value: str) -> dict[str, str]:
    # RFC 7239 permits a chain.  The first element describes the original
    # client as supplied by the trusted edge proxy.
    element = value.split(",", 1)[0].strip()
    if not element:
        raise TrustedProxyError("invalid_forwarded_header")
    parsed: dict[str, str] = {}
    for item in element.split(";"):
        if "=" not in item:
            raise TrustedProxyError("invalid_forwarded_header")
        key, raw = item.split("=", 1)
        key = key.strip().casefold()
        raw = raw.strip()
        if not _TOKEN.fullmatch(key) or key in parsed or not raw:
            raise TrustedProxyError("invalid_forwarded_header")
        if raw.startswith('"') != raw.endswith('"'):
            raise TrustedProxyError("invalid_forwarded_header")
        parsed[key] = raw[1:-1] if raw.startswith('"') else raw
    return parsed


def _select_client_ip(chain: list[str], peer_ip: str,
                      networks: Sequence[ipaddress._BaseNetwork]) -> str:
    addresses = [_parse_ip(item) for item in chain]
    addresses.append(str(ipaddress.ip_address(peer_ip)))
    # Walk inward from the known socket peer.  Trusted proxies are discarded;
    # the first untrusted address is the effective client.
    for address in reversed(addresses):
        if not is_trusted_proxy(address, networks):
            return address
    return addresses[0]


def resolve_forwarded_context(
    *, peer_ip: str, direct_scheme: str, direct_host: str,
    headers: Mapping[str, str] | Iterable[tuple[str, str]],
    trusted_cidrs: Sequence[ipaddress._BaseNetwork],
    reject_untrusted_forwarding: bool = True,
) -> ForwardedContext:
    """Resolve protocol/host/client only when the socket peer is trusted."""
    try:
        peer = str(ipaddress.ip_address(peer_ip))
    except ValueError as exc:
        raise TrustedProxyError("invalid_peer_address") from exc
    values = _header_map(headers)
    forwarded_names = FORWARDING_HEADERS.intersection(values)
    trusted = is_trusted_proxy(peer, trusted_cidrs)
    if forwarded_names and not trusted:
        if reject_untrusted_forwarding:
            raise TrustedProxyError("untrusted_forwarding_headers")
        forwarded_names = frozenset()

    direct_scheme = direct_scheme.strip().casefold()
    if direct_scheme not in {"http", "https"}:
        raise TrustedProxyError("invalid_direct_scheme")
    host = normalize_host(direct_host)
    if not forwarded_names:
        return ForwardedContext(peer, direct_scheme, host, peer, False)

    forwarded = _forwarded_first(values["forwarded"]) if "forwarded" in values else {}
    x_for = values.get("x-forwarded-for")
    x_proto = values.get("x-forwarded-proto")
    x_host = values.get("x-forwarded-host")
    x_port = values.get("x-forwarded-port")

    forwarded_for = forwarded.get("for")
    if forwarded_for and x_for and _parse_ip(forwarded_for) != _parse_ip(x_for.split(",", 1)[0]):
        raise TrustedProxyError("conflicting_forwarded_for")
    chain = x_for.split(",") if x_for else ([forwarded_for] if forwarded_for else [peer])
    client_ip = _select_client_ip(chain, peer, trusted_cidrs)

    scheme = (forwarded.get("proto") or x_proto or direct_scheme).strip().casefold()
    if forwarded.get("proto") and x_proto and forwarded["proto"].casefold() != x_proto.casefold():
        raise TrustedProxyError("conflicting_forwarded_proto")
    if scheme not in {"http", "https"}:
        raise TrustedProxyError("invalid_forwarded_proto")

    forwarded_host = forwarded.get("host") or x_host
    if forwarded.get("host") and x_host and normalize_host(forwarded["host"]) != normalize_host(x_host):
        raise TrustedProxyError("conflicting_forwarded_host")
    host = normalize_host(forwarded_host) if forwarded_host else host
    if x_port:
        if not x_port.isdigit() or not 1 <= int(x_port) <= 65535:
            raise TrustedProxyError("invalid_forwarded_port")
        parsed_host = urlsplit(f"//{host}")
        if parsed_host.port is not None and parsed_host.port != int(x_port):
            raise TrustedProxyError("conflicting_forwarded_port")
        hostname = str(parsed_host.hostname)
        if ":" in hostname:
            hostname = f"[{hostname}]"
        host = f"{hostname}:{int(x_port)}"
    return ForwardedContext(client_ip, scheme, host, peer, True)
