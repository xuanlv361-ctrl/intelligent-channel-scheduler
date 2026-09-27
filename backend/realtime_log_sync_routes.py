from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.realtime_log_sync_service import RealtimeLogSyncError, RealtimeLogSyncService


class StartBody(BaseModel):
    model_config = {"extra": "forbid"}
    environment_id: str
    date_from: str
    date_to: str
    timezone: str
    sync_interval_seconds: int = Field(default=7, ge=3, le=30)
    maximum_records: int = Field(default=500, ge=1, le=1000)


class ConfirmBody(BaseModel):
    model_config = {"extra": "forbid"}
    environment_id: str
    explicit_confirmation: bool


class StopBody(ConfirmBody):
    pass


class ManualCorrelationBody(BaseModel):
    model_config = {"extra": "forbid"}
    provider_record_id: str = Field(min_length=1, max_length=128)
    local_request_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=8, max_length=500)


def error(exc: Exception, status: int = 409) -> HTTPException:
    return HTTPException(status, detail={"code": str(exc), "message": {
        "log_sync_concrete_environment_required": "请选择国内 UAT 或海外站具体环境。",
        "log_sync_job_already_active": "当前环境已有活动同步任务。",
        "log_sync_log_page_unconfirmed": "当前环境尚未确认只读日志页面。",
        "log_sync_log_page_not_allowed": "日志页面不属于当前环境允许主机。",
        "log_sync_explicit_confirmation_required": "必须明确确认已完成手动登录。",
        "log_sync_log_page_not_visible": "浏览器当前未停留在已批准日志页面。",
        "log_sync_invalid_state": "当前同步状态不允许此操作。",
        "log_sync_date_from_required": "必须提供开始日期和时间。",
        "log_sync_date_to_required": "必须提供结束日期和时间。",
        "log_sync_date_from_invalid": "开始日期和时间格式无效。",
        "log_sync_date_to_invalid": "结束日期和时间格式无效。",
        "log_sync_date_from_timezone_required": "开始时间必须包含时区偏移。",
        "log_sync_date_to_timezone_required": "结束时间必须包含时区偏移。",
        "log_sync_timezone_invalid": "浏览器时区无效。",
        "log_sync_range_order_invalid": "开始时间必须早于结束时间。",
        "log_sync_range_too_large": "选择的时间范围超过允许上限。",
        "log_sync_date_to_in_future": "结束时间超过允许的时钟偏差。",
    }.get(str(exc), "日志同步操作未能完成。")})


def build_realtime_log_sync_router(
    service: RealtimeLogSyncService,
    launch: Callable[[str], int],
    stop_worker: Callable[[str], bool],
    secure_request: Callable[[Request], None],
    coordinator: Any | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/log-sync", tags=["log-sync"])
    def principal(request: Request):
        return getattr(request.state, "principal_context", None)

    @router.post("/ensure")
    def ensure(request: Request):
        """Idempotent read-only synchronization; no UAT execution gate applies."""
        if coordinator is None:
            raise HTTPException(503, detail={"code": "log_sync_coordinator_unavailable",
                                             "message": "日志同步协调器尚未启动。"})
        return coordinator.ensure(trigger="frontend_ensure", principal=principal(request))

    @router.get("/status")
    def coordinator_status(request: Request):
        if coordinator is None:
            raise HTTPException(503, detail={"code": "log_sync_coordinator_unavailable"})
        return coordinator.status(principal=principal(request))

    @router.get("/history")
    def coordinator_history(request: Request):
        if coordinator is None:
            raise HTTPException(503, detail={"code": "log_sync_coordinator_unavailable"})
        return coordinator.history(principal=principal(request))

    @router.post("/jobs")
    def start(body: StartBody, request: Request):
        secure_request(request)
        try:
            job = service.create_job(
                body.environment_id, body.date_from, body.date_to, body.timezone,
                body.sync_interval_seconds, body.maximum_records,
                principal=principal(request))
            try:
                pid = launch(job["sync_job_id"])
                service.update_job(job["sync_job_id"], principal=principal(request),
                                   permission="collector.oneshot.execute", worker_pid=pid)
            except Exception:
                service.update_job(
                    job["sync_job_id"], principal=principal(request),
                    permission="collector.oneshot.execute",
                    state="failed", stopped_at=job["created_at"],
                    error_code="log_sync_browser_launch_failed",
                    safe_error_message="无法启动本地可见 Chromium。", browser_context_active=0)
            return service.get_job(job["sync_job_id"], principal=principal(request),
                                   permission="collector.oneshot.execute")
        except RealtimeLogSyncError as exc:
            raise error(exc)

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, request: Request):
        try:
            return service.get_job(job_id, principal=principal(request))
        except LookupError as exc:
            raise HTTPException(404, detail={"code": str(exc), "message": "同步任务不存在。"})

    @router.post("/jobs/{job_id}/confirm-login")
    def confirm(job_id: str, body: ConfirmBody, request: Request):
        secure_request(request)
        try:
            job = service.get_job(job_id, private=True, principal=principal(request),
                                  permission="collector.oneshot.execute")
            if job["environment_id"] != body.environment_id:
                raise RealtimeLogSyncError("log_sync_environment_mismatch")
            return service.confirm_login(
                job_id, body.explicit_confirmation, job.get("current_safe_url"),
                principal=principal(request))
        except (RealtimeLogSyncError, LookupError) as exc:
            raise error(exc)

    @router.post("/jobs/{job_id}/stop")
    def stop(job_id: str, body: StopBody, request: Request):
        secure_request(request)
        try:
            job = service.get_job(job_id, private=True, principal=principal(request),
                                  permission="collector.oneshot.execute")
            if job["environment_id"] != body.environment_id or not body.explicit_confirmation:
                raise RealtimeLogSyncError("log_sync_explicit_confirmation_required")
            result = service.stop(job_id, principal=principal(request))
            stop_worker(job_id)
            return result
        except (RealtimeLogSyncError, LookupError) as exc:
            raise error(exc)

    @router.get("/jobs")
    def jobs(request: Request, environment_id: str | None = None):
        if environment_id == "all":
            raise error(RealtimeLogSyncError("log_sync_concrete_environment_required"), 400)
        return {"items": service.list_jobs(environment_id, principal=principal(request))}

    @router.get("/events")
    def events(request: Request, environment_id: str | None = None, after_id: int = 0):
        if coordinator is not None and (environment_id in {None, "china_uat"}):
            return coordinator.events(after_id, principal=principal(request))
        return {"items": service.events(environment_id, after_id,
                                        principal=principal(request)),
                "transport": "short_polling", "recommended_interval_seconds": 5}

    @router.get("/health")
    def health(request: Request):
        return service.health(principal=principal(request))

    @router.get("/reconciliation")
    def reconciliation(environment_id: str, request: Request):
        if environment_id not in {"china_uat", "overseas"}:
            raise error(RealtimeLogSyncError("log_sync_concrete_environment_required"), 400)
        return {"environment_id": environment_id,
                "items": service.reconciliation(environment_id,
                                                principal=principal(request))}

    @router.post("/correlations/manual-confirm")
    def manual_confirm(body: ManualCorrelationBody, request: Request):
        secure_request(request)
        try:
            return service.manually_confirm_correlation(
                body.provider_record_id, body.local_request_id, body.reason,
                principal=principal(request))
        except (RealtimeLogSyncError, LookupError) as exc:
            raise error(exc)

    return router
