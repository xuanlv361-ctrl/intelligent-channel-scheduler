"""Non-network channel adapter used by the Scheduler MVP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AdapterReceipt:
    adapter_name: str
    status: str
    candidate_id: str
    request_id: str
    is_mock: bool = True
    real_api_called: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "status": self.status,
            "candidate_id": self.candidate_id,
            "request_id": self.request_id,
            "is_mock": self.is_mock,
            "real_api_called": self.real_api_called,
        }


class MockChannelAdapter:
    """Return a deterministic dispatch receipt without contacting a channel."""

    name = "mock_channel_adapter_v1"

    VALID_RESULTS = {"success", "timeout", "rate_limited", "authentication_failed", "upstream_error", "invalid_response"}

    # Maps this adapter's own failure-mode vocabulary to the canonical runtime
    # error taxonomy (data/error_taxonomy_runtime_v1.json), so the Scheduler's
    # fallback policy and the Console's classify() reason about the same
    # category names. "status" keeps returning the raw failure_mode unchanged
    # for backward compatibility; only "error_category" is canonicalized.
    ERROR_CATEGORY_BY_FAILURE_MODE = {
        "timeout": "upstream_timeout",
        "rate_limited": "rate_limited",
        "authentication_failed": "channel_authentication_error",
        "upstream_error": "upstream_5xx",
        "invalid_response": "response_schema_error",
    }

    def __init__(self, failure_mode: str = "success"):
        if failure_mode not in self.VALID_RESULTS:
            raise ValueError(f"unsupported failure_mode: {failure_mode}")
        self.failure_mode = failure_mode

    def send(self, request: Any, candidate: Mapping[str, Any]) -> dict[str, Any]:
        candidate_id = str(candidate.get("candidate_id", ""))
        if not candidate_id:
            raise ValueError("candidate_id is required")
        input_tokens = int(getattr(request, "input_tokens", 0))
        output_tokens = int(getattr(request, "output_tokens", 0))
        error = None if self.failure_mode == "success" else self.ERROR_CATEGORY_BY_FAILURE_MODE[self.failure_mode]
        return {
            "adapter_type": "mock", "candidate_id": candidate_id,
            "status": self.failure_mode, "error_category": error,
            "latency_ms": float(candidate.get("latency_ms", 0)),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "is_mock": True, "network_called": False,
        }

    def dispatch(self, *, request_id: str, candidate_id: str) -> AdapterReceipt:
        if not request_id or not candidate_id:
            raise ValueError("request_id and candidate_id are required")
        return AdapterReceipt(
            adapter_name=self.name,
            status="mock_accepted",
            candidate_id=candidate_id,
            request_id=request_id,
        )


class RealExecutionBlockedError(RuntimeError):
    pass


class BlockedRealChannelAdapter:
    """Non-network placeholder. Real execution is unavailable in Runtime v1."""

    def send(self, request: Any, candidate: Mapping[str, Any]) -> dict[str, Any]:
        raise RealExecutionBlockedError("real channel execution is not authorized")
