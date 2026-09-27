"""Windows-DPAPI-backed pairing and headless UAT log synchronization worker."""
from __future__ import annotations

import argparse
import inspect
import json
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from backend.encrypted_session_vault import EncryptedSessionVault
from backend.domestic_uat_chrome_manager import (
    DomesticUatChromeError, DomesticUatChromeManager,
)
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from backend.persistent_session_service import (
    PersistentSessionError, PersistentSessionService,
)
from collector.collector_worker import resolve_worker_principal
from backend.realtime_log_sync_service import (
    ACTIVE_STATES, RealtimeLogSyncService, safe_url, utcnow,
)
from backend.uat_log_read_contract import (
    DomesticUatLogQuery,
    UatLogReadContractError,
    validate_response_pagination,
    validate_authorized_url,
    validate_observed_url,
)

ROOT = Path(__file__).resolve().parents[1]
WATERMARK_OVERLAP_SECONDS = 60
APPROVED_ORIGINS = {
    "https://uat.weimeta.cn", "https://admin-uat.weimeta.cn",
}


def next_poll_deadline(completed_at: float, interval_seconds: int) -> float:
    """Schedule from completion; slow polls are never followed by catch-up polls."""
    return float(completed_at) + int(interval_seconds)


def retry_backoff_seconds(failure_number: int, jitter: float) -> float:
    """Bounded exponential backoff; caller supplies injectable jitter [0, 1]."""
    if failure_number not in {1, 2} or not 0 <= float(jitter) <= 1:
        raise ValueError("retry_backoff_input_invalid")
    return min(8.0, 2.0 ** (failure_number - 1)) * (
        0.75 + 0.5 * float(jitter))


def retryable_read_error(code: str) -> bool:
    return (
        code in {
            "log_sync_response_too_large",
            "log_sync_response_not_json",
            "log_sync_response_invalid_json",
        }
        or code.startswith("log_sync_http_429")
        or code.startswith("log_sync_http_5")
    )


def is_approved_log_api_read(url: str) -> bool:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and port in {None, 443}
        and (parsed.hostname or "").casefold() in {
            "uat.weimeta.cn", "admin-uat.weimeta.cn"}
        and parsed.path == "/api/log/self"
    )


def handle_log_api_route(route, authorize) -> dict | None:
    """Continue only reserved exact billing reads; abort lookalikes and overflow."""
    parsed = urlsplit(route.request.url)
    if parsed.path != "/api/log/self":
        route.continue_()
        return None
    if not is_approved_log_api_read(route.request.url):
        route.abort()
        return {"authorized": False, "reason": "source_not_allowed"}
    result = authorize()
    if result["authorized"]:
        route.continue_()
    else:
        route.abort()
    return result


def read_billing_page(
    page, query: DomesticUatLogQuery, *, use_reviewed_page_query: bool = False,
) -> dict:
    """Issue exactly one same-origin GET and return a bounded JSON envelope."""
    target = query.url()
    validate_authorized_url(target, query)
    if use_reviewed_page_query:
        # The reviewed console attaches its authentication material internally.
        # Drive its date controls instead of reading, copying, or replaying an
        # Authorization value from the operator's persistent Chrome profile.
        local_zone = ZoneInfo("Asia/Shanghai")
        start_text = query.start.astimezone(local_zone).strftime(
            "%Y-%m-%d %H:%M:%S")
        end_text = query.end.astimezone(local_zone).strftime(
            "%Y-%m-%d %H:%M:%S")
        if query.page == 1:
            start_input = page.locator('input[placeholder="开始日期"]')
            end_input = page.locator('input[placeholder="结束日期"]')
            start_input.fill(start_text)
            start_input.press("Tab")
            end_input.fill(end_text)
            end_input.press("Tab")
            trigger = page.locator(
                "button.semi-button-primary.semi-button-size-small."
                "semi-button-solid"
            ).first
        else:
            # The console adds its credential internally. Continue by driving
            # the exact visible page control instead of copying that credential
            # into a synthetic request. The response URL is validated below.
            trigger = page.locator(
                f'li.semi-page-item[aria-label="Page {int(query.page)}"]')
            if trigger.count() != 1:
                raise PersistentSessionError(
                    "log_sync_pagination_control_missing")
        with page.expect_response(
            lambda item: is_approved_log_api_read(item.url), timeout=15_000,
        ) as observed:
            trigger.click()
        page_response = observed.value
        observed_query = validate_observed_url(page_response.url, query)
        text = page_response.text()
        response = {
            "status": page_response.status,
            "contentType": page_response.headers.get("content-type", ""),
            "text": text if len(text) <= 2_097_152 else None,
            "oversized": len(text) > 2_097_152,
        }
        target = page_response.url
    else:
        response = page.evaluate(
            """async (url) => {
              const response = await fetch(url, {
                method: "GET",
                credentials: "include",
                headers: {Accept: "application/json"}
              });
              const contentType = response.headers.get("content-type") || "";
              const text = await response.text();
              return {
                status: response.status,
                contentType,
                text: text.length <= 2097152 ? text : null,
                oversized: text.length > 2097152
              };
            }""",
            target,
        )
    if response.get("oversized"):
        raise PersistentSessionError("log_sync_response_too_large")
    status = int(response.get("status") or 0)
    if status in {401, 403}:
        raise PersistentSessionError(
            "persistent_session_reauthentication_required")
    if status != 200:
        raise PersistentSessionError(f"log_sync_http_{status or 'unknown'}")
    if "json" not in str(response.get("contentType") or "").casefold():
        raise PersistentSessionError("log_sync_response_not_json")
    try:
        payload = json.loads(str(response.get("text") or ""))
    except json.JSONDecodeError as exc:
        raise PersistentSessionError("log_sync_response_invalid_json") from exc
    return {
        "payload": payload, "source_url": target, "http_status": status,
        "observed_query": observed_query if use_reviewed_page_query else query,
    }


def read_public_billing_context(page) -> dict[str, Any]:
    """Read only the public conversion settings used by the billing log UI."""
    response = page.evaluate("""async () => {
      const response = await fetch('/api/status', {
        method: 'GET', credentials: 'omit', headers: {Accept: 'application/json'}
      });
      const payload = await response.json();
      const data = payload && payload.data || {};
      return {
        status: response.status,
        quota_per_unit: data.quota_per_unit,
        quota_display_type: data.quota_display_type,
        usd_exchange_rate: data.usd_exchange_rate
      };
    }""")
    if int(response.get("status") or 0) != 200:
        raise PersistentSessionError("billing_context_unavailable")
    context = {
        "quota_per_unit": response.get("quota_per_unit"),
        "quota_display_type": response.get("quota_display_type"),
        "usd_exchange_rate": response.get("usd_exchange_rate"),
    }
    cost, currency = RealtimeLogSyncService._quota_cost(1, context)
    if cost is None or currency is None:
        raise PersistentSessionError("billing_context_invalid")
    return context


def services(database_path: Path):
    principal, _authorization = resolve_worker_principal(database_path, "worker")
    runtime = EnvironmentRuntimeSettings(database_path)
    realtime = RealtimeLogSyncService(
        database_path, ROOT / "output" / "unified_uat_execution_v3.jsonl", runtime,
        maximum_range_hours=int(os.getenv("UAT_LOG_SYNC_MAX_RANGE_HOURS", "168")),
        clock_skew_seconds=int(os.getenv("UAT_LOG_SYNC_CLOCK_SKEW_SECONDS", "300")))
    configured_keys = os.getenv("UAT_PERSISTENT_SESSION_ALLOWED_STORAGE_KEYS")
    persistent = PersistentSessionService(
        realtime, EncryptedSessionVault(
            approved_origins={
                item.strip() for item in os.getenv(
                    "UAT_PERSISTENT_SESSION_ALLOWED_ORIGINS",
                    ",".join(sorted(APPROVED_ORIGINS))).split(",")
                if item.strip()},
            maximum_vault_bytes=int(os.getenv(
                "UAT_PERSISTENT_SESSION_MAX_VAULT_BYTES", str(4 * 1024 * 1024))),
            maximum_item_count=int(os.getenv(
                "UAT_PERSISTENT_SESSION_MAX_ITEM_COUNT", "512")),
            maximum_value_bytes=int(os.getenv(
                "UAT_PERSISTENT_SESSION_MAX_VALUE_BYTES", str(256 * 1024))),
            allowed_storage_keys=(
                {item.strip() for item in configured_keys.split(",") if item.strip()}
                if configured_keys else None)),
        enabled=os.getenv("UAT_PERSISTENT_SESSION_ENABLED", "false").lower() == "true",
        default_ttl_hours=int(os.getenv("UAT_PERSISTENT_SESSION_TTL_HOURS", "8")),
        maximum_ttl_hours=int(os.getenv("UAT_PERSISTENT_SESSION_MAX_TTL_HOURS", "24")))
    return realtime, persistent, principal


def _origin(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or \
            parsed.query or parsed.fragment:
        return None
    value = f"https://{(parsed.hostname or '').casefold()}"
    return value if value in APPROVED_ORIGINS and parsed.port in {None, 443} else None


def _storage_names(page) -> dict:
    return page.evaluate("""async () => {
      let indexed = [];
      if (indexedDB && typeof indexedDB.databases === "function") {
        indexed = (await indexedDB.databases()).map(item => item.name).filter(Boolean);
      }
      return {
        local: Object.keys(localStorage).sort(),
        session: Object.keys(sessionStorage).sort(),
        indexed: indexed.sort()
      };
    }""")


def _diagnostic(context, page, before: dict[str, list[str]]) -> dict:
    current_origin = _origin(page.url)
    if not current_origin:
        raise PersistentSessionError("persistent_session_origin_not_allowed")
    names = _storage_names(page)
    cookie_metadata = []
    for cookie in context.cookies(list(APPROVED_ORIGINS)):
        domain = str(cookie.get("domain") or "").lstrip(".").casefold()
        if domain not in {"uat.weimeta.cn", "admin-uat.weimeta.cn"}:
            continue
        cookie_metadata.append({
            key: cookie.get(key) for key in (
                "name", "domain", "secure", "sameSite", "expires")
        })
    changed = []
    for label, key in (
            ("cookies", "cookies"), ("localStorage", "local"),
            ("sessionStorage", "session"), ("IndexedDB", "indexed")):
        after_names = (
            sorted(item["name"] for item in cookie_metadata)
            if key == "cookies" else names[key])
        if after_names != before.get(key, []):
            changed.append(label)
    return {
        "cookie_metadata": cookie_metadata,
        "local_storage_keys": names["local"],
        "session_storage_keys": names["session"],
        "indexed_db_names": names["indexed"],
        "authentication_origins": [current_origin],
        "changed_storage_types": changed,
        "indexed_db_capture_supported": (
            "indexed_db" in inspect.signature(context.storage_state).parameters),
    }


def _capture_authentication_state(
    context, page, *, capture_indexed_db: bool = True,
) -> tuple[list, list, list]:
    origin = _origin(page.url)
    if not origin:
        raise PersistentSessionError("persistent_session_origin_not_allowed")
    values = page.evaluate("""() => ({
      local: Object.entries(localStorage).map(([name, value]) => ({name, value})),
      session: Object.entries(sessionStorage).map(([name, value]) => ({name, value}))
    })""")
    web_storage = [{
        "origin": origin,
        "local_storage": values["local"],
        "session_storage": values["session"],
    }]
    if not capture_indexed_db or \
            "indexed_db" not in inspect.signature(context.storage_state).parameters:
        if capture_indexed_db and _storage_names(page)["indexed"]:
            raise PersistentSessionError(
                "persistent_session_requires_unsupported_indexeddb")
        indexed_db = []
    else:
        official = context.storage_state(indexed_db=True)
        indexed_db = [{
            "origin": item["origin"],
            "indexedDB": item.get("indexedDB", []),
        } for item in official.get("origins", [])
            if item.get("origin") in APPROVED_ORIGINS and item.get("indexedDB")]
        official.clear()
    return context.cookies(list(APPROVED_ORIGINS)), web_storage, indexed_db


def _web_storage_init_script(web_storage: list[dict]) -> str:
    encoded = json.dumps(web_storage, ensure_ascii=False, separators=(",", ":"))
    return """(() => {
      const approved = %s;
      const state = approved.find(item => item.origin === location.origin);
      if (!state) return;
      for (const item of state.local_storage || []) localStorage.setItem(item.name, item.value);
      for (const item of state.session_storage || []) sessionStorage.setItem(item.name, item.value);
    })();""" % encoded


def _browser_was_closed(page: Any, browser: Any) -> bool:
    """Return whether the operator closed the visible pairing browser.

    Playwright raises when a page/window is closed while the worker is polling.
    Keep this check deliberately defensive so a diagnostic failure is not itself
    mistaken for an authentication/storage failure.
    """
    try:
        if browser is not None and not browser.is_connected():
            return True
    except Exception:
        pass
    try:
        return bool(page is not None and page.is_closed())
    except Exception:
        return False


def _wait_until_operator_closes(page: Any, browser: Any, poll_ms: int = 500) -> None:
    """Keep a successfully paired visible browser open for the operator.

    Saving the encrypted session and closing its temporary browser are separate
    lifecycle actions.  The browser remains visible until its window is closed;
    backend shutdown can still terminate the worker through the supervisor.
    """
    while not _browser_was_closed(page, browser):
        try:
            page.wait_for_timeout(poll_ms)
        except Exception:
            if _browser_was_closed(page, browser):
                return
            raise


def pair(identifier: str, database_path: Path):
    _, persistent, principal = services(database_path)
    from playwright.sync_api import sync_playwright
    page = context = browser = None
    try:
        pairing = persistent.get_pairing(identifier, private=True,principal=principal,
                                         permission="evidence.import")
        if principal is not None and (pairing["tenant_id"],pairing["workspace_id"]) != (
                principal.tenant_id,principal.workspace_id):
            raise PersistentSessionError("tenant_scope_mismatch")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            before_names = {"cookies": [], "local": [], "session": [], "indexed": []}
            persistent.update_pairing(
                identifier, state="waiting_for_manual_login",
                browser_state="visible_manual_login", worker_pid=os.getpid(),
                principal=principal,permission="evidence.import")
            page.goto("https://uat.weimeta.cn/console/billing/logs")
            if _origin(page.url):
                initial = _storage_names(page)
                before_names.update(initial)
                before_names["cookies"] = sorted(
                    item["name"] for item in context.cookies(list(APPROVED_ORIGINS)))
            while True:
                pairing = persistent.get_pairing(identifier, private=True,principal=principal,
                                                 permission="evidence.import")
                if pairing["state"] in {"completed", "stopped", "failed"}:
                    return
                parsed = urlsplit(page.url)
                current = safe_url(page.url) if (
                    parsed.scheme == "https" and
                    (parsed.hostname or "").casefold() in
                    {"uat.weimeta.cn", "admin-uat.weimeta.cn"}) else None
                next_state = pairing["state"]
                if pairing["state"] == "waiting_for_manual_login" and \
                        current == "https://uat.weimeta.cn/console/billing/logs":
                    persistent.update_pairing(
                        identifier, state="checking_authentication_storage",
                        principal=principal,permission="evidence.import",
                        storage_diagnostic_status="checking")
                    persistent.save_storage_diagnostic(
                        identifier, _diagnostic(context, page, before_names),
                        principal=principal)
                    next_state = "waiting_for_confirmation"
                persistent.update_pairing(
                    identifier, current_safe_url=current, state=next_state,
                    principal=principal,permission="evidence.import")
                if next_state != "confirmation_requested":
                    page.wait_for_timeout(500)
                    continue
                if current != "https://uat.weimeta.cn/console/billing/logs" or \
                        "login" in parsed.path.casefold():
                    persistent.update_pairing(
                        identifier, state="authentication_validation_failed",
                        principal=principal,permission="evidence.import",
                        failure_code="persistent_session_reauthentication_required",
                        failure_summary="尚未验证国内 UAT 日志页登录状态。")
                    persistent._event("persistent_session_validation_failed", {
                        "pairing_id": identifier,
                        "failure_code": "persistent_session_reauthentication_required"})
                    continue
                cookies, web_storage, indexed_db = _capture_authentication_state(
                    context, page)
                persistent.complete_pairing(
                    identifier, cookies, web_storage, indexed_db,principal=principal)
                cookies.clear()
                web_storage.clear()
                indexed_db.clear()
                _wait_until_operator_closes(page, browser)
                persistent.update_pairing(
                    identifier, browser_state="closed_by_operator",
                    principal=principal, permission="evidence.import")
                return
    except PersistentSessionError as exc:
        try:
            persistent.update_pairing(
                identifier, state="failed",
                principal=principal,permission="evidence.import",
                browser_state="closed", failure_code=str(exc),
                failure_summary=(
                    "检测到无法按已审核边界恢复的认证存储，持久模式保持阻止。"))
        except Exception:
            pass
    except UatLogReadContractError as exc:
        code = str(exc)
        try:
            current = realtime.get_job(identifier, private=True)
            if current["state"] == "stopping":
                persistent.finalize_stop(
                    identifier, lease_generation, "operator_stop")
            elif current["state"] in ACTIVE_STATES:
                realtime.update_job(
                    identifier, state="failed", stopped_at=utcnow(),
                    browser_context_active=0, error_code=code,
                    stop_reason=code,
                    safe_error_message=(
                        "UAT 日志响应未通过只读范围或分页契约校验。"))
        except Exception:
            pass
    except Exception:
        try:
            if _browser_was_closed(page, browser):
                pairing = persistent.get_pairing(
                    identifier, private=True, principal=principal,
                    permission="evidence.import")
                if pairing["state"] == "completed":
                    persistent.update_pairing(
                        identifier, browser_state="closed_by_operator",
                        principal=principal, permission="evidence.import")
                else:
                    persistent.update_pairing(
                        identifier, state="stopped",
                        browser_state="closed_by_operator",
                        principal=principal, permission="evidence.import",
                        failure_code="persistent_pairing_closed_by_operator",
                        failure_summary="操作员已关闭临时登录窗口；未保存新的登录会话。")
            else:
                persistent.update_pairing(
                    identifier, state="failed", browser_state="closed",
                    principal=principal,permission="evidence.import",
                    failure_code="persistent_session_save_failed",
                    failure_summary="加密登录状态配对工作进程失败。")
        except Exception:
            pass
    finally:
        for resource in (page, context, browser):
            try:
                if resource:
                    resource.close()
            except Exception:
                pass


def synchronize(identifier: str, database_path: Path):
    realtime, persistent, principal = services(database_path)
    from playwright.sync_api import sync_playwright
    page = context = browser = None
    external_browser = False
    heartbeat_stop = threading.Event()
    heartbeat_thread = None
    transport_stage = "worker_initialization"
    try:
        job = realtime.get_job(identifier, private=True)
        if principal is not None and (job["tenant_id"],job["workspace_id"]) != (
                principal.tenant_id,principal.workspace_id):
            raise PersistentSessionError("tenant_scope_mismatch")
        lease_generation = int(job["lease_generation"])
        if persistent.heartbeat_lease_outcome(
                identifier, lease_generation) != "renewed":
            return

        def maintain_lease():
            interval = max(
                5.0, min(30.0, persistent.lease_timeout_seconds / 3.0))
            while not heartbeat_stop.wait(interval):
                try:
                    if persistent.heartbeat_lease_outcome(
                            identifier, lease_generation) != "renewed":
                        return
                except Exception:
                    return

        heartbeat_thread = threading.Thread(
            target=maintain_lease,
            name=f"lease-heartbeat-{identifier[-8:]}",
            daemon=True)
        heartbeat_thread.start()
        realtime.update_job(
            identifier, state="decrypting_session", started_at=utcnow())
        authentication_state = persistent.load_authentication_state(
            job["persistent_session_id"], job["environment_id"],
            job_id=identifier, lease_generation=lease_generation)
        with sync_playwright() as playwright:
            use_formal_chrome = os.getenv(
                "UAT_PERSISTENT_USE_FORMAL_CHROME", "false").lower() == "true"
            realtime.update_job(identifier, state="launching_headless_browser")
            if use_formal_chrome:
                manager = DomesticUatChromeManager()
                target = None
                try:
                    if not manager.status()["running"]:
                        raise DomesticUatChromeError("browser_not_running")
                    transport_stage = "create_worker_target"
                    manager.cleanup_worker_targets()
                    target = manager.create_worker_target(identifier.casefold())
                    transport_stage = "connect_formal_chrome"
                    browser = playwright.chromium.connect_over_cdp(
                        manager._cdp_endpoint(), timeout=45_000)
                    if not browser.contexts:
                        raise DomesticUatChromeError(
                            "persistent_formal_chrome_context_unavailable")
                    context = browser.contexts[0]
                    external_browser = True
                    authentication_state.clear()
                except Exception as exc:
                    # The encrypted project session is the authoritative
                    # background-sync fallback. A stale localhost-only CDP
                    # endpoint must not make automatic read-only collection
                    # depend on an operator window remaining attachable.
                    persistent._event(
                        "persistent_formal_chrome_unavailable_headless_fallback",
                        {"exception_type": type(exc).__name__[:80]}, identifier)
                    if target is not None:
                        try:
                            manager.close_worker_target(target["target_id"])
                        except Exception:
                            pass
                    if browser is not None:
                        try:
                            browser.close()
                        except Exception:
                            pass
                    browser = context = None
                    external_browser = False
            if not external_browser:
                transport_stage = "launch_encrypted_headless_fallback"
                browser = playwright.chromium.launch(headless=True)
                official_state = {
                    "cookies": authentication_state["cookies"],
                    "origins": [{
                        "origin": item["origin"], "localStorage": [],
                        "indexedDB": item["indexedDB"],
                    } for item in authentication_state.get("indexed_db", [])],
                }
                context = browser.new_context(storage_state=official_state)
                official_state.clear()
                context.add_init_script(script=_web_storage_init_script(
                    authentication_state.get("web_storage", [])))
                authentication_state.clear()
            denied_read: dict[str, object] = {}
            query_gate = {"enabled": False}

            def authorize_log_api_read(route):
                if (
                    urlsplit(route.request.url).path == "/api/log/self"
                    and not query_gate["enabled"]
                ):
                    route.abort()
                    return
                try:
                    result = handle_log_api_route(
                        route, lambda: persistent.authorize_http_read(
                            identifier, lease_generation))
                except Exception as exc:
                    denied_read["exception"] = exc
                    route.abort()
                    return
                if result is None or result["authorized"]:
                    return
                denied_read["result"] = result

            transport_stage = "select_worker_target"
            if external_browser:
                page = next(
                    (candidate for candidate in context.pages
                     if candidate.url == target["target_url"]), None)
                if page is None:
                    raise PersistentSessionError(
                        "persistent_formal_chrome_worker_target_unavailable")
            else:
                page = context.new_page()
            # A route on the worker-owned page cannot interfere with the
            # operator's other tabs in the persistent Chrome profile.
            transport_stage = "configure_worker_target"
            page.route("**/*", authorize_log_api_read)
            realtime.update_job(
                identifier, state="validating_authentication",
                browser_context_active=1)
            captured: list[tuple[object, str]] = []
            auth_failures = 0

            def response_seen(response):
                nonlocal auth_failures
                parsed = urlsplit(response.url)
                try:
                    response_port = parsed.port
                except ValueError:
                    return
                if parsed.scheme != "https" or parsed.username or \
                        parsed.password or response_port not in {None, 443} or \
                        (parsed.hostname or "").casefold() not in {
                        "uat.weimeta.cn", "admin-uat.weimeta.cn"}:
                    return
                if response.status in {401, 403}:
                    auth_failures += 1
                    return
                if parsed.path != "/api/log/self" or \
                        "json" not in response.headers.get(
                            "content-type", "").casefold():
                    return
                try:
                    captured.append((response.json(), response.url))
                except Exception:
                    return

            page.on("response", response_seen)
            transport_stage = "validate_authenticated_navigation"
            page.goto("https://uat.weimeta.cn/console/billing/logs")
            if denied_read:
                result = denied_read.get("result")
                if isinstance(result, dict) and result.get("reason") == "stopping":
                    persistent.finalize_stop(
                        identifier, lease_generation, "operator_stop")
                if denied_read.get("exception"):
                    raise PersistentSessionError(
                        "persistent_http_read_authorization_failed")
                return
            parsed = urlsplit(page.url)
            if "login" in parsed.path.casefold() or \
                    safe_url(page.url) != "https://uat.weimeta.cn/console/billing/logs":
                persistent.invalidate_reauthentication(
                    job["persistent_session_id"])
                realtime.update_job(
                    identifier, state="reauthentication_required",
                    stopped_at=utcnow(), browser_context_active=0,
                    error_code="persistent_session_reauthentication_required",
                    safe_error_message="本地加密登录状态需要重新认证。")
                return
            rotated = _capture_authentication_state(context, page)
            try:
                persistent.rotate_authentication_state(
                    job["persistent_session_id"], *rotated,
                    job_id=identifier, lease_generation=lease_generation)
            finally:
                for value in rotated:
                    value.clear()
            realtime.update_job(identifier, state="synchronizing")
            query_gate["enabled"] = True
            billing_context = read_public_billing_context(page)
            persistent._event(
                "persistent_headless_sync_started",
                {"persistent_session_id": job["persistent_session_id"]}, identifier)
            next_poll = time.monotonic()
            retry_count = 0
            while True:
                job = realtime.get_job(identifier, private=True)
                if job["state"] == "stopping":
                    persistent.finalize_stop(
                        identifier, lease_generation, "operator_stop")
                    return
                if job["state"] not in ACTIVE_STATES:
                    persistent._event(
                        "persistent_headless_sync_stopped",
                        {"stop_reason": job["state"]}, identifier)
                    return
                if page.is_closed() or auth_failures >= 3:
                    persistent.invalidate_reauthentication(
                        job["persistent_session_id"])
                    realtime.update_job(
                        identifier, state="reauthentication_required",
                        stopped_at=utcnow(), browser_context_active=0,
                        error_code="persistent_session_reauthentication_required",
                        safe_error_message="平台登录状态已失效，需要重新配对。")
                    return
                captured.clear()
                bounded = persistent.check_bounded_limits(
                    identifier, lease_generation)
                if bounded["bounded"]:
                    if bounded["reason"] == "stopping":
                        persistent.finalize_stop(
                            identifier, lease_generation, "operator_stop")
                    return
                now = time.monotonic()
                if now >= next_poll:
                    heartbeat = persistent.heartbeat_lease_outcome(
                        identifier, lease_generation)
                    if heartbeat == "stopping":
                        persistent.finalize_stop(
                            identifier, lease_generation, "operator_stop")
                        return
                    if heartbeat != "renewed":
                        return
                    realtime.update_job(identifier, last_poll_at=utcnow())
                    query_start = job["date_from_utc"]
                    if (
                        job.get("periodic_polling", True)
                        and job.get("watermark_utc")
                        and int(job.get("current_page") or 1) == 1
                    ):
                        authorized_start = datetime.fromisoformat(
                            job["date_from_utc"])
                        overlap_start = datetime.fromisoformat(
                            job["watermark_utc"]) - timedelta(
                                seconds=WATERMARK_OVERLAP_SECONDS)
                        query_start = max(
                            authorized_start, overlap_start).isoformat()
                    query = DomesticUatLogQuery(
                        date_from_utc=query_start,
                        date_to_utc=job["date_to_utc"],
                        page=int(job.get("current_page") or 1),
                        page_size=int(job.get("page_size") or 100),
                    )
                    try:
                        response = read_billing_page(
                            page, query,
                            use_reviewed_page_query=external_browser)
                    except PersistentSessionError as exc:
                        code = str(exc)
                        retry_count += 1
                        realtime.update_job(
                            identifier,
                            consecutive_failures=retry_count,
                            error_code=code if retry_count > 2 else None,
                            safe_error_message=(
                                "UAT 日志读取发生可重试故障。"
                                if retryable_read_error(code)
                                else "UAT 日志读取失败。"),
                        )
                        if (
                            not job.get("periodic_polling", True)
                            or
                            not retryable_read_error(code)
                            or retry_count > 2
                        ):
                            raise
                        bounded_after_failure = \
                            persistent.check_bounded_limits(
                                identifier, lease_generation)
                        if bounded_after_failure["bounded"]:
                            return
                        page.wait_for_timeout(int(
                            retry_backoff_seconds(
                                retry_count, random.random()) * 1000))
                        next_poll = time.monotonic()
                        continue
                    retry_count = 0
                    if denied_read:
                        result = denied_read.get("result")
                        if isinstance(result, dict) and \
                                result.get("reason") == "stopping":
                            persistent.finalize_stop(
                                identifier, lease_generation, "operator_stop")
                        if denied_read.get("exception"):
                            raise PersistentSessionError(
                                "persistent_http_read_authorization_failed")
                        return
                    validate_response_pagination(
                        response["payload"],
                        response.get("observed_query", query),
                        previous_envelope_sha256=job.get(
                            "last_response_envelope_sha256"),
                    )
                    result = realtime.ingest(
                        identifier,
                        response["payload"],
                        response["source_url"],
                        lease_generation=lease_generation,
                        billing_context=billing_context,
                    )
                    if int(result.get("poll_out_of_range_count") or 0):
                        realtime.update_job(
                            identifier, state="failed", stopped_at=utcnow(),
                            browser_context_active=0,
                            error_code="log_sync_remote_range_filter_ignored",
                            stop_reason="log_sync_remote_range_filter_ignored",
                            safe_error_message=(
                                "远端返回了授权范围外记录；已拒绝记录并停止任务。"))
                        return
                    if not job.get("periodic_polling", True):
                        persistent.finalize_one_shot(
                            identifier,
                            lease_generation,
                            error_code=(
                                "schema_mapping_required"
                                if result.get("schema_mapping_required")
                                else None
                            ),
                        )
                        return
                    pagination = result.get("pagination") or {}
                    page_number = int(pagination.get("page") or query.page)
                    response_page_size = int(
                        pagination.get("page_size") or query.page_size)
                    total = int(pagination.get("total") or 0)
                    if page_number * response_page_size < total:
                        if int(result.get("pages_read") or 0) >= int(
                                job.get("maximum_pages") or 10):
                            realtime.update_job(
                                identifier, state="failed",
                                stopped_at=utcnow(),
                                browser_context_active=0,
                                error_code="log_sync_maximum_pages_reached",
                                stop_reason="bounded_limit_reached")
                            realtime.finalize_batch(
                                identifier, "pagination_incomplete")
                            return
                        realtime.update_job(
                            identifier, current_page=page_number + 1)
                        next_poll = time.monotonic()
                    else:
                        realtime.update_job(identifier, current_page=1)
                        next_poll = next_poll_deadline(
                            time.monotonic(),
                            int(job["poll_interval_seconds"]))
                    continue
                    page.wait_for_load_state("domcontentloaded")
                    if safe_url(page.url) != \
                            "https://uat.weimeta.cn/console/billing/logs":
                        persistent.invalidate_reauthentication(
                            job["persistent_session_id"])
                        realtime.update_job(
                            identifier, state="reauthentication_required",
                            stopped_at=utcnow(), browser_context_active=0,
                            error_code="persistent_session_reauthentication_required",
                            safe_error_message="平台登录状态已失效，需要重新配对。")
                        return
                    rotated = _capture_authentication_state(context, page)
                    try:
                        persistent.rotate_authentication_state(
                            job["persistent_session_id"], *rotated,
                            job_id=identifier,
                            lease_generation=lease_generation)
                    finally:
                        for value in rotated:
                            value.clear()
                    next_poll = None
                    continue
                remaining = max(0.001, next_poll - now)
                page.wait_for_timeout(
                    min(500, max(1, int(remaining * 1000))))
    except PersistentSessionError as exc:
        code = str(exc)
        try:
            current = realtime.get_job(identifier, private=True)
            if current["state"] == "stopping":
                persistent.finalize_stop(
                    identifier, lease_generation, "operator_stop")
            elif current["state"] in ACTIVE_STATES:
                if code == "persistent_session_reauthentication_required":
                    persistent.invalidate_reauthentication(
                        current["persistent_session_id"])
                realtime.update_job(
                    identifier,
                    state=(
                        "persistent_session_expired"
                        if code == "persistent_session_expired"
                        else "reauthentication_required"
                        if code == "persistent_session_reauthentication_required"
                        else "failed"
                    ),
                    stopped_at=utcnow(), browser_context_active=0,
                    error_code=code, safe_error_message="本地加密登录状态不可用。")
        except Exception:
            pass
    except Exception as exc:
        try:
            persistent._event(
                "persistent_browser_transport_failed",
                {
                    "transport_stage": transport_stage,
                    "exception_type": type(exc).__name__[:80],
                }, identifier)
            current = realtime.get_job(identifier, private=True)
            if current["state"] == "stopping":
                persistent.finalize_stop(
                    identifier, lease_generation, "operator_stop")
            elif current["state"] in ACTIVE_STATES:
                realtime.update_job(
                    identifier, state="failed", stopped_at=utcnow(),
                    browser_context_active=0,
                    error_code="persistent_headless_launch_failed",
                    safe_error_message="无界面同步工作进程失败。")
        except Exception:
            pass
    finally:
        heartbeat_stop.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        resources = (page,) if external_browser else (page, context, browser)
        for resource in resources:
            try:
                if resource:
                    resource.close()
            except Exception:
                pass
        try:
            generation = locals().get("lease_generation")
            current = realtime.get_job(identifier, private=True)
            if generation is not None and current["state"] == "stopping":
                persistent.finalize_stop(
                    identifier, generation, "operator_stop")
            elif generation is not None and current["state"] in ACTIVE_STATES:
                realtime.update_job(
                    identifier, state="failed", stopped_at=utcnow(),
                    browser_context_active=0,
                    error_code="persistent_worker_exited",
                    safe_error_message="无界面同步工作进程已结束。")
            persistent.release_lease(
                identifier, "worker_finally", generation=generation)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("pairing", "persistent"), required=True)
    parser.add_argument("--identifier", required=True)
    args = parser.parse_args()
    database_path = Path(os.environ["ROUTING_CONSOLE_DATABASE_PATH"])
    if args.kind == "pairing":
        pair(args.identifier, database_path)
    else:
        synchronize(args.identifier, database_path)


if __name__ == "__main__":
    main()
