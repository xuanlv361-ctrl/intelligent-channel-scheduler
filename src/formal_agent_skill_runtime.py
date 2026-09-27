"""Schema-validated, audited execution runtime for formal governance Skills."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from formal_agent_skill_redaction import redact
from formal_agent_skill_registry import (
    FormalAgentSkillError, FormalAgentSkillRegistry, validate_schema,
)
from formal_agent_skill_security import FormalAgentSkillSecurity, SkillGrant

try:
    from backend.security.authorization import AuthorizationService
    from backend.security.principal import PrincipalContext
except ImportError:  # pragma: no cover
    AuthorizationService = Any  # type: ignore[misc,assignment]
    PrincipalContext = Any  # type: ignore[misc,assignment]


PROMPT_INJECTION = re.compile(
    r"(?i)(ignore (?:all |the )?(?:previous|system)|system prompt|execution.guard.bypass|x-skill-id|call\s+/api/|enable real execution)")

MANAGED_BINDING_PERMISSIONS = {
    "scheduler_attribution.read": "decision.read",
    "channel_health.read": "circuit.read",
    "model_cost.read": "price.read",
    "provider_error.read": "evidence.read",
    "acceptance_evidence.read": "evidence.read",
    "uat_request.validate": "uat.execute",
    "uat_request.execute": "uat.execute",
    "routing_policy.write": "scheduler.decide",
    "circuit_breaker.write": "circuit.manage",
    "traffic_proposal.write": "exploration.manage",
}


class FormalAgentSkillRuntime:
    def __init__(self, *, registry: FormalAgentSkillRegistry, security: FormalAgentSkillSecurity,
                 bindings: Any, audit: Any, lifecycle: Any = None,
                 managed_executor: Any = None):
        self.registry, self.security = registry, security
        self.bindings, self.audit = bindings, audit
        self.authorization_service = getattr(bindings, "authorization_service", None)
        self.lifecycle = lifecycle
        self.managed_executor = managed_executor

    @classmethod
    def build(cls, *, bindings: Any, audit: Any, registry: FormalAgentSkillRegistry | None = None,
              clock=None, precondition_provider=None, lifecycle=None,
              managed_executor=None):
        registry = registry or FormalAgentSkillRegistry()
        policy = registry.document["security"]
        kwargs = {"allowed_origins": set(policy["allowed_origins"]),
                  "allowed_skill_ids": set(registry.skills),
                  "grant_ttl_seconds": policy["grant_ttl_seconds"],
                  "session_ttl_seconds": policy["session_ttl_seconds"]}
        if clock is not None:
            kwargs["clock"] = clock
        if precondition_provider is not None:
            kwargs["precondition_provider"] = precondition_provider
        return cls(registry=registry, security=FormalAgentSkillSecurity(**kwargs),
                   bindings=bindings, audit=audit, lifecycle=lifecycle,
                   managed_executor=managed_executor)

    def invoke_managed_from_agent(self, *, skill_id: str,
                                  arguments: Mapping[str, Any],
                                  actor_id: str,
                                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Invoke a lifecycle-managed Skill through this Formal Runtime.

        This is a compatibility extension of the existing runtime: it shares
        the redaction and immutable audit trail, while the lifecycle catalog
        controls versions/installations and the executor delegates to existing
        domain services.
        """
        if self.lifecycle is None or self.managed_executor is None:
            raise FormalAgentSkillError("formal_skill_managed_runtime_unavailable")
        invocation_id = f"FSI-{uuid.uuid4()}"
        started_at = datetime.now(timezone.utc).isoformat()
        invocation = {
            "invocation_id": invocation_id, "skill_id": skill_id,
            "arguments": dict(arguments),
            "decision_id": str(arguments.get("decision_id") or "NO-DECISION"),
            "evidence_ids": [],
        }
        audit_id = ""
        skill: Mapping[str, Any] | None = None
        try:
            if self._contains_injection(arguments):
                raise FormalAgentSkillError("formal_skill_prompt_injection_rejected")
            skill = self.lifecycle.prepare_invocation(skill_id, arguments)
            required_permission = MANAGED_BINDING_PERMISSIONS.get(skill["binding"])
            if required_permission is None:
                if skill["binding"] != "instruction_only":
                    raise FormalAgentSkillError("formal_skill_binding_permission_unmapped")
            elif self.authorization_service is not None:
                self.authorization_service.authorize(principal, required_permission)
            elif principal is not None:
                raise FormalAgentSkillError("permission_denied")
            claims = {"actor_id": actor_id, "origin": "local-agent://managed-runtime"}
            audit_id = self.audit.begin(
                invocation, claims, skill["binding"], principal=principal)
            raw = self.managed_executor(
                skill_id=skill_id, arguments=dict(arguments), principal=principal,
                skill=skill)
            safe, redactions = redact(raw)
            if not isinstance(safe, Mapping):
                safe = {"value": safe}
            status = str(safe.get("status") or "success")
            if status not in {"success", "conditions_validated", "failed", "not_found", "no_real_data",
                              "insufficient_evidence", "permission_denied"}:
                status = "success"
            result = {
                "invocation_id": invocation_id, "audit_id": audit_id,
                "operator_id": actor_id, "skill_id": skill_id,
                "skill_version": skill["version"], "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "permission_decision": "allowed", "execution_result": status,
                "data": dict(safe), "uncertainty": self._uncertainty(safe),
                "network_called": bool(safe.get("network_called")),
                "write_performed": bool(safe.get("write_performed")),
                "redaction_count": redactions,
            }
            self.audit.finish(audit_id, status=status, result=result,
                              network_called=result["network_called"],
                              principal=principal)
            self.lifecycle.record_invocation(
                invocation_id=invocation_id, audit_id=audit_id,
                operator_id=actor_id, skill=skill, started_at=started_at,
                result=result["data"], permission_decision="allowed",
                execution_result=status, arguments=arguments)
            return result
        except Exception as exc:
            safe_error, _ = redact(str(exc))
            if audit_id:
                self.audit.finish(audit_id, status="blocked",
                                  result={"error": safe_error}, principal=principal)
                # Once lifecycle and permission checks have succeeded, a
                # managed execution failure is still an invocation and must
                # remain queryable after refresh/restart.  Pre-lifecycle
                # denials remain in the immutable formal audit only because
                # there is no installed version that can authoritatively own
                # an invocation row.
                if skill is not None:
                    try:
                        self.lifecycle.record_invocation(
                            invocation_id=invocation_id, audit_id=audit_id,
                            operator_id=actor_id, skill=skill,
                            started_at=started_at,
                            result={"status": "failed", "error_code": safe_error,
                                    "network_called": False,
                                    "write_performed": False},
                            permission_decision="allowed",
                            execution_result="failed", arguments=arguments)
                    except Exception:
                        # The immutable audit above is the primary evidence;
                        # persistence cleanup must never replace the original
                        # managed execution error.
                        pass
            else:
                self.audit.deny(invocation, exc)
            raise

    def begin_managed_invocation(self, *, skill_id: str,
                                 arguments: Mapping[str, Any], actor_id: str,
                                 principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Authorize and open a managed invocation for streaming transports.

        Streaming delivery remains owned by the existing UAT workbench, while
        permission, lifecycle and audit ownership stays in this Formal Runtime.
        The returned context contains no credential or request header values.
        """
        if self.lifecycle is None:
            raise FormalAgentSkillError("formal_skill_managed_runtime_unavailable")
        invocation_id = f"FSI-{uuid.uuid4()}"
        started_at = datetime.now(timezone.utc).isoformat()
        invocation = {
            "invocation_id": invocation_id, "skill_id": skill_id,
            "arguments": dict(arguments),
            "decision_id": str(arguments.get("decision_id") or "NO-DECISION"),
            "evidence_ids": [],
        }
        try:
            if self._contains_injection(arguments):
                raise FormalAgentSkillError(
                    "formal_skill_prompt_injection_rejected")
            skill = self.lifecycle.prepare_invocation(skill_id, arguments)
            required_permission = MANAGED_BINDING_PERMISSIONS.get(skill["binding"])
            if required_permission is None:
                if skill["binding"] != "instruction_only":
                    raise FormalAgentSkillError(
                        "formal_skill_binding_permission_unmapped")
            elif self.authorization_service is not None:
                self.authorization_service.authorize(principal, required_permission)
            elif principal is not None:
                raise FormalAgentSkillError("permission_denied")
        except Exception as exc:
            self.audit.deny(invocation, exc)
            # Lifecycle and schema failures are part of the public managed
            # contract; do not collapse them into a permission failure.
            if isinstance(exc, FormalAgentSkillError):
                raise
            if exc.__class__.__name__ == "SkillLifecycleError":
                raise
            raise FormalAgentSkillError("permission_denied") from exc
        audit_id = self.audit.begin(
            invocation, {"actor_id": actor_id,
                         "origin": "local-agent://managed-runtime"},
            skill["binding"], principal=principal)
        return {"invocation_id": invocation_id, "audit_id": audit_id,
                "operator_id": actor_id, "skill": skill,
                "started_at": started_at, "arguments": dict(arguments),
                "principal": principal}

    def finish_managed_invocation(self, context: Mapping[str, Any],
                                  raw_result: Mapping[str, Any], *,
                                  status: str = "success") -> dict[str, Any]:
        """Close and persist a managed streaming invocation."""
        safe, redactions = redact(raw_result)
        if not isinstance(safe, Mapping):
            safe = {"value": safe}
        bounded_status = status if status in {
            "success", "failed", "not_found", "no_real_data",
            "insufficient_evidence", "permission_denied",
        } else "failed"
        result = {
            "invocation_id": context["invocation_id"],
            "audit_id": context["audit_id"],
            "operator_id": context["operator_id"],
            "skill_id": context["skill"]["skill_id"],
            "skill_version": context["skill"]["version"],
            "started_at": context["started_at"],
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "permission_decision": "allowed",
            "execution_result": bounded_status, "data": dict(safe),
            "uncertainty": self._uncertainty(safe),
            "network_called": bool(safe.get("network_called")),
            "write_performed": bool(safe.get("write_performed")),
            "redaction_count": redactions,
        }
        self.audit.finish(context["audit_id"], status=bounded_status,
                          result=result,
                          network_called=result["network_called"],
                          principal=context.get("principal"))
        self.lifecycle.record_invocation(
            invocation_id=context["invocation_id"],
            audit_id=context["audit_id"], operator_id=context["operator_id"],
            skill=context["skill"], started_at=context["started_at"],
            result=result["data"], permission_decision="allowed",
            execution_result=bounded_status, arguments=context["arguments"])
        return result

    def discover(self) -> list[dict[str, Any]]:
        return self.registry.discover()

    @staticmethod
    def _contains_injection(value: Any) -> bool:
        if isinstance(value, str):
            return bool(PROMPT_INJECTION.search(value))
        if isinstance(value, Mapping):
            return any(FormalAgentSkillRuntime._contains_injection(v) for v in value.values())
        if isinstance(value, list):
            return any(FormalAgentSkillRuntime._contains_injection(v) for v in value)
        return False

    @staticmethod
    def _uncertainty(data: Mapping[str, Any]) -> str:
        observed: set[str] = set()
        state_keys = {
            "state", "status", "confidence_state", "freshness_status",
            "verification_status", "availability",
        }
        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    if str(key).casefold() in state_keys and isinstance(item, str):
                        observed.add(item.casefold())
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(data)
        if "conflicting" in observed:
            return "conflicting"
        if "stale" in observed or "expired" in observed:
            return "stale"
        if observed & {"insufficient", "insufficient_evidence", "blocked_insufficient_data"}:
            return "insufficient_evidence"
        if observed & {"unknown", "pending_confirmation", "unavailable"} or not data:
            return "unknown"
        return "known"

    def invoke(self, invocation: Mapping[str, Any], *, grant: SkillGrant,
               principal: PrincipalContext | None = None) -> dict[str, Any]:
        value = dict(invocation)
        try:
            validate_schema(value, self.registry.invocation_schema)
            identifiers = [value["invocation_id"], value["decision_id"], *value["evidence_ids"]]
            if any(redact(identifier)[1] for identifier in identifiers):
                raise FormalAgentSkillError("formal_skill_sensitive_identifier_rejected")
            skill = self.registry.get(value["skill_id"])
            self.registry.validate_arguments(value["skill_id"], value["arguments"])
            if self._contains_injection(value["arguments"]):
                raise FormalAgentSkillError("formal_skill_prompt_injection_rejected")
            claims = self.security.validate_grant(
                grant, skill_id=value["skill_id"], binding=skill["binding"])
            if self.authorization_service is not None:
                permission = self.bindings.PERMISSIONS[skill["binding"]]
                self.authorization_service.authorize(principal, permission)
        except Exception as exc:
            self.audit.deny(value, exc)
            raise
        audit_id = self.audit.begin(
            value, claims, skill["binding"], principal=principal)
        try:
            raw, binding_claims = self.bindings.invoke(
                skill_id=value["skill_id"], binding=skill["binding"],
                arguments=value["arguments"], grant=grant,
                security=self.security, principal=principal)
            # Binding authorization is intentionally repeated so a direct
            # binding call cannot use a valid Skill grant as an RBAC bypass.
            safe, redactions = redact(raw)
            if not isinstance(safe, Mapping):
                safe = {"value": safe}
            if safe.get("network_called") is not False:
                raise FormalAgentSkillError("formal_skill_network_path_forbidden")
            validate_schema(safe, skill["output_schema"], "$.data")
            status = "unavailable" if safe.get("availability") == "unavailable" else "success"
            result = {
                "invocation_id": value["invocation_id"], "skill_id": value["skill_id"],
                "status": status, "data": dict(safe), "uncertainty": self._uncertainty(safe),
                "provenance": {"registry_version": self.registry.document["registry_version"],
                               "skill_version": skill["version"],
                               "artifact_sha256": skill["artifact_sha256"],
                               "decision_id": value["decision_id"],
                               "evidence_ids": value["evidence_ids"],
                               "binding": skill["binding"], "redaction_count": redactions,
                               "precondition_snapshot_sha256": binding_claims[
                                   "precondition_sha256"],
                               "precondition_versions": {
                                   "budget": binding_claims["budget_policy_version"],
                                   "circuit": binding_claims["circuit_policy_version"],
                                   "capability": binding_claims[
                                       "capability_evidence_version"],
                                   "kill_switch": binding_claims["kill_switch_version"],
                               },
                               "provider_output_trust": "untrusted_data"},
                "limitations": ["local read-only governance evidence", "no network or mutation"],
                "audit_id": audit_id, "network_called": False,
            }
            validate_schema(result, self.registry.result_schema)
            self.audit.finish(
                audit_id, status=status, result=result, principal=principal)
            return result
        except Exception as exc:
            safe_error, _ = redact(str(exc))
            self.audit.finish(
                audit_id, status="blocked", result={"error": safe_error},
                principal=principal)
            raise

    def invoke_from_agent(self, *, skill_id: str, arguments: Mapping[str, Any],
                          decision_id: str, evidence_ids: list[str] | None = None,
                          actor_id: str = "formal-agent-runtime",
                          principal: PrincipalContext | None = None) -> dict[str, Any]:
        skill = self.registry.get(skill_id)
        environment = str(arguments.get("environment_id", "local"))
        invocation = {
            "invocation_id": f"FSI-{uuid.uuid4()}", "skill_id": skill_id,
            "arguments": dict(arguments), "decision_id": decision_id,
            "evidence_ids": list(evidence_ids or []),
        }
        try:
            session = self.security.open_session(
                actor_id=actor_id, origin="local-agent://runtime")
            lease = self.security.acquire_lease(
                session_id=session, skill_id=skill_id, environment_id=environment)
            grant = self.security.issue_grant(
                session_id=session, lease_id=lease, skill_id=skill_id,
                binding=skill["binding"])
        except Exception as exc:
            self.audit.deny(invocation, exc)
            raise
        return self.invoke(invocation, grant=grant, principal=principal)

    def select_for_task(self, task: str) -> list[str]:
        if self._contains_injection(task):
            return []
        text = task.casefold()
        return [row["skill_id"] for row in self.registry.discover()
                if any(trigger.casefold() in text for trigger in row["triggers"])]
