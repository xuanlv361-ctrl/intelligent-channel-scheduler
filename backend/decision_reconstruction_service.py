"""Content-addressed, read-only reconstruction of one Scheduler decision."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from decision_logger import (  # noqa: E402
    DecisionLogger,
    content_sha256,
    redact_sensitive,
)

from backend.scheduler_attribution_service import SchedulerAttributionService


class DecisionReconstructionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


REQUIRED_RUNTIME_FIELDS = {
    "decision_id",
    "run_id",
    "request_id",
    "runtime_version",
    "mode",
    "strategy",
    "requested_model",
    "catalog_version",
    "catalog_sha256",
    "decision_policy_version",
    "candidate_ranking",
    "excluded_candidates",
    "fallback_order",
    "attempts",
    "outcome",
    "stopped_reason",
}

SAFE_REQUEST_FIELDS = (
    "request_id",
    "requested_model",
    "stream",
    "mode",
    "strategy",
    "request_constraints",
)


def _assert_finite(value: Any, path: str = "root") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise DecisionReconstructionError(f"non_finite_reconstruction_value:{path}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _assert_finite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_finite(item, f"{path}[{index}]")


class DecisionReconstructionService:
    """Build a byte-stable explanation without exposing request content."""

    def __init__(
        self,
        runtime_log_path: str | Path,
        attribution_service: SchedulerAttributionService,
    ) -> None:
        self.logger = DecisionLogger(runtime_log_path)
        self.attribution_service = attribution_service

    def reconstruct(self, decision_id: str) -> dict[str, Any]:
        if not isinstance(decision_id, str) or not decision_id.strip():
            raise DecisionReconstructionError("decision_id_required")
        try:
            runtime = self.logger.find_unique_by_decision_id(decision_id.strip())
        except ValueError as exc:
            raise DecisionReconstructionError(str(exc)) from exc
        if runtime is None:
            return {
                "status": "blocked",
                "reason": "decision_not_found",
                "decision_id": decision_id.strip(),
                "network_called": False,
            }

        safe_runtime = redact_sensitive(runtime)
        missing = sorted(REQUIRED_RUNTIME_FIELDS - set(safe_runtime))
        if missing:
            return {
                "status": "blocked",
                "reason": "decision_reconstruction_fields_missing",
                "missing_fields": missing,
                "decision_id": decision_id.strip(),
                "runtime_record_sha256": content_sha256(safe_runtime),
                "network_called": False,
            }
        _assert_finite(safe_runtime)

        attribution = self.attribution_service.chain(decision_id.strip())
        scheduler_intent = attribution.get("scheduler_decision")
        if scheduler_intent is not None:
            if scheduler_intent.get("request_id") != safe_runtime.get("request_id"):
                raise DecisionReconstructionError("attribution_request_identity_mismatch")
            if scheduler_intent.get("run_id") != safe_runtime.get("run_id"):
                raise DecisionReconstructionError("attribution_run_identity_mismatch")

        request_classification = {
            key: safe_runtime.get(key) for key in SAFE_REQUEST_FIELDS
        }
        policy_binding = {
            "runtime_version": safe_runtime["runtime_version"],
            "catalog_version": safe_runtime["catalog_version"],
            "catalog_sha256": safe_runtime["catalog_sha256"],
            "decision_policy_version": safe_runtime["decision_policy_version"],
            "metric_snapshot_ids": list(safe_runtime.get("metric_snapshot_ids") or []),
            "confidence_snapshot_ids": list(
                safe_runtime.get("confidence_snapshot_ids") or []
            ),
            "capability_evidence_ids": list(
                safe_runtime.get("capability_evidence_ids") or []),
            "price_record_sha256s": list(
                safe_runtime.get("price_record_sha256s") or []),
            "price_context": safe_runtime.get("price_context"),
            "metric_context": safe_runtime.get("metric_context"),
            "capability_context": (
                safe_runtime.get("safety_governance") or {}).get("capability"),
        }
        candidate_evaluation = {
            "candidate_count": safe_runtime.get("candidate_count"),
            "eligible_count": safe_runtime.get("eligible_count"),
            "excluded_count": safe_runtime.get("excluded_count"),
            "candidate_ranking": safe_runtime["candidate_ranking"],
            "excluded_candidates": safe_runtime["excluded_candidates"],
            "selected_target_id": safe_runtime.get("selected_target_id"),
            "selected_channel_id": safe_runtime.get("selected_channel_id"),
            "fallback_order": safe_runtime["fallback_order"],
        }
        execution_trace = {
            "execution_attempted": safe_runtime.get("execution_attempted"),
            "execution_status": safe_runtime.get("execution_status"),
            "attempts": safe_runtime["attempts"],
            "fallback_executed": safe_runtime.get("fallback_executed"),
            "fallback_trace": safe_runtime.get("fallback_trace"),
            "stopped_reason": safe_runtime["stopped_reason"],
            "error_category": safe_runtime.get("error_category"),
            "network_called": bool(safe_runtime.get("network_called")),
        }
        package = {
            "schema_version": "decision_reconstruction_v1",
            "decision_id": decision_id.strip(),
            "run_id": safe_runtime["run_id"],
            "request_id": safe_runtime["request_id"],
            "status": "ready",
            "request_classification": request_classification,
            "policy_binding": policy_binding,
            "candidate_evaluation": candidate_evaluation,
            "decision_result": {
                "outcome": safe_runtime["outcome"],
                "recommended_candidate": safe_runtime.get("recommended_candidate"),
                "selection_reason": safe_runtime.get("selection_reason"),
                "acceptance_assertion": safe_runtime.get("acceptance_assertion"),
                "governance_assertions": safe_runtime.get("governance_assertions"),
            },
            "execution_trace": execution_trace,
            "attribution": attribution,
            "limitations": list(safe_runtime.get("limitations") or []),
            "runtime_record_sha256": content_sha256(safe_runtime),
            "network_called": False,
        }
        package["component_sha256"] = {
            "request_classification": content_sha256(request_classification),
            "policy_binding": content_sha256(policy_binding),
            "candidate_evaluation": content_sha256(candidate_evaluation),
            "execution_trace": content_sha256(execution_trace),
            "attribution": content_sha256(attribution),
        }
        package["reconstruction_sha256"] = content_sha256(package)
        return package
