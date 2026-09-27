from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.persistent_session_service import (
    PersistentSessionError, PersistentSessionService,
)
from backend.domestic_uat_chrome_manager import (
    DomesticUatChromeError, DomesticUatChromeManager,
)


MESSAGES = {
    "persistent_mode_not_supported_on_platform": "加密持久后台同步默认关闭，请由本机管理员明确启用。",
    "persistent_mode_windows_only": "加密持久后台同步仅支持 Windows DPAPI CurrentUser。",
    "persistent_session_not_found": "未找到可用的本地加密登录状态。",
    "persistent_pairing_not_found": "未找到登录配对任务。",
    "persistent_session_expired": "本地加密登录状态已过期，请重新登录配对。",
    "persistent_session_corrupted": "本地加密登录状态完整性校验失败，已安全删除。",
    "persistent_session_environment_mismatch": "加密持久模式仅限国内 UAT，不能跨环境使用。",
    "persistent_session_host_mismatch": "登录状态包含非批准主机，已拒绝使用。",
    "persistent_session_decryption_failed": "当前 Windows 用户无法解密本地登录状态。",
    "persistent_session_reauthentication_required": "平台登录状态已失效，请重新登录配对。",
    "persistent_session_requires_unsupported_web_storage": "认证依赖 Web Storage，未经独立评审不能启用持久模式。",
    "persistent_session_requires_unsupported_indexeddb": "认证依赖当前无法安全恢复的 IndexedDB，持久模式保持阻止。",
    "persistent_session_origin_not_allowed": "认证状态包含未批准的来源，已拒绝保存或恢复。",
    "persistent_session_storage_value_too_large": "认证存储值超过本机安全上限，已拒绝保存。",
    "persistent_session_storage_item_limit_exceeded": "认证存储项目数量超过本机安全上限，已拒绝保存。",
    "persistent_session_storage_key_not_allowed": "认证存储包含未批准的键，已拒绝保存。",
    "persistent_storage_diagnostic_invalid": "认证存储诊断结构无效。",
    "persistent_storage_diagnostic_contains_secret": "认证存储诊断疑似包含秘密值，已拒绝保存。",
    "persistent_session_save_failed": "本地登录状态加密保存失败。",
    "persistent_session_revoke_failed": "无法清除本地加密登录状态。",
    "persistent_session_already_active": "国内 UAT 已有活动配对或持久会话。",
    "persistent_headless_launch_failed": "无法启动无界面同步浏览器。",
    "persistent_session_ttl_invalid": "会话有效期必须大于 0 且不能超过配置上限。",
    "persistent_explicit_confirmation_required": "必须明确确认此持久会话操作。",
    "persistent_revoke_confirmation_required": "请输入指定确认文字后才能清除本地登录状态。",
    "persistent_pairing_invalid_state": "当前配对状态不能确认登录。",
    "chrome_not_installed": "本机未安装 Google Chrome。",
    "browser_not_running": "国内 UAT Google Chrome 尚未运行。",
    "browser_close_confirmation_required": "必须明确确认关闭国内 UAT 浏览器。",
    "browser_close_failed": "无法安全关闭国内 UAT Google Chrome。",
    "chrome_start_failed": "Google Chrome 启动失败。",
    "chrome_start_timeout": "Google Chrome 启动超时。",
    "authentication_not_confirmed": "尚未检测到已登录的国内 UAT 日志页。",
    "authentication_check_failed": "认证状态检查失败，请保留 Chrome 窗口并重试。",
    "remote_session_expiry_unavailable": "远端会话未提供可验证的到期时间，未保存登录状态。",
    "remote_session_expiry_invalid": "远端会话到期时间无效，未保存登录状态。",
    "remote_session_expired": "远端登录会话已经到期，请重新登录。",
    "log_sync_log_page_unconfirmed": "国内 UAT 日志页尚未由本机操作员明确确认。",
}


def failure(exc: Exception, status: int = 409):
    code = str(exc)
    return HTTPException(status, detail={
        "code": code, "message": MESSAGES.get(code, "持久后台同步操作未能完成。")})


class PairingStart(BaseModel):
    model_config = {"extra": "forbid"}
    environment_id: str
    ttl_hours: int = Field(default=8, ge=1, le=24)
    explicit_confirmation: bool


class ExplicitConfirmation(BaseModel):
    model_config = {"extra": "forbid"}
    explicit_confirmation: bool


class SessionValidationBody(BaseModel):
    model_config = {"extra": "forbid"}
    environment_id: str
    explicit_confirmation: bool


class PersistentJob(BaseModel):
    model_config = {"extra": "forbid"}
    environment_id: str
    date_from: str
    date_to: str
    timezone: str
    periodic_polling: bool = True
    sync_interval_seconds: int = Field(default=7, ge=7, le=30)
    maximum_records: int = Field(default=500, ge=1, le=1000)
    maximum_http_reads: int = Field(default=50, ge=1, le=50)
    maximum_records_observed: int | None = Field(
        default=None, ge=1, le=1000)
    maximum_records_accepted: int | None = Field(
        default=None, ge=1, le=1000)
    maximum_elapsed_seconds: int = Field(default=600, ge=10, le=600)
    page_size: int = Field(default=100, ge=1, le=100)
    maximum_pages: int = Field(default=50, ge=1, le=50)
    persistent_session_id: str
    explicit_confirmation: bool


class RevokeBody(BaseModel):
    model_config = {"extra": "forbid"}
    persistent_session_id: str
    confirmation_text: str


class AutoResumeBody(BaseModel):
    model_config = {"extra": "forbid"}
    persistent_session_id: str
    enabled: bool
    explicit_confirmation: bool


def build_persistent_session_router(
    service: PersistentSessionService,
    launch_pairing: Callable[[str], int],
    launch_persistent: Callable[[str], int],
    stop_worker: Callable[[str], bool],
    secure_request: Callable[[Request], None],
    secure_read_only_request: Callable[[Request], str],
    chrome_manager: DomesticUatChromeManager | None = None,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/log-sync/persistent", tags=["persistent-log-sync"])
    def principal(request: Request):
        return getattr(request.state, "principal_context", None)

    def launch_pairing_operation(pairing: dict, *, reauthentication: bool,
                                 principal_context=None):
        try:
            if chrome_manager is not None:
                browser = chrome_manager.open_or_focus()
                service.update_pairing(
                    pairing["pairing_id"], state="waiting_for_operator",
                    browser_state="formal_google_chrome",
                    principal=principal_context)
            else:
                pid = launch_pairing(pairing["pairing_id"])
                service.update_pairing(pairing["pairing_id"], worker_pid=pid,
                                       principal=principal_context)
                browser = None
        except (DomesticUatChromeError, Exception) as exc:
            code = str(exc)
            service.update_pairing(
                pairing["pairing_id"], state="failed", browser_state="closed",
                failure_code=(code if code in MESSAGES else
                              "persistent_headless_launch_failed"),
                principal=principal_context,
                failure_summary=MESSAGES.get(
                    code, "无法启动正式 Google Chrome 认证浏览器。"))
            browser = None
        result = service.get_pairing(
            pairing["pairing_id"], principal=principal_context,
            permission="collector.session.reauthenticate")
        result["operation"] = (
            "reauthentication" if reauthentication else "initial_pairing")
        result["synchronization_job_created"] = False
        result["billing_log_read_issued"] = False
        result["browser"] = browser
        result["safe_instructions"] = (
            "请只在项目专用的正式 Google Chrome 中手动登录；"
            "本应用不会读取或保存密码，也不会处理 CAPTCHA 或 MFA。")
        return result

    def require_chrome() -> DomesticUatChromeManager:
        if chrome_manager is None:
            raise failure(DomesticUatChromeError("chrome_not_installed"))
        return chrome_manager

    def verify_and_save(pairing_id: str, request: Request):
        manager = require_chrome()
        captured = None
        try:
            captured = manager.capture_authenticated_session()
            service.save_storage_diagnostic(
                pairing_id, captured["diagnostic"], principal=principal(request))
            service.request_confirmation(
                pairing_id, True, principal=principal(request))
            session = service.complete_pairing(
                pairing_id, captured["cookies"], captured["web_storage"],
                captured["indexed_db"],
                remote_expires_at=captured["remote_expires_at"],
                principal=principal(request))
            manager.mark_authenticated()
            return {"session": session, "browser": manager.status()}
        except (PersistentSessionError, DomesticUatChromeError) as exc:
            code = str(exc)
            try:
                service.update_pairing(
                    pairing_id, state="authentication_validation_failed",
                    browser_state="formal_google_chrome",
                    failure_code=code,
                    failure_summary=MESSAGES.get(
                        code, "认证状态尚未通过安全检查。"),
                    principal=principal(request))
            except Exception:
                pass
            raise failure(exc)
        finally:
            if captured:
                for key in ("cookies", "web_storage", "indexed_db"):
                    value = captured.get(key)
                    if isinstance(value, list):
                        value.clear()

    @router.get("/browser/status")
    def chrome_status(request: Request):
        secure_read_only_request(request)
        return require_chrome().status()

    @router.post("/browser/open-or-focus")
    def open_or_focus_chrome(body: ExplicitConfirmation, request: Request):
        secure_request(request)
        if not body.explicit_confirmation:
            raise failure(PersistentSessionError(
                "persistent_explicit_confirmation_required"))
        try:
            return require_chrome().open_or_focus()
        except DomesticUatChromeError as exc:
            raise failure(exc)

    @router.post("/browser/close")
    def close_chrome(body: ExplicitConfirmation, request: Request):
        secure_request(request)
        try:
            result = require_chrome().explicit_close(body.explicit_confirmation)
            status = service.status(principal=principal(request))
            pairing = status.get("pairing")
            if pairing and pairing.get("state") in {
                    "browser_starting", "waiting_for_operator",
                    "authentication_in_progress", "waiting_for_confirmation",
                    "authentication_validation_failed"}:
                service.update_pairing(
                    pairing["pairing_id"], state="stopped",
                    browser_state="closed_by_operator",
                    failure_code="persistent_pairing_closed_by_operator",
                    failure_summary="操作员已明确关闭国内 UAT Google Chrome。",
                    principal=principal(request))
            return result
        except DomesticUatChromeError as exc:
            raise failure(exc)

    @router.post("/pairing/start")
    def start_pairing(body: PairingStart, request: Request):
        secure_request(request)
        try:
            pairing = service.start_pairing(
                body.environment_id, body.ttl_hours, body.explicit_confirmation,
                principal=principal(request))
            return launch_pairing_operation(
                pairing, reauthentication=False, principal_context=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.post("/reauthentication/start")
    def start_reauthentication(body: PairingStart, request: Request):
        secure_request(request)
        try:
            pairing = service.start_reauthentication(
                body.environment_id, body.ttl_hours,
                body.explicit_confirmation, principal=principal(request))
            return launch_pairing_operation(
                pairing, reauthentication=True, principal_context=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.post("/pairing/{pairing_id}/confirm")
    def confirm_pairing(pairing_id: str, body: ExplicitConfirmation,
                        request: Request):
        secure_request(request)
        try:
            return service.request_confirmation(
                pairing_id, body.explicit_confirmation, principal=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.get("/pairing/{pairing_id}")
    def pairing_status(pairing_id: str, request: Request):
        try:
            return service.get_pairing(pairing_id, principal=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc, 404)

    @router.get("/pairing/{pairing_id}/diagnostic")
    def storage_diagnostic(pairing_id: str, request: Request):
        try:
            return service.storage_diagnostic(pairing_id, principal=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc, 404)

    @router.post("/pairing/{pairing_id}/encrypted-save")
    def confirm_encrypted_save(
        pairing_id: str, body: ExplicitConfirmation, request: Request,
    ):
        secure_request(request)
        if not body.explicit_confirmation:
            raise failure(PersistentSessionError(
                "persistent_explicit_confirmation_required"))
        if chrome_manager is not None:
            return verify_and_save(pairing_id, request)
        try:
            return service.request_confirmation(
                pairing_id, body.explicit_confirmation, principal=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.post("/pairing/{pairing_id}/check-authentication")
    def check_authentication(
        pairing_id: str, body: ExplicitConfirmation, request: Request,
    ):
        secure_request(request)
        if not body.explicit_confirmation:
            raise failure(PersistentSessionError(
                "persistent_explicit_confirmation_required"))
        return verify_and_save(pairing_id, request)

    @router.post("/jobs")
    def start_job(body: PersistentJob, request: Request):
        secure_request(request)
        try:
            job = service.create_job(body.model_dump(), principal=principal(request))
            try:
                pid = launch_persistent(job["sync_job_id"])
                service.realtime.update_job(job["sync_job_id"], worker_pid=pid,
                                            principal=principal(request),
                                            permission="collector.oneshot.execute")
            except Exception:
                service.realtime.update_job(
                    job["sync_job_id"], state="failed", stopped_at=job["created_at"],
                    principal=principal(request), permission="collector.oneshot.execute",
                    error_code="persistent_headless_launch_failed",
                    safe_error_message=MESSAGES["persistent_headless_launch_failed"])
            return service.realtime.get_job(job["sync_job_id"], principal=principal(request),
                                            permission="collector.oneshot.execute")
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.get("/status")
    def status(request: Request):
        result = service.status(principal=principal(request))
        result["browser"] = chrome_manager.status() if chrome_manager else None
        return result

    @router.get("/sessions/{session_id}/validate")
    def inspect_session(
        session_id: str, environment_id: str, request: Request,
        response: Response,
    ):
        origin_policy = secure_read_only_request(request)
        try:
            result = service.inspect_session(session_id, environment_id,
                                             principal=principal(request))
            service._event("persistent_session_read_only_access", {
                "session_id_hash": service.audit_session_id(session_id),
                "origin_policy": origin_policy,
                "endpoint": "session_metadata_validation",
            }, principal=principal(request), permission="collector.session.read")
            response.headers["Cache-Control"] = "no-store"
            return {**result, "origin_policy": origin_policy}
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.post("/sessions/{session_id}/validate")
    def validate_session(
        session_id: str, body: SessionValidationBody, request: Request,
    ):
        secure_request(request)
        if not body.explicit_confirmation:
            raise failure(PersistentSessionError(
                "persistent_explicit_confirmation_required"))
        try:
            return service.validate_session(session_id, body.environment_id,
                                            principal=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.post("/jobs/{job_id}/stop")
    def stop_persistent_job(job_id: str, request: Request):
        secure_request(request)
        try:
            return service.stop_job(job_id, stop_worker, principal=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.post("/auto-resume")
    def auto_resume(body: AutoResumeBody, request: Request):
        secure_request(request)
        try:
            return service.set_auto_resume(
                body.persistent_session_id, body.enabled,
                body.explicit_confirmation, principal=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc)

    @router.post("/revoke")
    def revoke(body: RevokeBody, request: Request):
        secure_request(request)
        try:
            return service.revoke(
                body.persistent_session_id, body.confirmation_text, stop_worker,
                principal=principal(request))
        except PersistentSessionError as exc:
            raise failure(exc)

    return router
