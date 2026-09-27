"""FastAPI routes for safe sticky-binding operator inspection."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request

from backend.sticky_routing_service import StickyRoutingService
from sticky_routing import StickyRoutingError
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext


def build_sticky_routing_router(
    service_provider: Callable[[], StickyRoutingService],
    secure_mutation: Callable[[Request], None],
    environment_authorized: Callable[[Request, str], bool],
    *,
    authorization_service: AuthorizationService | None = None,
    principal_resolver: Callable[[Request], PrincipalContext] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sticky-routing", tags=["sticky-routing"])

    def principal_for(request: Request) -> PrincipalContext | None:
        if authorization_service is None:
            return None
        if principal_resolver is None:
            raise PermissionError("principal_resolver_required")
        return principal_resolver(request)

    @router.get("/bindings")
    def bindings(
        request: Request,
        environment_id: str | None = None, state: str | None = None,
        model: str | None = None, channel: str | None = None,
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    ):
        if state and state not in {"ACTIVE", "EXPIRED", "INVALIDATED", "INTERRUPTED"}:
            raise HTTPException(400, detail={"code": "invalid_state", "message": "未知绑定状态。"})
        return service_provider().list(
            environment_id=environment_id, state=state, model=model,
            channel=channel, limit=limit, offset=offset,
            principal=principal_for(request),
        )

    @router.get("/bindings/{binding_id}")
    def binding_detail(binding_id: str, request: Request):
        item = service_provider().get(binding_id, principal=principal_for(request))
        if not item:
            raise HTTPException(404, detail={"code": "sticky_binding_not_found", "message": "未找到粘性路由绑定。"})
        return item

    @router.get("/status")
    def status(request: Request):
        return service_provider().status(principal=principal_for(request))

    @router.get("/metrics")
    def metrics(request: Request, environment_id: str | None = None):
        return service_provider().metrics(
            environment_id=environment_id, principal=principal_for(request))

    @router.post("/bindings/{binding_id}/invalidate")
    async def invalidate(binding_id: str, request: Request):
        secure_mutation(request)
        body: dict[str, Any] = await request.json()
        environment_id = str(body.get("environment_id") or "").strip()
        confirmation = str(body.get("confirmation_text") or "")
        idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
        policy = service_provider().policy
        if not environment_id or not environment_authorized(request, environment_id):
            raise HTTPException(403, detail={
                "code": "environment_not_authorized", "message": "当前环境未获授权。"})
        if confirmation != policy["invalidation_confirmation_text"]:
            raise HTTPException(400, detail={
                "code": "explicit_confirmation_required",
                "message": "请输入完整中文确认文本后再使绑定失效。"})
        if not idempotency_key or len(idempotency_key) > 200:
            raise HTTPException(400, detail={
                "code": "idempotency_key_required",
                "message": "缺少有效的 Idempotency-Key。"})
        reason = str(body.get("reason") or "operator_confirmed_invalidation").strip()[:200]
        canonical = json.dumps(
            {"binding_id": binding_id, "environment_id": environment_id,
             "confirmation_text": confirmation, "reason": reason},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        try:
            return service_provider().invalidate(
                binding_id, environment_id=environment_id, reason=reason,
                idempotency_key=idempotency_key,
                request_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                principal=principal_for(request),
            )
        except StickyRoutingError as exc:
            code = str(exc)
            status_code = 409 if code == "idempotency_key_conflict" else 404
            raise HTTPException(status_code, detail={"code": code, "message": "操作无法完成。"})

    return router
