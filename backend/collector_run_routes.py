from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.browser_import_service import BrowserImportService
from backend.collector_run_service import CollectorRunError, CollectorRunService
from backend.collector_supervisor import CollectorSupervisor


class StartRunBody(BaseModel):
    model_config = {"extra": "forbid"}
    environment_id: str
    date_from: str
    date_to: str
    maximum_pages: int = Field(ge=1, le=10)
    maximum_records: int = Field(ge=1, le=500)
    page_delay_ms: int = Field(ge=1000, le=30000)
    operator_confirmation: bool


class ConfirmationBody(BaseModel):
    model_config = {"extra": "forbid"}
    environment_id: str
    explicit_confirmation: bool = False


class ImportBody(ConfirmationBody):
    collection_id: str
    payload_sha256: str


def _error(exc: Exception, status: int = 409) -> HTTPException:
    code = str(exc)
    messages = {
        "collector_already_running": "当前环境或浏览器会话已有采集任务。",
        "collector_environment_required": "请选择国内 UAT 或海外站。",
        "collector_log_page_unconfirmed": "当前环境的日志页尚未确认。",
        "collector_log_page_not_visible": "请先在新浏览器中打开已确认的日志页。",
        "collector_invalid_state_transition": "当前采集状态不允许此操作。",
        "collector_preview_not_ready": "结构化预览尚未生成。",
        "collector_preview_hash_mismatch": "预览摘要不匹配，禁止导入。",
        "collector_import_environment_mismatch": "导入环境与采集环境不一致。",
    }
    return HTTPException(status, detail={"code": code, "message": messages.get(code, "采集操作未能完成。")})


def build_collector_run_router(
    runs: CollectorRunService,
    supervisor: CollectorSupervisor,
    imports: BrowserImportService,
    secure_request: Callable[[Request], None],
    browser_session: Callable[[Request], str],
) -> APIRouter:
    router = APIRouter(prefix="/api/v1/collector/runs", tags=["collector-runs"])
    def principal(request: Request):
        return getattr(request.state, "principal_context", None)

    @router.post("/start")
    def start(body: StartRunBody, request: Request):
        secure_request(request)
        try:
            run = runs.create(
                environment_id=body.environment_id, date_from=body.date_from, date_to=body.date_to,
                maximum_pages=body.maximum_pages, maximum_records=body.maximum_records,
                page_delay_ms=body.page_delay_ms, operator_confirmation=body.operator_confirmation,
                browser_session=browser_session(request),
                principal=principal(request),
            )
            run = runs.transition(run["run_id"], "launching_browser",
                                  principal=principal(request))
            try:
                pid = supervisor.launch(run["run_id"])
                runs.update(run["run_id"], principal=principal(request), worker_pid=pid)
            except Exception:
                runs.transition(run["run_id"], "failed", principal=principal(request),
                                error_code="collector_browser_launch_failed",
                                safe_error_message="无法启动本地浏览器采集进程。")
                raise CollectorRunError("collector_browser_launch_failed")
            return {"run_id": run["run_id"], "environment_id": body.environment_id,
                    "status": "launching_browser", "browser_launch_requested": True}
        except CollectorRunError as exc:
            raise _error(exc)

    @router.get("")
    def list_runs(request: Request, environment_id: str | None = None, status: str | None = None,
                  date_from: str | None = None, date_to: str | None = None):
        try:
            return {"items": runs.list(environment_id, status, date_from, date_to,
                                        principal=principal(request))}
        except CollectorRunError as exc:
            raise _error(exc, 400)

    @router.get("/{run_id}")
    def get_run(run_id: str, request: Request):
        try:
            runs.heartbeat_lost(run_id, principal=principal(request),
                                permission="collector.session.read")
            item = runs.get(run_id, principal=principal(request))
            if item["status"] in {"launching_browser", "awaiting_manual_login", "login_confirmed",
                                  "collecting"} and not supervisor.is_alive(run_id):
                private = runs.get(run_id, include_private=True,
                                   principal=principal(request))
                if private.get("last_heartbeat_at"):
                    item = runs.transition(run_id, "browser_closed",
                                           principal=principal(request),
                                           permission="collector.session.read",
                                           error_code="collector_browser_closed",
                                           safe_error_message="浏览器采集进程已关闭。")
                    for key in ("preview_payload", "created_by_browser_session", "runtime_setting_sha256",
                                "console_url_sha256", "log_page_url_sha256", "worker_pid"):
                        item.pop(key, None)
            return item
        except LookupError as exc:
            raise _error(exc, 404)
        except CollectorRunError as exc:
            raise _error(exc)

    @router.post("/{run_id}/confirm-login")
    def confirm_login(run_id: str, body: ConfirmationBody, request: Request):
        secure_request(request)
        try:
            run = runs.get(run_id, include_private=True, principal=principal(request),
                           permission="collector.oneshot.execute")
            if not runs.matches_session(run_id, browser_session(request),
                                        principal=principal(request)):
                raise CollectorRunError("collector_environment_mismatch")
            if run["status"] != "awaiting_manual_login":
                raise CollectorRunError("collector_invalid_state_transition")
            if not body.explicit_confirmation or body.environment_id != run["environment_id"]:
                raise CollectorRunError("collector_environment_mismatch")
            contract = runs.verify_runtime(run_id, principal=principal(request),
                                           permission="collector.oneshot.execute")
            current = urlsplit(run.get("current_safe_url") or "")
            expected = urlsplit(contract["log_page_url"])
            visible = (
                current.scheme == "https" and current.hostname == expected.hostname
                and current.path == expected.path
                and current.port is None and expected.port is None
            )
            if not visible:
                raise CollectorRunError("collector_log_page_not_visible")
            runs.transition(run_id, "login_confirmed", principal=principal(request))
            return {"run_id": run_id, "status": "login_confirmed"}
        except (CollectorRunError, LookupError) as exc:
            raise _error(exc, 404 if isinstance(exc, LookupError) else 409)

    @router.post("/{run_id}/stop")
    def stop(run_id: str, body: ConfirmationBody, request: Request):
        secure_request(request)
        try:
            run = runs.get(run_id, include_private=True, principal=principal(request),
                           permission="collector.oneshot.execute")
            if not runs.matches_session(run_id, browser_session(request),
                                        principal=principal(request)):
                raise CollectorRunError("collector_environment_mismatch")
            if body.environment_id != run["environment_id"]:
                raise CollectorRunError("collector_environment_mismatch")
            requested = runs.request_stop(run_id, principal=principal(request))
            supervisor.stop(run_id)
            requested = runs.get(run_id, include_private=True, principal=principal(request),
                                 permission="collector.oneshot.execute")
            if requested["status"] == "stop_requested":
                requested = runs.transition(run_id, "stopped", principal=principal(request))
            return {"run_id": run_id, "status": requested["status"], "idempotent": True}
        except (CollectorRunError, LookupError) as exc:
            raise _error(exc, 404 if isinstance(exc, LookupError) else 409)

    @router.get("/{run_id}/preview")
    def preview(run_id: str, environment_id: str, request: Request):
        try:
            run = runs.get(run_id, include_private=True, principal=principal(request))
            if run["environment_id"] != environment_id:
                raise CollectorRunError("collector_import_environment_mismatch")
            return runs.preview(run_id, principal=principal(request))
        except (CollectorRunError, LookupError) as exc:
            raise _error(exc, 404 if isinstance(exc, LookupError) else 409)

    @router.post("/{run_id}/confirm-import")
    def confirm_import(run_id: str, body: ImportBody, request: Request):
        secure_request(request)
        try:
            run = runs.get(run_id, include_private=True, principal=principal(request),
                           permission="collector.oneshot.execute")
            if run["status"] != "preview_ready":
                raise CollectorRunError("collector_preview_not_ready")
            if not runs.matches_session(run_id, browser_session(request),
                                        principal=principal(request)):
                raise CollectorRunError("collector_environment_mismatch")
            if body.environment_id != run["environment_id"] or body.collection_id != run["collection_id"]:
                raise CollectorRunError("collector_import_environment_mismatch")
            if not body.explicit_confirmation or body.payload_sha256 != run["preview_payload_sha256"]:
                raise CollectorRunError("collector_preview_hash_mismatch")
            result = imports.confirm(body.collection_id, body.payload_sha256, True,
                                     body.environment_id, principal=principal(request))
            runs.transition(run_id, "import_confirmed", principal=principal(request))
            runs.transition(run_id, "completed", principal=principal(request))
            return result
        except LookupError as exc:
            raise _error(exc, 404)
        except (CollectorRunError, ValueError) as exc:
            raise _error(exc)

    return router
