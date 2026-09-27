"""Controlled offline recovery across a decision's primary and backup channels."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from execution_protocol import (
    AttemptContext, CancellationToken, ExecutionCancelled, ReleaseOnce,
)
from retry_policy import RetryPolicy
from error_taxonomy import classify_runtime_error, load_taxonomy, redact_error_message

try:
    from backend.security.authorization import AuthorizationService
    from backend.security.principal import PrincipalContext, PrincipalType
except ImportError:  # pragma: no cover - standalone source compatibility
    AuthorizationService = Any  # type: ignore[misc,assignment]
    PrincipalContext = Any  # type: ignore[misc,assignment]
    PrincipalType = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "config" / "retry_policy_v1.json"
DEFAULT_DEMO_OUTPUT = ROOT / "output" / "execution_recovery_demo_v1.json"


def load_json(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


class ExecutionEngineError(ValueError):
    pass


class ExecutionEngine:
    def __init__(
        self,
        executor: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]],
        retry_policy: RetryPolicy,
        *,
        attempt_logger: Callable[[Mapping[str, Any]], None] | None = None,
        before_fallback: Callable[[Mapping[str, Any], str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        authorization_service: AuthorizationService | None = None,
    ):
        self.executor = executor
        self.retry_policy = retry_policy
        self.attempt_logger = attempt_logger
        self.before_fallback = before_fallback
        self.clock = clock
        self.sleeper = sleeper
        self.authorization_service = authorization_service
        self.error_taxonomy = load_taxonomy()
        self._idempotency_lock = threading.Lock()
        self._idempotency_results: dict[str, tuple[str, dict[str, Any]]] = {}

    @staticmethod
    def _scope_digest(request_id: str, order: list[str], *,
                      tenant_id: str, workspace_id: str) -> str:
        encoded = json.dumps(
            {"request_id": request_id, "candidate_order": order,
             "tenant_id": tenant_id, "workspace_id": workspace_id},
            sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _authorize(self, principal: PrincipalContext | None) -> tuple[str, str]:
        if self.authorization_service is None:
            return "tenant_local_dev_v1", "workspace_local_dev_v1"
        verified = self.authorization_service.authorize(
            principal, "scheduler.execute", principal_type=PrincipalType.SERVICE)
        if "scheduler_service" not in verified.roles:
            raise ExecutionEngineError("scheduler_service_identity_required")
        return verified.tenant_id, verified.workspace_id

    def _invoke(self, request: Any, candidate: Mapping[str, Any],
                context: AttemptContext) -> Mapping[str, Any]:
        contextual = getattr(self.executor, "execute_attempt", None)
        if callable(contextual):
            return contextual(request, candidate, context)
        return self.executor(request, candidate)

    def _normalise_failure(self, raw: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        explicit = str(raw.get("error_category") or "")
        categories = self.error_taxonomy["categories"]
        if explicit in categories:
            meta = categories[explicit]
            classified = {
                "error_category": explicit,
                "error_code": meta["error_code"],
                "error_layer": meta["error_layer"],
                "retryable": bool(meta["retryable"]),
                "fallback_allowed": bool(meta["fallback_allowed"]),
                "sanitized_message": redact_error_message(raw.get("error") or explicit),
                "original_status": raw.get("http_status"),
                "classification_rule_version": self.error_taxonomy["taxonomy_version"],
                "classification_source": "adapter_explicit_canonical_category",
            }
        else:
            classified = classify_runtime_error(
                status=(raw.get("http_status") if isinstance(raw.get("http_status"), int)
                        else raw.get("status_code") if isinstance(raw.get("status_code"), int)
                        else None),
                message=str(raw.get("error") or raw.get("message") or raw.get("status") or ""),
                context=str(raw.get("error_context") or raw.get("context") or ""),
                taxonomy=self.error_taxonomy,
            )
            classified["classification_source"] = "execution_boundary_normalization"
        legacy = str(raw.get("error") or raw.get("error_category") or "")
        policy_category = classified["error_category"]
        configured = self.retry_policy.retryable_errors | self.retry_policy.non_retryable_errors
        if legacy in configured:
            policy_category = legacy
        return policy_category, classified

    def execute(
        self,
        *,
        request: Any,
        decision: Mapping[str, Any],
        candidates: Mapping[str, Mapping[str, Any]],
        cancellation_token: CancellationToken | None = None,
        deadline_seconds: float | None = None,
        idempotency_key: str | None = None,
        release_callback: Callable[[Mapping[str, Any]], None] | None = None,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        tenant_id, workspace_id = self._authorize(principal)
        request_id = str(
            getattr(request, "request_id", None) or decision.get("request_id", "")
        )
        primary = decision.get("recommended_candidate") or decision.get(
            "selected_candidate"
        )
        if not request_id or not primary:
            raise ExecutionEngineError("request_id and primary candidate are required")
        order = [str(primary), *[
            str(value) for value in decision.get("fallback_order", [])
            if str(value) != str(primary)
        ]]
        token = cancellation_token or CancellationToken()
        release = ReleaseOnce(release_callback)
        scope_digest = self._scope_digest(
            request_id, order, tenant_id=tenant_id, workspace_id=workspace_id)
        if idempotency_key:
            with self._idempotency_lock:
                replay = self._idempotency_results.get(str(idempotency_key))
            if replay:
                if replay[0] != scope_digest:
                    raise ExecutionEngineError("idempotency_key_scope_conflict")
                result = copy.deepcopy(replay[1])
                result["idempotent_replay"] = True
                return result
        attempts = []
        final_channel = None
        final_status = "failed"
        stopped_reason = "candidate_order_exhausted"
        bounded_order = order[: self.retry_policy.max_attempts]
        total_limit = (self.retry_policy.total_deadline_seconds
                       if deadline_seconds is None else float(deadline_seconds))
        if not math.isfinite(total_limit) or total_limit <= 0:
            raise ExecutionEngineError("deadline_seconds must be positive")
        started = self.clock()
        deadline = started + total_limit
        release_error = None
        try:
            for candidate_id in bounded_order:
                if token.cancelled:
                    stopped_reason = "cancelled"
                    final_status = "cancelled"
                    break
                remaining = deadline - self.clock()
                if remaining <= 0:
                    stopped_reason = "total_deadline_exceeded"
                    break
                if candidate_id not in candidates:
                    raise ExecutionEngineError(f"candidate not found: {candidate_id}")
                attempt_started = self.clock()
                timeout = min(self.retry_policy.per_attempt_timeout_seconds, remaining)
                context = AttemptContext(
                    attempt_number=len(attempts) + 1,
                    candidate_id=candidate_id,
                    idempotency_key=(f"{idempotency_key}:attempt:{len(attempts) + 1}"
                                     if idempotency_key else
                                     f"{request_id}:attempt:{len(attempts) + 1}"),
                    started_monotonic=attempt_started,
                    attempt_timeout_seconds=timeout,
                    deadline_monotonic=deadline,
                    cancellation_token=token,
                    clock=self.clock,
                )
                try:
                    raw = dict(self._invoke(request, candidates[candidate_id], context))
                except ExecutionCancelled:
                    raw = {"status": "cancelled", "error": "cancelled",
                           "is_mock": True, "network_called": False}
                except TimeoutError as exc:
                    raw = {
                        "status": "timeout", "error": "timeout",
                        "error_category": "upstream_timeout",
                        "adapter_exception_type": type(exc).__name__,
                        "safe_exception_message": redact_error_message(exc),
                        "is_mock": True, "network_called": False,
                    }
                except (ConnectionError, OSError) as exc:
                    raw = {
                        "status": "failed", "error": "connection failure",
                        "error_category": "network_transport_error",
                        "adapter_exception_type": type(exc).__name__,
                        "safe_exception_message": redact_error_message(exc),
                        "is_mock": True, "network_called": False,
                    }
                elapsed = max(0.0, self.clock() - attempt_started)
                status = str(raw.get("status", "failed"))
                success = status in {"success", "ok", "mock_success"}
                if elapsed > timeout:
                    success, status = False, "timeout"
                    raw = {**raw, "status": "timeout", "error": "attempt_timeout",
                           "error_category": "upstream_timeout"}
                if token.cancelled or status == "cancelled":
                    success, status = False, "cancelled"
                    raw = {**raw, "status": "cancelled", "error": "cancelled"}
                classification = None
                if success:
                    error = None
                elif status == "cancelled":
                    error = "cancelled"
                else:
                    error, classification = self._normalise_failure(raw)
                output_started = bool(raw.get("output_started")
                                      or raw.get("first_byte_emitted"))
                attempt = {
                    "attempt_number": len(attempts) + 1,
                    "channel": candidate_id,
                    "result": "success" if success else status if status == "cancelled" else "failed",
                    "error": error,
                    "error_category": (classification or {}).get("error_category"),
                    "error_classification": classification,
                    "is_mock": bool(raw.get("is_mock", True)),
                    "network_called": bool(raw.get("network_called", False)),
                    "raw_result": raw,
                    "attempt_timeout_seconds": timeout,
                    "elapsed_seconds": elapsed,
                    "output_started": output_started,
                    "idempotency_key": context.idempotency_key,
                }
                if attempt["network_called"]:
                    raise ExecutionEngineError(
                        "network execution is forbidden in offline recovery")
                retry = None
                if not success and status != "cancelled":
                    retry = self.retry_policy.retry_decision(
                        error, len(attempts) + 1,
                        retry_after_seconds=raw.get("retry_after_seconds"),
                        remaining_seconds=deadline - self.clock(),
                        output_started=output_started)
                attempt["retry_decision"] = retry
                attempts.append(attempt)
                if self.attempt_logger:
                    self.attempt_logger(attempt)
                if success:
                    final_channel = candidate_id
                    final_status = "success"
                    stopped_reason = "success"
                    break
                if status == "cancelled":
                    final_status = "cancelled"
                    stopped_reason = "cancelled"
                    break
                assert retry is not None
                if not retry["allowed"]:
                    stopped_reason = (
                        retry["reason"] if retry["reason"] in {
                            "output_started_fallback_forbidden",
                            "retry_after_invalid", "retry_after_out_of_bounds",
                            "retry_after_exceeds_deadline"}
                        else "non_retryable_error_or_attempt_limit")
                    break
                if len(attempts) < len(bounded_order) and self.before_fallback:
                    self.before_fallback(attempt, bounded_order[len(attempts)])
                if retry["delay_seconds"]:
                    self.sleeper(retry["delay_seconds"])
                    if token.cancelled:
                        final_status, stopped_reason = "cancelled", "cancelled"
                        break
        finally:
            summary = {
                "request_id": request_id, "idempotency_key": idempotency_key,
                "attempt_count": len(attempts), "final_status": final_status,
                "stopped_reason": stopped_reason,
            }
            try:
                release.release(summary)
            except Exception as exc:  # release failures are evidence, not retries
                release_error = type(exc).__name__
        result = {
            "request_id": request_id,
            "attempts": attempts,
            "final_channel": final_channel,
            "final_status": final_status,
            "fallback_executed": len(attempts) > 1,
            "fallback_trace": [attempt["channel"] for attempt in attempts],
            "stopped_reason": stopped_reason,
            "retry_policy_version": self.retry_policy.policy_version,
            "is_mock": True,
            "network_called": False,
            "validation_scope": "offline demonstration only",
            "deadline_seconds": total_limit,
            "elapsed_seconds": max(0.0, self.clock() - started),
            "idempotency_key": idempotency_key,
            "idempotent_replay": False,
            "release_callback_invoked": release_callback is not None,
            "release_error": release_error,
        }
        if idempotency_key:
            with self._idempotency_lock:
                existing = self._idempotency_results.get(str(idempotency_key))
                if existing and existing[0] != scope_digest:
                    raise ExecutionEngineError("idempotency_key_scope_conflict")
                self._idempotency_results[str(idempotency_key)] = (
                    scope_digest, copy.deepcopy(result))
        return result


class ScriptedMockExecutor:
    def __init__(self, results: Mapping[str, Mapping[str, Any]]):
        self.results = dict(results)

    def __call__(self, request: Any, candidate: Mapping[str, Any]) -> Mapping[str, Any]:
        candidate_id = str(candidate["candidate_id"])
        return {
            **self.results.get(candidate_id, {
                "status": "failed", "error": "unscripted_candidate"
            }),
            "is_mock": True,
            "network_called": False,
        }


def build_demo() -> dict[str, Any]:
    policy = RetryPolicy(load_json(DEFAULT_POLICY_PATH))
    executor = ScriptedMockExecutor({
        "19": {"status": "failed", "error": "timeout"},
        "48": {"status": "success"},
    })
    engine = ExecutionEngine(executor, policy)
    trace = engine.execute(
        request=type("DemoRequest", (), {"request_id": "DEMO-001"})(),
        decision={
            "request_id": "DEMO-001", "recommended_candidate": "19",
            "fallback_order": ["48"],
        },
        candidates={
            "19": {"candidate_id": "19"},
            "48": {"candidate_id": "48"},
        },
    )
    return {
        "demonstration_type": "offline demonstration only",
        "real_api_calls_performed": 0,
        "trace": trace,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_DEMO_OUTPUT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_demo()
        if not args.dry_run:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps({
            "status": "ok", "attempts": len(result["trace"]["attempts"]),
            "real_api_calls_performed": 0, "dry_run": args.dry_run,
        }, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
