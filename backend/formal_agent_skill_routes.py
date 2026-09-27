"""Origin-guarded, read-only HTTP discovery for formal Agent Skills."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.formal_agent_skill_audit_service import FormalAgentSkillAuditService
from formal_agent_skill_registry import FormalAgentSkillRegistry
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext


HOST_DEPLOYMENT_STATUS = "repository_harness_only"


class FormalSkillDiscoveryItem(BaseModel):
    model_config = {"extra": "forbid"}

    skill_id: str
    version: str
    binding: str
    read_only: Literal[True]
    artifact_sha256: str


class FormalSkillDiscoveryResponse(BaseModel):
    model_config = {"extra": "forbid"}

    registry_version: str
    skill_count: int
    skills: list[FormalSkillDiscoveryItem]
    host_deployment_status: Literal["repository_harness_only"]
    network_called: Literal[False]


class FormalSkillStatusResponse(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["ready", "degraded", "unavailable"]
    registered_skill_count: int
    browser_invocation_allowed: Literal[False]
    grant_issuance_allowed: Literal[False]
    host_deployment_status: Literal["repository_harness_only"]
    network_called: Literal[False]


class FormalSkillAuditSummaryResponse(BaseModel):
    model_config = {"extra": "forbid"}

    total_events: int
    latest_event_type: str | None
    host_deployment_status: Literal["repository_harness_only"]
    network_called: Literal[False]


class FormalSkillInvocationBody(BaseModel):
    model_config = {"extra": "forbid"}

    skill_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    decision_id: str
    evidence_ids: list[str] = Field(default_factory=list)


class FormalSkillInvocationResponse(BaseModel):
    model_config = {"extra": "forbid"}

    invocation_id: str
    skill_id: str
    status: Literal["success", "unavailable"]
    data: dict[str, Any]
    uncertainty: str
    provenance: dict[str, Any]
    limitations: list[str]
    audit_id: str
    network_called: Literal[False]


def build_formal_agent_skill_router(
    registry: FormalAgentSkillRegistry,
    audit: FormalAgentSkillAuditService | Callable[[], FormalAgentSkillAuditService],
    runtime_status: Mapping[str, Any] | Callable[[], Mapping[str, Any]],
    secure_read_only_request: Callable[[Request], None],
    *,
    runtime: Any | Callable[[Request], Any] | None = None,
    secure_mutation_request: Callable[[Request], None] | None = None,
    authorization_service: AuthorizationService | None = None,
    principal_resolver: Callable[[Request], PrincipalContext] | None = None,
) -> APIRouter:
    """Build metadata routes and, when configured, a local read-only harness."""

    def require_approved_read_origin(request: Request) -> None:
        secure_read_only_request(request)

    def principal_for(request: Request) -> PrincipalContext | None:
        if authorization_service is None:
            return None
        if principal_resolver is None:
            raise PermissionError("principal_resolver_required")
        return principal_resolver(request)

    def runtime_for(request: Request) -> Any:
        if runtime is None:
            raise HTTPException(404, detail={"code": "formal_skill_runtime_unavailable"})
        return runtime(request) if callable(runtime) else runtime

    def require_mutation_origin(request: Request) -> None:
        if secure_mutation_request is None:
            raise HTTPException(404, detail={"code": "formal_skill_runtime_unavailable"})
        secure_mutation_request(request)

    router = APIRouter(prefix="/api/v1/formal-agent-skills", tags=["formal-agent-skills"])

    @router.get("/discovery", response_model=FormalSkillDiscoveryResponse,
                dependencies=[Depends(require_approved_read_origin)])
    def discovery(request: Request) -> dict[str, Any]:
        principal = principal_for(request)
        if authorization_service is not None:
            authorization_service.authorize(principal, "evidence.read")
        skills = [
            {
                key: row[key]
                for key in ("skill_id", "version", "binding", "read_only", "artifact_sha256")
            }
            for row in registry.skills.values()
        ]
        return {
            "registry_version": str(registry.document["registry_version"]),
            "skill_count": len(skills),
            "skills": skills,
            "host_deployment_status": HOST_DEPLOYMENT_STATUS,
            "network_called": False,
        }

    @router.get("/status", response_model=FormalSkillStatusResponse,
                dependencies=[Depends(require_approved_read_origin)])
    def status(request: Request) -> dict[str, Any]:
        principal = principal_for(request)
        if authorization_service is not None:
            authorization_service.authorize(principal, "evidence.read")
        supplied = runtime_status() if callable(runtime_status) else runtime_status
        safe_status = supplied.get("status") if isinstance(supplied, Mapping) else None
        if safe_status not in {"ready", "degraded", "unavailable"}:
            safe_status = "unavailable"
        return {
            "status": safe_status,
            "registered_skill_count": len(registry.skills),
            "browser_invocation_allowed": False,
            "grant_issuance_allowed": False,
            "host_deployment_status": HOST_DEPLOYMENT_STATUS,
            "network_called": False,
        }

    @router.get("/audit-summary", response_model=FormalSkillAuditSummaryResponse,
                dependencies=[Depends(require_approved_read_origin)])
    def audit_summary(request: Request) -> dict[str, Any]:
        principal = principal_for(request)
        if authorization_service is not None:
            authorization_service.authorize(principal, "audit.read")
        current_audit = audit() if callable(audit) else audit
        return {
            **current_audit.safe_summary(principal=principal),
            "host_deployment_status": HOST_DEPLOYMENT_STATUS,
            "network_called": False,
        }

    if runtime is not None:
        @router.post("/invoke-local", response_model=FormalSkillInvocationResponse,
                     dependencies=[Depends(require_mutation_origin)])
        def invoke_local(body: FormalSkillInvocationBody, request: Request) -> dict[str, Any]:
            principal = principal_for(request)
            if authorization_service is not None:
                authorization_service.authorize(principal, "evidence.read")
            try:
                return runtime_for(request).invoke_from_agent(
                    skill_id=body.skill_id,
                    arguments=body.arguments,
                    decision_id=body.decision_id,
                    evidence_ids=body.evidence_ids,
                    actor_id=(principal.principal_id if principal is not None
                              else "local-console-operator"),
                    principal=principal,
                )
            except Exception as exc:
                raise HTTPException(409, detail={
                    "code": str(exc),
                    "message": "Agent Skill 本地只读调用被安全策略阻止。",
                    "network_called": False,
                }) from exc

    return router
