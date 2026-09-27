"""Trusted UI-launched collector worker.

The only command-line input is a server-generated run id. All URLs and limits are
resolved again from the local SQLite run and environment registry.
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from backend.browser_import_service import BrowserImportService
from backend.collector_run_service import CollectorRunError, CollectorRunService
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from collector.evidence_writer import EvidenceWriter
from collector.log_page_parser import in_date_range, parse_dom_table, parse_structured
from collector.network_response_capture import capture_json_response
from collector.pagination_controller import PaginationController, PaginationLimits
from src.services.console_service import Store
from backend.security import (AuthorizationService, EnterpriseIdentityConfig,
                              PrincipalResolver, ServiceIdentityManager)


def resolve_worker_principal(database_path: Path, expected_service: str):
    """Validate the short-lived child-process credential; never trust CLI scope."""
    token = os.getenv("ROUTING_SERVICE_TOKEN")
    nonce = os.getenv("ROUTING_SERVICE_REQUEST_NONCE")
    signing_key = os.getenv("ROUTING_SERVICE_SIGNING_KEY")
    required = os.getenv("ROUTING_ENTERPRISE_SECURITY_REQUIRED") == "1"
    if not token or not nonce or not signing_key:
        if required:
            raise PermissionError("worker_service_identity_required")
        return None, None
    root = Path(__file__).resolve().parents[1]
    authorization = AuthorizationService(root / "config" / "rbac_permissions_v1.json")
    resolver = PrincipalResolver(
        EnterpriseIdentityConfig.load(root / "config" / "enterprise_identity_v1.json"),
        authorization)
    manager = ServiceIdentityManager(
        database_path, root / "config" / "service_identities_v1.json",
        signing_key=signing_key.encode("utf-8"), resolver=resolver)
    principal = manager.validate(
        token, os.getenv("ROUTING_SERVICE_AUDIENCE", "worker-api"),
        request_nonce=nonce, correlation_id=f"worker-{expected_service}-{os.getpid()}")
    if principal.principal_id != f"service:{expected_service}":
        raise PermissionError("worker_service_name_mismatch")
    return principal, authorization


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_current_url(url: str, allowed_hosts: set[str]) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts:
        return ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _exact_log_page_visible(current_url: str, log_url: str, allowed_hosts: set[str]) -> bool:
    return bool(_safe_current_url(current_url, allowed_hosts) == _safe_current_url(log_url, allowed_hosts))


def run(run_id: str, database_path: Path) -> None:
    principal, authorization = resolve_worker_principal(database_path, "collector")
    runtime = EnvironmentRuntimeSettings(database_path)
    runs = CollectorRunService(database_path, lambda: runtime, authorization)
    imports = BrowserImportService(database_path, Store(database_path), lambda: runtime,
                                   authorization)
    browser = context = page = None
    started = time.monotonic()
    try:
        record = runs.get(run_id, include_private=True,principal=principal,
                          permission="evidence.import")
        contract = runs.verify_runtime(run_id,principal=principal)
        allowed = set(contract["allowed_hosts"])
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            runs.transition(run_id, "awaiting_manual_login", principal=principal,
                            permission="evidence.import",last_heartbeat_at=_now())
            page.goto(contract["console_url"])
            while True:
                current = runs.get(run_id, include_private=True,principal=principal,
                                   permission="evidence.import")
                if current["stop_requested"]:
                    runs.transition(run_id, "stopped", principal=principal,
                                    permission="evidence.import",finished_at=_now())
                    return
                if page.is_closed():
                    runs.transition(run_id, "browser_closed", principal=principal,
                                    permission="evidence.import",error_code="collector_browser_closed",
                                    safe_error_message="浏览器窗口已关闭。")
                    return
                safe_url = _safe_current_url(page.url, allowed)
                runs.update(run_id, principal=principal,permission="evidence.import",
                            current_safe_url=safe_url, last_heartbeat_at=_now(),
                            elapsed_ms=int((time.monotonic() - started) * 1000))
                if current["status"] == "login_confirmed":
                    break
                page.wait_for_timeout(750)

            captured: list[dict] = []
            rejected = duplicates = 0
            structure_recognized = False
            limits = PaginationLimits(record["maximum_pages"], record["maximum_records"],
                                      record["page_delay_ms"])
            pager = PaginationController(limits, None)
            writer = EvidenceWriter(Path("evidence/uat_browser_collector"))

            def response_seen(response):
                nonlocal rejected, duplicates, structure_recognized
                try:
                    parsed = urlsplit(response.url)
                    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed:
                        return
                    # Reuse the established bounded JSON capture after its host check.
                    item = capture_json_response(response.url, response.headers.get("content-type", ""),
                                                 response.body(), allowed_hosts=allowed)
                    if not item:
                        return
                    records = parse_structured(item.payload, item.source_url, _now())
                    structure_recognized = structure_recognized or bool(records)
                    writer.write(run_id, response.url, "structured_log_response",
                                 item.content_type, item.payload, len(records))
                    for candidate in records:
                        if not in_date_range(candidate, record["date_from"], record["date_to"]):
                            rejected += 1
                        elif not pager.unique(candidate):
                            duplicates += 1
                        elif len(captured) < limits.maximum_records:
                            captured.append(candidate)
                except Exception:
                    return

            page.on("response", response_seen)
            runs.transition(run_id, "collecting", principal=principal,
                            permission="evidence.import",last_heartbeat_at=_now())
            for page_number in range(1, limits.maximum_pages + 1):
                latest = runs.get(run_id, include_private=True,principal=principal,
                                  permission="evidence.import")
                if latest["stop_requested"]:
                    runs.transition(run_id, "stopped", principal=principal,
                                    permission="evidence.import",finished_at=_now())
                    return
                page.goto(contract["log_page_url"])
                page.wait_for_load_state("domcontentloaded")
                html = page.content()
                dom_records = parse_dom_table(html, {}, _safe_current_url(page.url, allowed), _now())
                structure_recognized = structure_recognized or "<table" in html.casefold()
                writer.write(run_id, page.url, "visible_dom_table", "text/html",
                             {"table_records": dom_records}, len(dom_records))
                for candidate in dom_records:
                    if len(captured) >= limits.maximum_records:
                        break
                    if not in_date_range(candidate, record["date_from"], record["date_to"]):
                        rejected += 1
                    elif not pager.unique(candidate):
                        duplicates += 1
                    else:
                        captured.append(candidate)
                runs.update(
                    run_id, principal=principal,permission="evidence.import",
                    current_page=page_number, captured_count=len(captured),
                    accepted_count=len(captured), duplicate_count=duplicates, rejected_count=rejected,
                    current_safe_url=_safe_current_url(page.url, allowed), last_heartbeat_at=_now(),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                if len(captured) >= limits.maximum_records:
                    break
                page.wait_for_timeout(limits.delay_ms)

            body = {
                "collection_id": f"COL-{run_id[5:21]}", "source_type": record["source_type"],
                "environment_id": record["environment_id"], "structure_recognized": structure_recognized,
                "records": captured, "captured_count": len(captured),
                "duplicate_count": duplicates, "rejected_count": rejected,
            }
            preview = imports.preview(body,principal=principal,permission="evidence.import")
            runs.save_preview(run_id, preview,principal=principal)
    except Exception as exc:
        try:
            current = runs.get(run_id, include_private=True,principal=principal,
                               permission="evidence.import")
            if current["status"] not in {"stopped", "browser_closed", "failed", "heartbeat_lost"}:
                code = str(exc) if isinstance(exc, CollectorRunError) else "collector_worker_failed"
                runs.transition(run_id, "failed", principal=principal,
                                permission="evidence.import",error_code=code,
                                safe_error_message="采集进程未能完成，请检查浏览器与本地服务。")
        except Exception:
            pass
    finally:
        for item in (page, context, browser):
            try:
                if item:
                    item.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    path = Path(os.environ["ROUTING_CONSOLE_DATABASE_PATH"])
    run(args.run_id, path)


if __name__ == "__main__":
    main()
