"""Bounded model-level recovery for live China-UAT acceptance traffic.

The service owns orchestration only.  Provider I/O is supplied by the caller so
credentials never enter persisted configuration or this module's audit data.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Mapping


RETRYABLE = frozenset({"rate_limited", "upstream_timeout", "upstream_5xx", "network_timeout", "network_error", "sse_protocol_error"})


class UatLiveRecoveryError(ValueError):
    pass


class UatLiveRecoveryService:
    def __init__(self, *, call_logs: Any, circuit_breakers: Any,
                 acceptance_runs: Any | None = None):
        self.call_logs = call_logs
        self.circuit_breakers = circuit_breakers
        self.acceptance_runs = acceptance_runs

    @staticmethod
    def _id(prefix: str) -> str:
        return prefix + uuid.uuid4().hex.upper()

    def execute(self, *, environment_id: str, acceptance_run_id: str,
                models: list[str], stream: bool, endpoint_type: str,
                strategy_variant: str,
                executor: Callable[[str, int], Mapping[str, Any]],
                maximum_attempts: int = 2,
                traffic_proposal_id: str | None = None) -> dict[str, Any]:
        if environment_id != "china_uat":
            raise UatLiveRecoveryError("uat_recovery_environment_not_allowed")
        order = [str(model).strip() for model in models if str(model).strip()]
        if not order or len(set(order)) != len(order):
            raise UatLiveRecoveryError("uat_recovery_candidates_invalid")
        if maximum_attempts not in {1, 2}:
            raise UatLiveRecoveryError("uat_recovery_attempt_limit_invalid")
        order = order[:maximum_attempts]
        request_id, decision_id = self._id("REQ-"), self._id("DEC-")
        record_id = self.call_logs.start_execution(
            request_id=request_id, decision_id=decision_id,
            environment_id=environment_id, requested_model=order[0], stream=stream,
            endpoint_type=endpoint_type, acceptance_run_id=acceptance_run_id,
            error_source="provider_live", is_fault_injected=False,
            fallback_used=False, output_started=False,
            strategy_variant=strategy_variant, traffic_proposal_id=traffic_proposal_id)
        attempts: list[dict[str, Any]] = []
        selected: str | None = None
        terminal: dict[str, Any] = {}
        for index, model in enumerate(order, start=1):
            circuit_id = f"china_uat:model:{model}"
            admission = self.circuit_breakers.before_request(circuit_id, f"{request_id}:{index}")
            if not admission["allowed"]:
                observed = {"execution_status": "failed", "http_status": None,
                            "error_category": "circuit_open", "retryable": False,
                            "error_source": "circuit_breaker", "is_fault_injected": False,
                            "output_started": False, "total_latency_ms": 0.0}
            else:
                observed = dict(executor(model, index))
                category = str(observed.get("error_category") or "")
                affects = bool(observed.get("fault_affects_circuit", True))
                outcome_id = self._id("CBE-")
                lease = admission.get("probe_lease_id")
                if observed.get("execution_status") == "success":
                    self.circuit_breakers.record_success(circuit_id, outcome_id, lease)
                elif affects and category in RETRYABLE:
                    self.circuit_breakers.record_failure(circuit_id, outcome_id, lease)
            success = observed.get("execution_status") == "success"
            category = str(observed.get("error_category") or "") or None
            attempt = {
                "attempt_number": index, "model": model,
                "status": "SUCCESS" if success else "FAILED",
                "http_status": observed.get("http_status"),
                "error_category": category,
                "retryable": bool(category in RETRYABLE),
                "fallback_used": index > 1,
                "output_started": bool(observed.get("output_started")),
                "error_source": observed.get("error_source") or "provider_live",
                "is_fault_injected": bool(observed.get("is_fault_injected")),
                "fault_id": observed.get("fault_id"),
                "total_latency_ms": observed.get("total_latency_ms"),
                "first_token_latency_ms": observed.get("first_token_latency_ms"),
                "input_tokens": observed.get("input_tokens"),
                "cached_input_tokens": observed.get("cached_input_tokens"),
                "output_tokens": observed.get("output_tokens"),
                "cost_amount": observed.get("cost_amount"),
                "currency": observed.get("currency"),
                "response_id": observed.get("response_id"),
                "actual_model": observed.get("actual_model"),
            }
            attempts.append(attempt)
            if index > 1:
                self.call_logs.record_attempt(
                    request_id=request_id, attempt_number=index,
                    requested_model=model, status=attempt["status"],
                    actual_model=attempt["actual_model"], http_status=attempt["http_status"],
                    error_code=None if success else f"http_{attempt['http_status'] or 'transport'}",
                    error_category=category, retryable=attempt["retryable"],
                    total_latency_ms=attempt["total_latency_ms"],
                    first_token_latency_ms=attempt["first_token_latency_ms"],
                    input_tokens=attempt["input_tokens"], cached_input_tokens=attempt["cached_input_tokens"],
                    output_tokens=attempt["output_tokens"], cost_amount=attempt["cost_amount"],
                    currency=attempt["currency"], acceptance_run_id=acceptance_run_id,
                    error_source=attempt["error_source"], is_fault_injected=attempt["is_fault_injected"],
                    fault_id=attempt["fault_id"], fallback_used=True,
                    output_started=attempt["output_started"], strategy_variant=strategy_variant,
                    traffic_proposal_id=traffic_proposal_id)
            terminal = observed
            if success:
                selected = model
                break
            if attempt["output_started"] or category not in RETRYABLE:
                break
        success = selected is not None
        self.call_logs.finish_execution(
            record_id, status="SUCCESS" if success else "FAILED",
            response_id=terminal.get("response_id"),
            actual_model=terminal.get("actual_model") or selected,
            http_status=terminal.get("http_status"),
            error_code=None if success else f"http_{terminal.get('http_status') or 'transport'}",
            error_category=None if success else terminal.get("error_category"),
            retryable=False, total_latency_ms=sum(float(a.get("total_latency_ms") or 0) for a in attempts),
            first_token_latency_ms=terminal.get("first_token_latency_ms"),
            input_tokens=sum(int(a.get("input_tokens") or 0) for a in attempts) or None,
            cached_input_tokens=sum(int(a.get("cached_input_tokens") or 0) for a in attempts) or None,
            output_tokens=sum(int(a.get("output_tokens") or 0) for a in attempts) or None,
            cost_amount=(sum(float(a.get("cost_amount") or 0) for a in attempts)
                         if any(a.get("cost_amount") is not None for a in attempts) else None),
            currency=terminal.get("currency"), total_attempts=len(attempts),
            error_source=terminal.get("error_source") or "provider_live",
            is_fault_injected=bool(terminal.get("is_fault_injected")),
            fault_id=terminal.get("fault_id"), fallback_used=len(attempts) > 1,
            output_started=bool(terminal.get("output_started")), strategy_variant=strategy_variant,
            traffic_proposal_id=traffic_proposal_id,
            cost_source=terminal.get("cost_source"))
        result = {
            "execution_status": "success" if success else "failed",
            "request_id": request_id, "decision_id": decision_id,
            "response_id": terminal.get("response_id"), "requested_model": order[0],
            "actual_model": terminal.get("actual_model") or selected,
            "http_status": terminal.get("http_status"), "attempts": attempts,
            "fallback_used": len(attempts) > 1, "selected_model": selected,
            "stopped_reason": ("success" if success else
                "output_started_fallback_forbidden" if attempts[-1]["output_started"] else
                "non_retryable_error" if not attempts[-1]["retryable"] else "all_candidates_failed"),
        }
        self.call_logs.save_routing_decision(result=result, routing_decision={
            "policy": strategy_variant,
            "candidates": [{"model_id": model, "score": round(1-index*0.01, 4),
                            "capability_status": "catalog_confirmed", "reason": "ordered_candidate"}
                           for index, model in enumerate(order)],
            "excluded": [], "selected_model": selected,
            "selection_reason": result["stopped_reason"],
            "confidence": "high" if success else "failed",
            "catalog_candidate_count": len(order),
        })
        if self.acceptance_runs is not None:
            self.acceptance_runs.link_evidence(acceptance_run_id,
                request_id=request_id, decision_id=decision_id,
                fault_id=terminal.get("fault_id"), audit_id=terminal.get("fault_audit_id"))
        return result
