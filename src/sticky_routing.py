"""Safe, versioned primitives for persistent policy-aware sticky routing.

The raw affinity value is an ephemeral input.  It is never returned or stored.
The persisted route identifier is an HMAC-SHA256 digest scoped by environment,
requested model, canonical capabilities, policy/configuration versions and key
identifier.  Rotating the server-side secret/key identifier makes old bindings
ineligible; recovery explicitly invalidates them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


GATE_ORDER = (
    "security_authorization",
    "capability",
    "environment",
    "budget",
    "health",
    "statistical_confidence",
    "freshness",
    "circuit_breaker",
)
CAPABILITY_KEYS = (
    "stream",
    "required_modalities",
    "requires_tools",
    "requires_structured_output",
)
ALLOWED_MODALITIES = frozenset({"text", "image", "audio", "video"})
GATE_PASS_STATES = {
    "security_authorization": frozenset({"pass", "allowed", "authorized"}),
    "capability": frozenset({"pass", "allowed", "supported"}),
    "environment": frozenset({"pass", "allowed", "matched"}),
    "budget": frozenset({"pass", "allowed", "within_budget"}),
    "health": frozenset({"pass", "allowed", "healthy"}),
    "statistical_confidence": frozenset({"pass", "allowed", "ready", "sufficient"}),
    "freshness": frozenset({"pass", "ready", "fresh"}),
    "circuit_breaker": frozenset({"pass", "closed"}),
}


class StickyRoutingError(ValueError):
    """Safe configuration or input failure."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def load_policy(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        policy = json.load(handle)
    required = {
        "policy_version", "configuration_version", "enabled", "ttl_seconds",
        "maximum_total_duration_seconds", "route_key_derivation_version",
        "capability_scope_version", "maximum_evidence_ids",
    }
    missing = sorted(required - policy.keys())
    if missing:
        raise StickyRoutingError(f"sticky policy missing fields: {', '.join(missing)}")
    ttl = int(policy["ttl_seconds"])
    maximum = int(policy["maximum_total_duration_seconds"])
    if ttl <= 0 or maximum < ttl:
        raise StickyRoutingError("sticky TTL must be positive and not exceed maximum duration")
    if tuple(policy.get("required_gate_order", GATE_ORDER)) != GATE_ORDER:
        raise StickyRoutingError("sticky eligibility gate order is immutable")
    interruption_categories = set(policy.get("interruption_error_categories", []))
    required_interruptions = {
        "fallback_requires_reassignment", "upstream_timeout", "upstream_5xx",
        "rate_limited", "gateway_timeout", "network_transport_error",
    }
    if not required_interruptions.issubset(interruption_categories):
        raise StickyRoutingError("sticky interruption policy omits mandatory reassignment errors")
    return policy


def normalize_capability_scope(value: Mapping[str, Any] | None, *, stream: bool) -> dict[str, Any]:
    raw = dict(value or {})
    unknown = sorted(set(raw) - set(CAPABILITY_KEYS))
    if unknown:
        raise StickyRoutingError(f"unsupported capability fields: {', '.join(unknown)}")
    modalities = raw.get("required_modalities", ["text"])
    if not isinstance(modalities, (list, tuple)) or not modalities:
        raise StickyRoutingError("required_modalities must be a non-empty list")
    normalized_modalities = sorted({str(item).strip().lower() for item in modalities})
    if any(item not in ALLOWED_MODALITIES for item in normalized_modalities):
        raise StickyRoutingError("unsupported capability modality")
    for field in ("requires_tools", "requires_structured_output"):
        if field in raw and not isinstance(raw[field], bool):
            raise StickyRoutingError(f"{field} must be a boolean")
    if "stream" in raw and raw["stream"] is not stream:
        raise StickyRoutingError("capability stream must match request stream")
    return {
        "stream": stream,
        "required_modalities": normalized_modalities,
        "requires_tools": bool(raw.get("requires_tools", False)),
        "requires_structured_output": bool(raw.get("requires_structured_output", False)),
    }


def validate_affinity(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StickyRoutingError("sticky affinity must be a non-empty opaque value")
    cleaned = value.strip()
    if len(cleaned) > 512 or any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise StickyRoutingError("sticky affinity is invalid")
    return cleaned


@dataclass(frozen=True, slots=True)
class RouteScope:
    route_key_fingerprint: str
    affinity_fingerprint: str
    safe_fingerprint: str
    capability_scope: dict[str, Any]
    capability_scope_hash: str


def derive_route_scope(
    *,
    secret: str,
    key_id: str,
    affinity: str,
    environment_id: str,
    requested_model: str,
    capability_scope: Mapping[str, Any] | None,
    stream: bool,
    policy_version: str,
    configuration_version: str,
    derivation_version: str = "hmac-sha256-v1",
    capability_scope_version: str = "sticky-capability-scope-v1",
) -> RouteScope:
    if derivation_version != "hmac-sha256-v1":
        raise StickyRoutingError("unsupported route-key derivation version")
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise StickyRoutingError("sticky route-key secret must contain at least 32 bytes")
    if not key_id or any(ord(ch) < 33 or ord(ch) > 126 for ch in key_id):
        raise StickyRoutingError("sticky route-key identifier is invalid")
    affinity_value = validate_affinity(affinity)
    environment = str(environment_id).strip()
    model = str(requested_model).strip()
    if not environment or not model:
        raise StickyRoutingError("environment and requested model are required")
    capabilities = normalize_capability_scope(capability_scope, stream=stream)
    capability_json = canonical_json(capabilities)
    capability_hash = hashlib.sha256(capability_json.encode("ascii")).hexdigest()
    message = canonical_json({
        "affinity": affinity_value,
        "environment_id": environment,
        "requested_model": model,
        "capability_scope": capabilities,
        "policy_version": policy_version,
        "configuration_version": configuration_version,
        "capability_scope_version": capability_scope_version,
        "key_id": key_id,
        "derivation_version": derivation_version,
    }).encode("ascii")
    secret_bytes = secret.encode("utf-8")
    digest = hmac.new(secret_bytes, message, hashlib.sha256).hexdigest()
    affinity_digest = hmac.new(
        secret_bytes,
        canonical_json({
            "affinity": affinity_value, "key_id": key_id,
            "derivation_version": derivation_version,
            "purpose": "sticky-affinity-interruption-lookup",
        }).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return RouteScope(digest, affinity_digest, digest[:16], capabilities, capability_hash)


def normalize_gate_results(values: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], str | None]:
    supplied = dict(values or {})
    trace: list[dict[str, Any]] = []
    first_failure = None
    for gate in GATE_ORDER:
        raw = supplied.get(gate)
        if isinstance(raw, Mapping):
            state = str(raw.get("state", "unknown")).strip().lower()
            reason = str(raw.get("reason") or "") or None
            evidence_id = str(raw.get("evidence_id") or "") or None
        elif isinstance(raw, bool):
            state, reason, evidence_id = ("pass" if raw else "blocked"), None, None
        elif isinstance(raw, str):
            state, reason, evidence_id = raw.strip().lower(), None, None
        else:
            state, reason, evidence_id = (
                "unknown", f"{gate}_gate_result_missing", None)
        passed = state in GATE_PASS_STATES[gate]
        item = {"gate": gate, "state": state, "passed": passed}
        if reason:
            item["reason"] = reason
        if evidence_id:
            item["evidence_id"] = evidence_id
        trace.append(item)
        if not passed and first_failure is None:
            first_failure = reason or f"{gate}_{state}"
    return trace, first_failure
