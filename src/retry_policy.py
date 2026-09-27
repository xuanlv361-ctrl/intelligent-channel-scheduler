"""Fail-closed retry classification for the local recovery layer."""

from __future__ import annotations

import math
from typing import Any, Mapping


MAX_SAFE_ATTEMPTS = 2


class RetryPolicyError(ValueError):
    pass


class RetryPolicy:
    def __init__(self, config: Mapping[str, Any]):
        self.policy_version = str(config["policy_version"])
        configured_attempts = config["max_attempts"]
        if isinstance(configured_attempts, bool) or not isinstance(
                configured_attempts, int):
            raise RetryPolicyError("max_attempts must be an integer")
        self.max_attempts = configured_attempts
        if not 1 <= self.max_attempts <= MAX_SAFE_ATTEMPTS:
            raise RetryPolicyError(
                f"max_attempts must be between 1 and {MAX_SAFE_ATTEMPTS}")
        self.retryable_errors = {str(value) for value in config["retryable_errors"]}
        self.non_retryable_errors = {
            str(value) for value in config["non_retryable_errors"]
        }
        if self.retryable_errors & self.non_retryable_errors:
            raise RetryPolicyError("retryable and non-retryable errors overlap")
        self.per_attempt_timeout_seconds = float(
            config.get("per_attempt_timeout_seconds", 30.0))
        self.total_deadline_seconds = float(
            config.get("total_deadline_seconds",
                       self.per_attempt_timeout_seconds * self.max_attempts))
        self.max_retry_after_seconds = float(
            config.get("max_retry_after_seconds", 2.0))
        if (not math.isfinite(self.per_attempt_timeout_seconds)
                or self.per_attempt_timeout_seconds <= 0):
            raise RetryPolicyError("per_attempt_timeout_seconds must be positive")
        if (not math.isfinite(self.total_deadline_seconds)
                or self.total_deadline_seconds <= 0):
            raise RetryPolicyError("total_deadline_seconds must be positive")
        if (not math.isfinite(self.max_retry_after_seconds)
                or self.max_retry_after_seconds < 0):
            raise RetryPolicyError("max_retry_after_seconds must be non-negative")
        confirmed = config.get("confirmed_transient_errors", self.retryable_errors)
        self.confirmed_transient_errors = {str(value) for value in confirmed}
        if not self.confirmed_transient_errors <= self.retryable_errors:
            raise RetryPolicyError(
                "confirmed transient errors must be retryable errors")
        self.automatic_real_execution_authorized = bool(
            config.get("automatic_real_execution_authorized", False)
        )
        if self.automatic_real_execution_authorized:
            raise RetryPolicyError("real execution must remain unauthorized")

    def should_retry(self, error: str | int | None, attempt_number: int) -> bool:
        return self.retry_decision(error, attempt_number)["allowed"]

    def retry_decision(
        self, error: str | int | None, attempt_number: int, *,
        retry_after_seconds: float | int | str | None = None,
        remaining_seconds: float | None = None,
        output_started: bool = False,
    ) -> dict[str, Any]:
        """Return a reasoned retry decision without sleeping or doing I/O."""
        if output_started:
            return {"allowed": False, "delay_seconds": 0.0,
                    "reason": "output_started_fallback_forbidden"}
        if attempt_number >= self.max_attempts:
            return {"allowed": False, "delay_seconds": 0.0,
                    "reason": "attempt_limit_reached"}
        if error is None:
            return {"allowed": False, "delay_seconds": 0.0,
                    "reason": "error_missing"}
        normalized = str(error)
        if normalized in self.non_retryable_errors:
            return {"allowed": False, "delay_seconds": 0.0,
                    "reason": "non_retryable_error"}
        if normalized not in self.confirmed_transient_errors:
            return {"allowed": False, "delay_seconds": 0.0,
                    "reason": "transient_error_not_confirmed"}
        delay = 0.0
        if retry_after_seconds not in (None, ""):
            try:
                delay = float(retry_after_seconds)
            except (TypeError, ValueError):
                return {"allowed": False, "delay_seconds": 0.0,
                        "reason": "retry_after_invalid"}
            if (not math.isfinite(delay) or delay < 0
                    or delay > self.max_retry_after_seconds):
                return {"allowed": False, "delay_seconds": 0.0,
                        "reason": "retry_after_out_of_bounds"}
        if remaining_seconds is not None and delay >= max(0.0, remaining_seconds):
            return {"allowed": False, "delay_seconds": 0.0,
                    "reason": "retry_after_exceeds_deadline"}
        return {"allowed": True, "delay_seconds": delay,
                "reason": "confirmed_transient_error"}
