"""Lifecycle compatibility API for the existing Formal Agent Skill Runtime."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from backend.agent_skill_error_contract import build_error_detail
from backend.formal_agent_skill_lifecycle_service import SkillLifecycleError


TOML_AGENT_ALLOWLIST = {
    Path(r"C:\Users\LX\.codex\agents\frontend-developer.toml").resolve()
}


def build_formal_agent_skill_lifecycle_router(
    lifecycle: Callable[[Request], Any], runtime: Callable[[Request], Any],
    secure_read: Callable[[Request], None], secure_mutation: Callable[[Request], None],
    principal_resolver: Callable[[Request], Any],
    host_service: Callable[[Request], Any] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/skills", tags=["formal-agent-skills-lifecycle"])

    def actor(request: Request) -> str:
        principal = principal_resolver(request)
        return str(getattr(principal, "principal_id", None) or "local-console-operator")

    def fail(exc: Exception, request: Request, *, skill_id: str | None = None,
             version: str | None = None) -> HTTPException:
        lifecycle_status = None
        installation_status = None
        resolved_version = version
        if skill_id:
            try:
                snapshot = lifecycle(request).get(skill_id)
                skill = snapshot.get("skill") or {}
                installation = snapshot.get("installation") or {}
                lifecycle_status = skill.get("version_status")
                installation_status = installation.get("installation_status")
                resolved_version = resolved_version or skill.get("version")
            except Exception:
                # Error responses must never be replaced by a secondary lookup failure.
                pass
        status, detail = build_error_detail(
            exc, skill_id=skill_id, version=resolved_version,
            lifecycle_status=lifecycle_status,
            installation_status=installation_status)
        return HTTPException(status, detail=detail)

    @router.get("")
    def list_skills(request: Request) -> dict[str, Any]:
        secure_read(request)
        return lifecycle(request).list()

    @router.get("/audit-records")
    def audit_records(request: Request, limit: int = 200) -> dict[str, Any]:
        secure_read(request)
        bounded = min(max(limit, 1), 500)
        lifecycle_rows = [
            {**row, "record_type": "lifecycle"}
            for row in lifecycle(request).audits(bounded)
        ]
        invocation_rows = [
            {**row, "record_type": "invocation",
             "created_at": row.get("started_at"),
             "operation": "invoke",
             "version": row.get("skill_version"),
             "result": row.get("execution_result")}
            for row in lifecycle(request).invocations(None, bounded)
        ]
        items = sorted(
            lifecycle_rows + invocation_rows,
            key=lambda row: str(row.get("created_at") or ""), reverse=True,
        )[:bounded]
        return {"items": items, "lifecycle_count": len(lifecycle_rows),
                "invocation_count": len(invocation_rows)}

    def hosts_for(request: Request) -> Any:
        if host_service is None:
            raise HTTPException(503, detail={"code": "host_not_configured",
                "message": "外部 Agent Host 控制面尚未配置。"})
        return host_service(request)

    @router.get("/hosts")
    def hosts(request: Request) -> dict[str, Any]:
        secure_read(request)
        return hosts_for(request).list_hosts()

    @router.post("/hosts")
    def create_host(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secure_mutation(request)
        try:
            return hosts_for(request).create_host(
                host_name=str(body.get("host_name") or ""),
                host_type=str(body.get("host_type") or "remote_agent"),
                base_url=str(body.get("base_url") or ""),
                environment=str(body.get("environment") or "china_uat"),
                protocol=str(body.get("protocol") or "formal-agent-skill"),
                protocol_version=str(body.get("protocol_version") or "1.0"),
                auth_type=str(body.get("auth_type") or "none"), operator_id=actor(request))
        except Exception as exc:
            raise fail(exc, request) from exc

    @router.get("/hosts/{host_id}")
    def get_host(host_id: str, request: Request) -> dict[str, Any]:
        secure_read(request)
        try: return hosts_for(request).get_host(host_id)
        except Exception as exc: raise fail(exc, request) from exc

    @router.patch("/hosts/{host_id}")
    def update_host(host_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secure_mutation(request)
        try: return hosts_for(request).update_host(host_id, operator_id=actor(request), **body)
        except Exception as exc: raise fail(exc, request) from exc

    @router.put("/hosts/{host_id}/credentials")
    def set_host_credentials(host_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secure_mutation(request)
        try:
            credential: Any = body.get("credential")
            if body.get("header_name"):
                credential = {"secret": credential, "header_name": body.get("header_name")}
            return hosts_for(request).set_credentials(
                host_id, credential, auth_type=str(body.get("auth_type") or "none"),
                operator_id=actor(request))
        except Exception as exc: raise fail(exc, request) from exc

    @router.delete("/hosts/{host_id}/credentials")
    def revoke_host_credentials(host_id: str, request: Request) -> dict[str, Any]:
        secure_mutation(request)
        try: return hosts_for(request).revoke_credentials(host_id, operator_id=actor(request))
        except Exception as exc: raise fail(exc, request) from exc

    @router.post("/hosts/{host_id}/test")
    def test_host(host_id: str, request: Request) -> dict[str, Any]:
        secure_mutation(request)
        try:
            result = hosts_for(request).test_connection(host_id, operator_id=actor(request))
            return {**result, "tls_result": result.get("tls"),
                    "authentication_result": result.get("authentication"), "safe_error": None}
        except Exception as exc: raise fail(exc, request) from exc

    @router.post("/hosts/{host_id}/enable")
    def enable_host(host_id: str, request: Request) -> dict[str, Any]:
        secure_mutation(request)
        try: return hosts_for(request).enable_host(host_id, operator_id=actor(request))
        except Exception as exc: raise fail(exc, request) from exc

    @router.post("/hosts/{host_id}/disable")
    def disable_host(host_id: str, request: Request) -> dict[str, Any]:
        secure_mutation(request)
        try: return hosts_for(request).disable_host(host_id, operator_id=actor(request))
        except Exception as exc: raise fail(exc, request) from exc

    @router.get("/hosts/{host_id}/capabilities")
    def host_capabilities(host_id: str, request: Request) -> dict[str, Any]:
        secure_read(request)
        try: return {"items": hosts_for(request).capabilities(host_id)}
        except Exception as exc: raise fail(exc, request) from exc

    @router.get("/hosts/{host_id}/installations")
    def host_installations(host_id: str, request: Request) -> dict[str, Any]:
        secure_read(request)
        try: return {"items": hosts_for(request).list_installations(host_id)}
        except Exception as exc: raise fail(exc, request) from exc

    @router.post("/hosts/{host_id}/installations")
    def install_on_host(host_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secure_mutation(request)
        try:
            return hosts_for(request).install_skill(
                host_id, str(body.get("skill_id") or ""), str(body.get("version") or ""),
                operator_id=actor(request))
        except Exception as exc: raise fail(exc, request, skill_id=str(body.get("skill_id") or ""),
                                            version=str(body.get("version") or "")) from exc

    @router.delete("/hosts/{host_id}/installations/{installation_id}")
    def uninstall_from_host(host_id: str, installation_id: str, request: Request) -> dict[str, Any]:
        secure_mutation(request)
        try:
            hosts_for(request).uninstall_skill(host_id, installation_id, operator_id=actor(request))
            return {"status": "uninstalled"}
        except Exception as exc: raise fail(exc, request) from exc

    @router.post("/hosts/{host_id}/installations/{installation_id}/{action}")
    def set_host_installation_state(host_id: str, installation_id: str, action: str,
                                    request: Request) -> dict[str, Any]:
        secure_mutation(request)
        if action not in {"enable", "disable"}:
            raise HTTPException(404, detail={"code": "unsupported_installation_action"})
        try:
            return hosts_for(request).set_installation_enabled(
                host_id, installation_id, action == "enable", operator_id=actor(request))
        except Exception as exc: raise fail(exc, request) from exc

    @router.post("/hosts/{host_id}/invoke")
    def invoke_on_host(host_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secure_mutation(request)
        skill_id = str(body.get("skill_id") or "")
        arguments = dict(body.get("arguments") or {})
        context: dict[str, Any] | None = None
        try:
            formal_runtime = runtime(request)
            context = formal_runtime.begin_managed_invocation(
                skill_id=skill_id, arguments=arguments, actor_id=actor(request),
                principal=principal_resolver(request))
            permissions = set(context["skill"].get("requested_permissions") or [])
            # The control plane owns the authenticated Host transport.  An
            # instruction-only Skill does not thereby receive arbitrary
            # external-network permission inside the remote runtime.
            if not permissions.issubset({"read", "local_compute"}):
                raise PermissionError("permission_denied")
            result = hosts_for(request).invoke_external(
                host_id, skill_id, str(context["skill"]["version"]), arguments,
                operator_id=actor(request), invocation_id=context["invocation_id"],
                audit_id=context["audit_id"])
            return formal_runtime.finish_managed_invocation(context, result, status="success")
        except Exception as exc:
            if context is not None:
                try:
                    runtime(request).finish_managed_invocation(
                        context, {"status": "failed", "error_code": str(getattr(exc, "code", exc)),
                                  "network_called": False, "write_performed": False}, status="failed")
                except Exception:
                    pass
            raise fail(exc, request, skill_id=skill_id) from exc

    @router.get("/hosts/{host_id}/invocations")
    def host_invocations(host_id: str, request: Request) -> dict[str, Any]:
        secure_read(request)
        try:
            items = hosts_for(request).invocations(host_id)
            return {"items": [{**item, "execution_result": item.get("result"),
                                "data": item.get("safe_result")} for item in items]}
        except Exception as exc: raise fail(exc, request) from exc

    @router.get("/hosts/{host_id}/audit")
    def host_audit(host_id: str, request: Request) -> dict[str, Any]:
        secure_read(request)
        try: return {"items": hosts_for(request).audits(host_id)}
        except Exception as exc: raise fail(exc, request) from exc

    @router.post("/import")
    async def import_skill(request: Request) -> dict[str, Any]:
        secure_mutation(request); body = await request.json()
        try:
            return lifecycle(request).import_external(
                skill_id=str(body.get("skill_id") or ""),
                display_name=str(body.get("display_name") or body.get("skill_id") or ""),
                description=str(body.get("description") or ""),
                source_type=str(body.get("source_type") or "github_skill_md"),
                source_uri=str(body.get("source_uri") or ""),
                content=str(body.get("content") or ""), operator_id=actor(request),
                repository_commit=body.get("repository_commit"),
                license_name=body.get("license_name"), scripts=list(body.get("scripts") or []),
                dependencies=list(body.get("dependencies") or []),
                references=list(body.get("references") or []))
        except Exception as exc: raise fail(exc, request, skill_id=str(body.get("skill_id") or "")) from exc

    @router.post("/import/toml")
    async def import_toml(request: Request) -> dict[str, Any]:
        secure_mutation(request); body = await request.json()
        path = Path(str(body.get("source_path") or "")).resolve()
        if path not in TOML_AGENT_ALLOWLIST:
            raise HTTPException(403, detail={"code": "toml_agent_source_not_allowed"})
        try:
            parsed = tomllib.loads(path.read_text("utf-8"))
            selected = {key: parsed.get(key) for key in
                        ("name", "description", "developer_instructions")}
            content = "\n\n".join(str(selected.get(key) or "") for key in selected)
            return lifecycle(request).import_external(
                skill_id="frontend-developer-toml", display_name=str(selected["name"] or "frontend-developer"),
                description=str(selected["description"] or "Codex TOML Agent 转换草稿"),
                source_type="codex_agent_toml", source_uri=str(path), content=content,
                operator_id=actor(request), toml_source=selected)
        except Exception as exc: raise fail(exc, request, skill_id="frontend-developer-toml") from exc

    @router.get("/{skill_id}")
    def get_skill(skill_id: str, request: Request) -> dict[str, Any]:
        secure_read(request)
        try: return lifecycle(request).get(skill_id)
        except Exception as exc: raise fail(exc, request, skill_id=skill_id) from exc

    @router.post("/{skill_id}/validate")
    def validate_skill(skill_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secure_mutation(request)
        try: return lifecycle(request).transition_version(
            skill_id, str(body.get("version") or "0.1.0"), "validate", actor(request))
        except Exception as exc: raise fail(exc, request, skill_id=skill_id,
                                            version=str(body.get("version") or "0.1.0")) from exc

    @router.post("/{skill_id}/versions/{version}/publish")
    def publish_skill(skill_id: str, version: str, request: Request) -> dict[str, Any]:
        secure_mutation(request)
        try: return lifecycle(request).transition_version(
            skill_id, version, "publish", actor(request))
        except Exception as exc: raise fail(exc, request, skill_id=skill_id, version=version) from exc

    @router.post("/{skill_id}/versions")
    def create_version(skill_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secure_mutation(request)
        try:
            return lifecycle(request).create_version(
                skill_id, version=str(body.get("version") or ""),
                content=str(body.get("content") or ""), operator_id=actor(request),
                change_summary=str(body.get("change_summary") or ""))
        except Exception as exc: raise fail(exc, request, skill_id=skill_id,
                                            version=str(body.get("version") or "") or None) from exc

    def install_operation(skill_id: str, request: Request, operation: str,
                          body: dict[str, Any] | None = None) -> dict[str, Any]:
        secure_mutation(request)
        try: return lifecycle(request).install_action(
            skill_id, operation, actor(request), version=(body or {}).get("version"))
        except Exception as exc: raise fail(exc, request, skill_id=skill_id,
                                            version=(body or {}).get("version")) from exc

    @router.post("/{skill_id}/install")
    def install(skill_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        return install_operation(skill_id, request, "install", body)
    @router.post("/{skill_id}/enable")
    def enable(skill_id: str, request: Request) -> dict[str, Any]:
        return install_operation(skill_id, request, "enable")
    @router.post("/{skill_id}/disable")
    def disable(skill_id: str, request: Request) -> dict[str, Any]:
        return install_operation(skill_id, request, "disable")
    @router.post("/{skill_id}/upgrade")
    def upgrade(skill_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        return install_operation(skill_id, request, "upgrade", body)
    @router.post("/{skill_id}/rollback")
    def rollback(skill_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        return install_operation(skill_id, request, "rollback", body)
    @router.delete("/{skill_id}/installations/{_installation_id}")
    def uninstall(skill_id: str, _installation_id: str, request: Request) -> dict[str, Any]:
        return install_operation(skill_id, request, "uninstall")

    @router.post("/{skill_id}/invoke")
    def invoke(skill_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        secure_mutation(request)
        try:
            return runtime(request).invoke_managed_from_agent(
                skill_id=skill_id, arguments=dict(body.get("arguments") or {}),
                actor_id=actor(request), principal=principal_resolver(request))
        except Exception as exc: raise fail(exc, request, skill_id=skill_id) from exc

    @router.get("/{skill_id}/invocations")
    def invocations(skill_id: str, request: Request, limit: int = 100) -> dict[str, Any]:
        secure_read(request)
        return {"items": lifecycle(request).invocations(skill_id, limit)}

    return router
