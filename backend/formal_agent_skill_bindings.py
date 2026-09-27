"""Static read-only bindings from formal Skills to existing domain services."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from formal_agent_skill_registry import FormalAgentSkillError
from formal_agent_skill_security import FormalAgentSkillSecurity, SkillGrant

from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext


ALLOWED_BINDINGS = frozenset({
    "channel_health.read", "statistical_confidence.read",
    "scheduler_attribution.read", "sticky_routing.read",
    "circuit_breaker.read", "exploration_governance.read",
    "capability_evidence.read", "cost_configuration.read",
})


class FormalAgentSkillBindings:
    PERMISSIONS = {
        "channel_health.read": "snapshot.read",
        "statistical_confidence.read": "snapshot.read",
        "scheduler_attribution.read": "decision.read",
        "sticky_routing.read": "decision.read",
        "circuit_breaker.read": "circuit.read",
        "exploration_governance.read": "exploration.read",
        "capability_evidence.read": "evidence.read",
        "cost_configuration.read": "price.read",
    }

    def __init__(self, providers: Mapping[str, Callable[..., Any]], *,
                 authorization_service: AuthorizationService | None = None):
        unknown = set(providers) - ALLOWED_BINDINGS
        if unknown or any(not callable(value) for value in providers.values()):
            raise FormalAgentSkillError("formal_skill_binding_configuration_invalid")
        self._providers = dict(providers)
        self.authorization_service = authorization_service

    @staticmethod
    def _invoke_provider(provider: Callable[..., Any], arguments: Mapping[str, Any],
                         principal: PrincipalContext | None) -> Any:
        import inspect
        try:
            supports_principal = "principal" in inspect.signature(provider).parameters
        except (TypeError, ValueError):
            supports_principal = False
        return (provider(arguments, principal=principal)
                if supports_principal else provider(arguments))

    @classmethod
    def from_services(cls, *, metrics: Any = None, confidence: Any = None,
                      attribution: Any = None, sticky: Any = None,
                      circuit: Any = None, exploration: Any = None,
                      capability: Any = None,
                      cost_configuration: Callable[[], Mapping[str, Any]] | None = None,
                      authorization_service: AuthorizationService | None = None):
        providers: dict[str, Callable[..., Any]] = {}
        def local(value: Any) -> dict[str, Any]:
            result = dict(value) if isinstance(value, Mapping) else {"items": list(value)}
            result.setdefault("network_called", False)
            return result
        def scoped(method: Callable[..., Any], *args: Any,
                   principal: PrincipalContext | None = None, **kwargs: Any) -> Any:
            import inspect
            try:
                if "principal" in inspect.signature(method).parameters:
                    kwargs["principal"] = principal
            except (TypeError, ValueError):
                pass
            return method(*args, **kwargs)
        if metrics is not None:
            providers["channel_health.read"] = lambda args: local(metrics.list_snapshots(
                environment_id=args.get("environment_id"), limit=args.get("limit", 100)))
        if confidence is not None:
            providers["statistical_confidence.read"] = lambda args: local(confidence.list_snapshots(
                limit=args.get("limit", 100)))
        if attribution is not None:
            providers["scheduler_attribution.read"] = lambda args, principal=None: local(
                scoped(attribution.chain, args["decision_id"], principal=principal))
        if sticky is not None:
            providers["sticky_routing.read"] = lambda args, principal=None: local(
                scoped(sticky.status, principal=principal))
        if circuit is not None:
            providers["circuit_breaker.read"] = lambda args, principal=None: (
                {"state": scoped(circuit.get_state, args["circuit_id"], principal=principal),
                 "transitions": scoped(circuit.list_transition_audits,
                     args["circuit_id"], principal=principal),
                 "network_called": False}
                if args.get("circuit_id") else scoped(circuit.list_states,
                    limit=args.get("limit", 100), principal=principal))
        if exploration is not None:
            providers["exploration_governance.read"] = lambda args, principal=None: local(
                scoped(exploration.status, principal=principal))
        if capability is not None:
            providers["capability_evidence.read"] = lambda args, principal=None: {
                "items": scoped(capability.list_evidence,
                    subject_id=args.get("subject_id"), limit=args.get("limit", 100),
                    principal=principal),
                "network_called": False,
            }
        if cost_configuration is not None:
            providers["cost_configuration.read"] = lambda args: local(cost_configuration())
        return cls(providers, authorization_service=authorization_service)

    def invoke(self, *, skill_id: str, binding: str, arguments: Mapping[str, Any],
               grant: SkillGrant, security: FormalAgentSkillSecurity,
               principal: PrincipalContext | None = None) -> tuple[dict[str, Any], dict]:
        if binding not in ALLOWED_BINDINGS or not binding.endswith(".read"):
            raise FormalAgentSkillError("formal_skill_binding_forbidden")
        claims = security.consume_grant(grant, skill_id=skill_id, binding=binding)
        required_preconditions = {
            "precondition_sha256", "budget_policy_version", "circuit_policy_version",
            "capability_evidence_version", "kill_switch_version",
        }
        if not required_preconditions.issubset(claims):
            raise FormalAgentSkillError("formal_skill_precondition_claims_missing")
        if self.authorization_service is not None:
            self.authorization_service.authorize(
                principal, self.PERMISSIONS[binding])
        provider = self._providers.get(binding)
        if provider is None:
            return {"availability": "unavailable", "network_called": False}, claims
        result = self._invoke_provider(provider, dict(arguments), principal)
        if not isinstance(result, Mapping):
            result = {"items": list(result) if isinstance(result, list) else result}
        return dict(result), claims
