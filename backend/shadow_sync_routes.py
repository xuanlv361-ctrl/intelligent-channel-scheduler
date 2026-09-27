from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.shadow_sync_service import ShadowSyncService


class SourceConfigUpdate(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool


class ManualCorrelationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    execution_id: str


def build_shadow_sync_router(service: ShadowSyncService, secure_mutation) -> APIRouter:
    router = APIRouter(prefix="/api/v1/shadow", tags=["shadow-sync"])

    def principal(request: Request):
        return getattr(request.state, "principal_context", None)

    @router.get("/dashboard")
    def dashboard(request: Request):
        return service.dashboard(principal=principal(request))

    @router.get("/source-config")
    def source_config(request: Request):
        return service.source_config(principal=principal(request))

    @router.put("/source-config")
    def update_source_config(body: SourceConfigUpdate, request: Request):
        secure_mutation(request)
        return service.set_enabled(body.enabled, principal=principal(request))

    @router.post("/ensure-sync")
    def ensure_sync(request: Request):
        secure_mutation(request)
        return service.ensure_sync(trigger="frontend_init", principal=principal(request))

    @router.post("/sync-now")
    def sync_now(request: Request):
        secure_mutation(request)
        result = service.ensure_sync(
            force=True, trigger="manual_sync", principal=principal(request))
        if result["status"] in {"blocked", "authentication_required", "not_configured"}:
            raise HTTPException(409, detail={
                "code": result.get("reason") or result["status"],
                "message": "无法启动真实日志同步，请检查日志页配置和国内 UAT 登录状态。",
            })
        return result

    @router.post("/correlations/{record_id}/confirm")
    def confirm_correlation(record_id: str, body: ManualCorrelationRequest, request: Request):
        secure_mutation(request)
        try:
            return service.confirm_correlation(
                record_id, body.execution_id, principal=principal(request))
        except LookupError as exc:
            raise HTTPException(404, detail={
                "code": str(exc), "message": "未找到当前工作区内的日志或执行记录。",
            }) from exc
        except ValueError as exc:
            raise HTTPException(409, detail={
                "code": str(exc), "message": "该日志不能按当前输入完成人工关联。",
            }) from exc

    return router
