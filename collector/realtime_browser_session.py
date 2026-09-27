"""Visible, non-persistent, read-only near-real-time log synchronization worker."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from backend.realtime_log_sync_service import (
    ACTIVE_STATES, RealtimeLogSyncService, safe_url, utcnow,
)
from collector.collector_worker import resolve_worker_principal


def run(job_id: str, database_path: Path) -> None:
    principal, authorization = resolve_worker_principal(database_path, "worker")
    runtime = EnvironmentRuntimeSettings(database_path)
    service = RealtimeLogSyncService(
        database_path,
        Path(__file__).resolve().parents[1] / "output" / "unified_uat_execution_v3.jsonl",
        runtime,
        authorization=authorization,
    )
    browser = context = page = None
    try:
        job = service.get_job(job_id, private=True,principal=principal,
                              permission="evidence.import")
        contract = service.resolve_environment(job["environment_id"])
        allowed = set(contract["allowed_hosts"])
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            service.update_job(
                job_id, state="waiting_for_manual_login",
                principal=principal,
                browser_context_active=1, current_safe_url=None)
            service.update_job(job_id, network_called=1,principal=principal)
            page.goto(contract["log_page_url"])
            captured_payloads: list[tuple[object, str]] = []
            auth_failures = 0

            def response_seen(response):
                nonlocal auth_failures
                parsed = urlsplit(response.url)
                if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed:
                    return
                if response.status in {401, 403}:
                    auth_failures += 1
                    return
                if parsed.path != "/api/log/self":
                    return
                content_type = response.headers.get("content-type", "")
                if "json" not in content_type.casefold():
                    return
                try:
                    captured_payloads.append((response.json(), response.url))
                except Exception:
                    return

            page.on("response", response_seen)
            next_poll = 0.0
            while True:
                job = service.get_job(job_id, private=True,principal=principal,
                                      permission="evidence.import")
                if job["state"] not in ACTIVE_STATES:
                    return
                if page.is_closed():
                    service.update_job(
                        job_id, state="stopped", stopped_at=utcnow(),
                        principal=principal,
                        browser_context_active=0, error_code="log_sync_browser_closed",
                        safe_error_message="可见浏览器窗口已关闭。")
                    return
                parsed = urlsplit(page.url)
                current = safe_url(page.url) if (
                    parsed.scheme == "https" and (parsed.hostname or "").lower() in allowed
                ) else None
                next_state = (
                    "waiting_for_operator_confirmation"
                    if job["state"] == "waiting_for_manual_login"
                    and current == contract["log_page_url"]
                    else job["state"]
                )
                service.update_job(job_id, current_safe_url=current, state=next_state,
                                   principal=principal)
                if "login" in parsed.path.casefold() or auth_failures >= 3:
                    service.mark_session_expired(job_id,principal=principal)
                    return
                if job["state"] != "syncing":
                    page.wait_for_timeout(500)
                    continue
                now = time.monotonic()
                if now >= next_poll:
                    service.update_job(job_id, last_poll_at=utcnow(),principal=principal)
                    page.goto(contract["log_page_url"])
                    page.wait_for_load_state("domcontentloaded")
                    next_poll = now + job["poll_interval_seconds"]
                while captured_payloads:
                    payload, source = captured_payloads.pop(0)
                    service.ingest(job_id, payload, source,principal=principal)
                    if service.get_job(job_id,principal=principal,
                                       permission="evidence.import")["inserted_count"] >= job["maximum_records"]:
                        service.update_job(
                            job_id, state="completed", stopped_at=utcnow(),
                            principal=principal,
                            browser_context_active=0)
                        return
                page.wait_for_timeout(500)
    except Exception:
        try:
            job = service.get_job(job_id, private=True,principal=principal,
                                  permission="evidence.import")
            if job["state"] in ACTIVE_STATES:
                service.update_job(
                    job_id, state="failed", stopped_at=utcnow(),
                    principal=principal,
                    browser_context_active=0, error_code="log_sync_worker_failed",
                    safe_error_message="近实时日志同步工作进程失败。")
        except Exception:
            pass
    finally:
        for resource in (page, context, browser):
            try:
                if resource:
                    resource.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    run(args.job_id, Path(os.environ["ROUTING_CONSOLE_DATABASE_PATH"]))


if __name__ == "__main__":
    main()
