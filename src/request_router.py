"""Request validation for the local Scheduler MVP.

This module performs no I/O other than ordinary in-process validation and has
no knowledge of channel outcomes or adapter execution.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


REQUIRED_FIELDS = (
    "request_id",
    "requested_model",
    "stream_required",
    "input_tokens",
    "output_tokens",
    "currency",
    "strategy",
)


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    requested_model: str
    stream_required: bool
    input_tokens: int
    output_tokens: int
    currency: str
    strategy: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RequestValidationError(ValueError):
    """Structured request validation failure without request-body disclosure."""

    def __init__(self, message: str, *, error_code: str = "invalid_request", field: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.field = field
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error_code": self.error_code, "field": self.field, "message": self.message}


RUNTIME_MODES = {"simulation", "mock_execute", "real_shadow", "real_execute"}
SENSITIVE_METADATA_TERMS = (
    "api_key", "apikey", "authorization", "secret", "cookie",
    "password", "credential", "dpapi", "browser_storage", "storage_state",
    "local_storage", "session_storage", "prompt", "messages", "raw_request",
    "raw_response", "request_body", "response_body",
)
SENSITIVE_TOKEN_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "bearer_token",
    "auth_token", "session_token", "csrf_token",
})
PROTECTED_METADATA_KEYS = frozenset({"body", "content", "response"})
FIRST_CLASS_STRATEGIES = frozenset({"latency_first", "cost_first"})


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    request_id: str
    requested_model: str
    stream: bool
    input_tokens: int
    output_tokens: int
    currency: str
    strategy: str
    mode: str
    max_latency_ms: float | None = None
    max_estimated_cost: float | None = None
    metadata: dict[str, Any] | None = None
    sticky_affinity_key: str | None = None
    capability_scope: dict[str, Any] | None = None

    @property
    def stream_required(self) -> bool:
        return self.stream

    def to_dict(self) -> dict[str, Any]:
        # The raw affinity value is deliberately absent from every serializable
        # request representation because decisions and logs persist this dict.
        values = asdict(self)
        values.pop("sticky_affinity_key", None)
        return values


class RequestRouter:
    def __init__(self, supported_strategies: list[str] | tuple[str, ...] | set[str]):
        self.supported_strategies = frozenset(supported_strategies)

    def route(self, payload: Request | Mapping[str, Any]) -> Request:
        if isinstance(payload, Request):
            values = payload.to_dict()
        elif isinstance(payload, Mapping):
            values = dict(payload)
        else:
            raise RequestValidationError("request must be a mapping or Request")

        missing = [field for field in REQUIRED_FIELDS if field not in values]
        if missing:
            raise RequestValidationError(f"missing required fields: {', '.join(missing)}")

        for field in ("request_id", "requested_model", "currency", "strategy"):
            if not isinstance(values[field], str) or not values[field].strip():
                raise RequestValidationError(f"{field} must be a non-empty string")
        if not isinstance(values["stream_required"], bool):
            raise RequestValidationError("stream_required must be a boolean")
        for field in ("input_tokens", "output_tokens"):
            value = values[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RequestValidationError(f"{field} must be a non-negative integer")
        if values["strategy"] not in self.supported_strategies:
            raise RequestValidationError(f"unsupported strategy: {values['strategy']}")

        return Request(
            request_id=values["request_id"].strip(),
            requested_model=values["requested_model"].strip(),
            stream_required=values["stream_required"],
            input_tokens=values["input_tokens"],
            output_tokens=values["output_tokens"],
            currency=values["currency"].strip().upper(),
            strategy=values["strategy"],
        )

    def route_runtime(self, payload: RuntimeRequest | Mapping[str, Any]) -> RuntimeRequest:
        if isinstance(payload, RuntimeRequest):
            # In-process revalidation may retain the ephemeral affinity value;
            # serialization through to_dict() still deliberately removes it.
            values = asdict(payload)
        elif isinstance(payload, Mapping):
            values = dict(payload)
        else:
            raise RequestValidationError("request must be a mapping or RuntimeRequest", field="request")
        if "stream" not in values and "stream_required" in values:
            values["stream"] = values["stream_required"]
        required = ("request_id", "requested_model", "stream", "input_tokens", "output_tokens", "currency", "strategy", "mode")
        for field in required:
            if field not in values:
                raise RequestValidationError(f"missing required field: {field}", error_code="missing_field", field=field)
        for field in ("request_id", "requested_model", "currency"):
            if not isinstance(values[field], str) or not values[field].strip():
                raise RequestValidationError(f"{field} must be a non-empty string", field=field)
        if not isinstance(values["stream"], bool):
            raise RequestValidationError("stream must be a boolean", field="stream")
        for field in ("input_tokens", "output_tokens"):
            value = values[field]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RequestValidationError(f"{field} must be a non-negative integer", field=field)
        if values["strategy"] not in self.supported_strategies:
            raise RequestValidationError("unsupported strategy", error_code="unsupported_strategy", field="strategy")
        # Strategy identity is contractual. In particular, the first-class
        # weighted strategies must never be rewritten to the legacy
        # fastest/cheapest single-metric strategies.
        strategy = values["strategy"]
        if values["mode"] not in RUNTIME_MODES:
            raise RequestValidationError("unsupported mode", error_code="unsupported_mode", field="mode")
        for field in ("max_latency_ms", "max_estimated_cost"):
            value = values.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
                raise RequestValidationError(f"{field} must be a non-negative number", field=field)
        metadata = values.get("metadata")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise RequestValidationError("metadata must be an object", field="metadata")
        if metadata is not None:
            def contains_sensitive(value: Any) -> bool:
                if isinstance(value, Mapping):
                    return any(
                        str(key).casefold() in PROTECTED_METADATA_KEYS
                        or str(key).casefold() in SENSITIVE_TOKEN_KEYS
                        or any(
                            term in str(key).casefold()
                            for term in SENSITIVE_METADATA_TERMS
                        )
                        or contains_sensitive(item)
                        for key, item in value.items()
                    )
                if isinstance(value, (list, tuple)):
                    return any(contains_sensitive(item) for item in value)
                return False
            if contains_sensitive(metadata):
                raise RequestValidationError("metadata contains a prohibited sensitive field", error_code="sensitive_metadata", field="metadata")
        sticky_affinity = values.get("sticky_affinity_key")
        if sticky_affinity is not None:
            if not isinstance(sticky_affinity, str) or not sticky_affinity.strip():
                raise RequestValidationError(
                    "sticky_affinity_key must be a non-empty opaque string",
                    field="sticky_affinity_key")
            if len(sticky_affinity.strip()) > 512 or any(
                ord(ch) < 32 or ord(ch) == 127 for ch in sticky_affinity.strip()
            ):
                raise RequestValidationError(
                    "sticky_affinity_key is invalid", field="sticky_affinity_key")
        capability_scope = values.get("capability_scope")
        if capability_scope is not None and not isinstance(capability_scope, Mapping):
            raise RequestValidationError(
                "capability_scope must be an object", field="capability_scope")
        if capability_scope is not None:
            # Import lazily to keep the basic Request path independent.
            from sticky_routing import StickyRoutingError, normalize_capability_scope
            try:
                normalized_capabilities = normalize_capability_scope(
                    capability_scope, stream=values["stream"])
            except StickyRoutingError as exc:
                raise RequestValidationError(
                    str(exc), field="capability_scope") from exc
        else:
            normalized_capabilities = None
        return RuntimeRequest(
            request_id=values["request_id"].strip(), requested_model=values["requested_model"].strip(),
            stream=values["stream"], input_tokens=values["input_tokens"], output_tokens=values["output_tokens"],
            currency=values["currency"].strip().upper(), strategy=strategy, mode=values["mode"],
            max_latency_ms=values.get("max_latency_ms"), max_estimated_cost=values.get("max_estimated_cost"),
            metadata=dict(metadata) if metadata is not None else None,
            sticky_affinity_key=sticky_affinity.strip() if sticky_affinity is not None else None,
            capability_scope=normalized_capabilities,
        )
