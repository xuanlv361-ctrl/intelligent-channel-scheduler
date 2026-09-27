"""Fail-closed bridge from F/G/H governance state to Scheduler eligibility."""

from __future__ import annotations

from typing import Any, Mapping

from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext, PrincipalType


class SchedulerGovernanceBridge:
    """Read-only admission projection; it never approves or mutates governance."""

    OPERATIONS = frozenset({"traffic_change", "probe", "high_cost_test"})

    def __init__(self, *, traffic_change: Any = None, probe: Any = None,
                 high_cost_test: Any = None,
                 authorization_service: AuthorizationService | None = None):
        self.traffic_change = traffic_change
        self.probe = probe
        self.high_cost_test = high_cost_test
        self.authorization_service = authorization_service

    def _authorize(self, principal: PrincipalContext | None) -> PrincipalContext | None:
        if self.authorization_service is None:
            return None
        verified = self.authorization_service.authorize(
            principal, "scheduler.decide", principal_type=PrincipalType.SERVICE)
        if "scheduler_service" not in verified.roles:
            raise PermissionError("scheduler_service_identity_required")
        return verified

    @staticmethod
    def _read(method: Any, identifier: str,
              principal: PrincipalContext | None) -> Mapping[str, Any]:
        import inspect
        kwargs = {}
        if "principal" in inspect.signature(method).parameters:
            kwargs["principal"] = principal
        return method(identifier, **kwargs)

    @staticmethod
    def _blocked(operation: str, reason: str, capability_id: str) -> dict[str, Any]:
        return {"operation": operation, "allowed": False, "reason": reason,
                "capability_id": capability_id, "fail_closed": True,
                "real_execution_allowed": False, "network_called": False}

    def evaluate(self, *, request: Any, candidate: Mapping[str, Any],
                 principal: PrincipalContext | None = None) -> dict[str, Any]:
        principal = self._authorize(principal)
        metadata = dict(request.metadata or {})
        operation = str(metadata.get("governance_operation") or "")
        if not operation:
            return {"operation": "standard_routing", "allowed": True,
                    "reason": None, "capability_id": None,
                    "real_execution_allowed": False, "network_called": False}
        if operation not in self.OPERATIONS:
            return self._blocked(operation, "unknown_governance_operation", "unknown")
        environment = str(metadata.get("environment_id") or "")
        channel = str(candidate.get("channel_id") or candidate.get("candidate_id") or "")
        model = str(request.requested_model)
        try:
            if operation == "traffic_change":
                capability = "ADV-017"
                if self.traffic_change is None:
                    return self._blocked(operation, "traffic_change_service_unavailable", capability)
                state = self._read(self.traffic_change.get, str(
                    metadata.get("traffic_change_proposal_id") or ""), principal)
                valid = (state.get("state") == "ACTIVE"
                         and state.get("environment_id") == environment
                         and str(state.get("channel_id")) == channel
                         and state.get("model_id") == model
                         and state.get("real_execution_allowed") is False)
                reason = None if valid else "traffic_change_scope_or_state_invalid"
                version = state.get("policy_version")
            elif operation == "probe":
                capability = "ADV-018"
                if self.probe is None:
                    return self._blocked(operation, "probe_service_unavailable", capability)
                state = self._read(
                    self.probe.get_lease,
                    str(metadata.get("probe_lease_id") or ""), principal)
                valid = (state.get("state") == "ACTIVE"
                         and state.get("environment_id") == environment
                         and str(state.get("channel_id")) == channel
                         and state.get("model_id") == model)
                reason = None if valid else "probe_scope_or_lease_invalid"
                version = state.get("policy_version")
            else:
                capability = "ADV-019"
                if self.high_cost_test is None:
                    return self._blocked(operation, "high_cost_service_unavailable", capability)
                state = self._read(self.high_cost_test.get, str(
                    metadata.get("high_cost_test_id") or ""), principal)
                valid = (state.get("state") == "RUNNING"
                         and state.get("environment_id") == environment
                         and state.get("model_id") == model
                         and state.get("real_execution_allowed") is False)
                reason = None if valid else "high_cost_scope_or_state_invalid"
                version = state.get("policy_version")
        except Exception:
            return self._blocked(operation, f"{operation}_evidence_unavailable", capability)
        if not valid:
            return self._blocked(operation, reason or "governance_denied", capability)
        if principal is not None and (
            state.get("tenant_id") != principal.tenant_id
            or state.get("workspace_id") != principal.workspace_id
        ):
            return self._blocked(operation, "governance_tenant_scope_mismatch", capability)
        return {"operation": operation, "allowed": True, "reason": None,
                "capability_id": capability, "policy_version": version,
                "authorization_scope": "offline_mock_only",
                "real_execution_allowed": False, "network_called": False}
