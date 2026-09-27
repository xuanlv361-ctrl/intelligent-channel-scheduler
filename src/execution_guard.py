"""Fail-closed execution authorization for Scheduler Runtime v1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from retry_policy import MAX_SAFE_ATTEMPTS


REQUIRED_CONFIG = {
    "runtime_version", "real_execution_enabled", "network_access_enabled",
    "real_api_adapter_enabled", "max_attempts", "fallback_enabled",
    "decision_log_enabled", "redact_sensitive_fields", "mock_execution_budget",
    "human_real_execution_authorization",
}

# Hard ceiling on total attempts per request, matching the base-tier spec
# ("基础版建议总尝试不超过2次"). Raising this requires a code change and
# review, not just a config edit.
@dataclass(frozen=True, slots=True)
class GuardDecision:
    status: str
    routing_allowed: bool
    execution_attempted: bool
    error_category: str | None


class ExecutionGuard:
    def __init__(self, config: Mapping[str, Any]):
        self.config = dict(config)

    def check(self, mode: str, candidate: Mapping[str, Any] | None, adapter: Any = None) -> GuardDecision:
        if not REQUIRED_CONFIG <= set(self.config):
            return GuardDecision("blocked", False, False, "runtime_configuration_incomplete")
        if mode not in {"simulation", "mock_execute", "real_shadow", "real_execute"}:
            return GuardDecision("blocked", False, False, "unknown_runtime_mode")
        # Network access, the real adapter, and real execution must stay
        # disabled unconditionally, independent of the attempt/fallback
        # configuration below. This is the actual safety line: bounded
        # mock-only fallback is allowed, any network path is not.
        if (
            self.config["network_access_enabled"]
            or self.config["real_api_adapter_enabled"]
            or self.config["real_execution_enabled"]
        ):
            return GuardDecision("blocked", False, False, "network_access_must_remain_disabled")
        max_attempts = self.config["max_attempts"]
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not (1 <= max_attempts <= MAX_SAFE_ATTEMPTS):
            return GuardDecision("blocked", False, False, "unsafe_attempt_configuration")
        fallback_enabled = self.config["fallback_enabled"]
        if fallback_enabled is True and max_attempts < 2:
            return GuardDecision("blocked", False, False, "fallback_requires_multiple_attempts")
        if fallback_enabled is False and max_attempts != 1:
            return GuardDecision("blocked", False, False, "unsafe_attempt_configuration")
        if fallback_enabled is not True and fallback_enabled is not False:
            return GuardDecision("blocked", False, False, "unsafe_attempt_configuration")
        if mode == "real_execute":
            return GuardDecision("blocked", False, False, "real_execution_not_authorized")
        if mode in {"simulation", "real_shadow"}:
            return GuardDecision("decision_only", False, False, None)
        if candidate is None or candidate.get("is_mock") not in {True, "TRUE"}:
            return GuardDecision("blocked", False, False, "mock_candidate_required")
        if adapter is None:
            return GuardDecision("blocked", False, False, "mock_adapter_missing")
        if not isinstance(self.config.get("mock_execution_budget"), Mapping):
            return GuardDecision("blocked", False, False, "mock_budget_missing")
        return GuardDecision("allowed", True, False, None)

    @staticmethod
    def check_uat(context: Mapping[str, Any]) -> GuardDecision:
        """Authorize the separately audited UAT path without weakening Runtime v1."""
        ordered_rules = (
            ("environment_supported", "environment_not_supported"),
            ("environment_configured", "environment_configuration_incomplete"),
            ("environment_execution_enabled", "environment_real_execution_disabled"),
            ("mode_valid", "invalid_execution_mode"),
            ("execution_enabled", "real_execution_disabled"),
            ("key_configured", "uat_key_not_configured"),
            ("host_allowed", "uat_host_not_allowed"),
            ("explicit_confirmation", "explicit_confirmation_required"),
            ("model_allowed", "model_not_allowed"),
            ("token_limit_ok", "max_tokens_limit_exceeded"),
            ("request_limit_ok", "daily_request_limit_reached"),
            ("budget_ok", "daily_budget_exceeded"),
            ("request_cost_ok", "single_request_cost_limit_exceeded"),
            ("input_safe", "sensitive_input_rejected"),
            ("stream_supported", "stream_execution_not_ready"),
            ("stop_rule_clear", "stop_rule_triggered"),
        )
        if (context.get("cancellation_requested") is True
                and context.get("cancellation_supported") is not True):
            return GuardDecision("blocked", False, False,
                                 "cancellation_not_ready")
        for field, error in ordered_rules:
            value = context.get(field, True) if field.startswith("environment_") else context.get(field)
            if value is not True:
                return GuardDecision("blocked", False, False, error)
        return GuardDecision("allowed", True, False, None)

    @staticmethod
    def check_formal_skill(context: Mapping[str, Any]) -> GuardDecision:
        """Authorize only registered, leased, local read-only Skill execution."""
        ordered_rules = (
            ("registered", "formal_skill_not_registered"),
            ("read_only", "formal_skill_write_forbidden"),
            ("operation_allowed", "formal_skill_operation_forbidden"),
            ("origin_allowed", "formal_skill_origin_rejected"),
            ("session_active", "formal_skill_session_invalid_or_expired"),
            ("lease_active", "formal_skill_lease_invalid_or_expired"),
            ("binding_static", "formal_skill_dynamic_binding_forbidden"),
            ("network_disabled", "formal_skill_network_path_forbidden"),
            ("path_safe", "formal_skill_path_escape"),
            ("bypass_absent", "formal_skill_bypass_attempt_rejected"),
        )
        for field, error in ordered_rules:
            if context.get(field) is not True:
                return GuardDecision("blocked", False, False, error)
        return GuardDecision("allowed", True, False, None)
