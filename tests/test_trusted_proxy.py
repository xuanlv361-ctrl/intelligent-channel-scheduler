import pytest

from backend.trusted_proxy import (
    TrustedProxyError, parse_trusted_cidrs, resolve_forwarded_context,
)


TRUSTED = parse_trusted_cidrs(["10.10.0.0/16", "2001:db8:1::/48"])


def test_direct_peer_is_authoritative_without_forwarding_headers():
    result = resolve_forwarded_context(
        peer_ip="192.0.2.10", direct_scheme="http",
        direct_host="LOCALHOST:8000", headers={}, trusted_cidrs=TRUSTED)
    assert result.client_ip == "192.0.2.10"
    assert result.scheme == "http"
    assert result.host == "localhost:8000"
    assert result.forwarded is False


def test_only_trusted_proxy_may_supply_forwarding_headers():
    with pytest.raises(TrustedProxyError, match="untrusted_forwarding_headers"):
        resolve_forwarded_context(
            peer_ip="192.0.2.10", direct_scheme="http", direct_host="internal:8000",
            headers={"x-forwarded-proto": "https"}, trusted_cidrs=TRUSTED)


def test_trusted_proxy_resolves_client_protocol_and_host():
    result = resolve_forwarded_context(
        peer_ip="10.10.1.9", direct_scheme="http", direct_host="internal:8000",
        headers={
            "forwarded": 'for="203.0.113.8";proto=https;host="console.example.test"',
            "x-forwarded-for": "203.0.113.8, 10.10.2.5",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "console.example.test",
        }, trusted_cidrs=TRUSTED)
    assert result.client_ip == "203.0.113.8"
    assert result.scheme == "https"
    assert result.host == "console.example.test"
    assert result.peer_ip == "10.10.1.9"
    assert result.forwarded is True


@pytest.mark.parametrize("headers,code", [
    ({"forwarded": "for=203.0.113.8;proto=https",
      "x-forwarded-for": "198.51.100.4"}, "conflicting_forwarded_for"),
    ({"forwarded": "for=203.0.113.8;proto=https",
      "x-forwarded-proto": "http"}, "conflicting_forwarded_proto"),
    ({"x-forwarded-for": "not-an-ip"}, "invalid_forwarded_for"),
    ({"x-forwarded-host": "good.test@evil.test"}, "invalid_forwarded_host"),
    ({"x-forwarded-port": "70000"}, "invalid_forwarded_port"),
])
def test_malformed_or_conflicting_forwarding_fails_closed(headers, code):
    with pytest.raises(TrustedProxyError, match=code):
        resolve_forwarded_context(
            peer_ip="10.10.1.9", direct_scheme="http", direct_host="internal:8000",
            headers=headers, trusted_cidrs=TRUSTED)


def test_duplicate_forwarding_header_is_rejected():
    with pytest.raises(TrustedProxyError, match="duplicate_forwarding_header"):
        resolve_forwarded_context(
            peer_ip="10.10.1.9", direct_scheme="http", direct_host="internal:8000",
            headers=[("x-forwarded-proto", "https"),
                     ("X-Forwarded-Proto", "https")], trusted_cidrs=TRUSTED)


def test_trusted_cidrs_require_canonical_networks():
    with pytest.raises(TrustedProxyError, match="invalid_trusted_proxy_cidr"):
        parse_trusted_cidrs(["10.10.1.9/16"])
