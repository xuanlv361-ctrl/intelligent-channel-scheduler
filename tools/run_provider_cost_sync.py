"""Run one audited Provider-log synchronization using the saved Chrome session.

This utility never accepts or reads a Provider credential.  It re-captures the
already authenticated project Chrome session when needed, starts the fenced
persistent worker, waits for a complete pagination pass, and seals the batch.
"""
from __future__ import annotations

import http.cookiejar
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


BASE = "http://127.0.0.1:5174"
ORIGIN = BASE
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "evidence" / "provider_cost_sync"


class Api:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def call(self, method: str, path: str, body: dict | None = None,
             *, retry_csrf: bool = True) -> dict:
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "Origin": ORIGIN}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = Request(BASE + path, method=method, data=payload, headers=headers)
        try:
            response = self.opener.open(request, timeout=30)
            raw = response.read()
        except HTTPError as exc:
            rotated = exc.headers.get("X-CSRF-Token")
            if rotated:
                self.csrf = rotated
            detail = json.loads(exc.read().decode("utf-8"))
            code = str((detail.get("detail") or {}).get("code") or "")
            if retry_csrf and code == "csrf_token_replay_detected":
                refreshed = self.call("GET", "/api/v1/security/session/csrf",
                                      retry_csrf=False)
                self.csrf = str(refreshed["csrf_token"])
                return self.call(method, path, body, retry_csrf=False)
            raise RuntimeError(f"api_failed:{exc.code}:{detail}") from exc
        rotated = response.headers.get("X-CSRF-Token")
        if rotated:
            self.csrf = rotated
        return json.loads(raw.decode("utf-8"))

    def bootstrap(self) -> None:
        result = self.call("POST", "/api/v1/security/session/bootstrap", {})
        self.csrf = str(result["csrf_token"])


def ensure_session(api: Api) -> dict:
    status = api.call("GET", "/api/v1/log-sync/persistent/status")
    session = status.get("session") or {}
    if session.get("state") == "active" and session.get(
            "authentication_status") == "verified":
        return session
    pairing = status.get("pairing") or {}
    if pairing.get("state") not in {"waiting_for_operator", "authenticated"}:
        pairing = api.call("POST", "/api/v1/log-sync/persistent/reauthentication/start", {
            "environment_id": "china_uat", "ttl_hours": 8,
            "explicit_confirmation": True,
        })
    pairing_id = str(pairing["pairing_id"])
    api.call("POST", "/api/v1/log-sync/persistent/browser/open-or-focus",
             {"explicit_confirmation": True})
    checked = None
    for attempt in range(2):
        try:
            checked = api.call("POST", (
                f"/api/v1/log-sync/persistent/pairing/{pairing_id}/check-authentication"),
                {"explicit_confirmation": True})
            break
        except (RuntimeError, TimeoutError):
            if attempt:
                raise
            time.sleep(2)
    assert checked is not None
    refreshed = api.call("GET", "/api/v1/log-sync/persistent/status")
    refreshed_session = refreshed.get("session") or {}
    if (refreshed_session.get("state") == "active" and
            refreshed_session.get("authentication_status") == "verified"):
        return refreshed_session
    if checked.get("authentication_status") not in {"verified", "authenticated"}:
        raise RuntimeError("provider_authentication_not_verified")
    saved = api.call("POST", (
        f"/api/v1/log-sync/persistent/pairing/{pairing_id}/encrypted-save"),
        {"explicit_confirmation": True})
    return saved


def main() -> None:
    api = Api()
    api.bootstrap()
    session = ensure_session(api)
    now = datetime.now(timezone.utc)
    job = api.call("POST", "/api/v1/log-sync/persistent/jobs", {
        "environment_id": "china_uat",
        "date_from": (now - timedelta(days=7)).isoformat(),
        "date_to": (now - timedelta(seconds=2)).isoformat(),
        "timezone": "Asia/Shanghai",
        "periodic_polling": True,
        "sync_interval_seconds": 7,
        "maximum_records": 1000,
        "maximum_http_reads": 50,
        "maximum_records_observed": 1000,
        "maximum_records_accepted": 1000,
        "maximum_elapsed_seconds": 600,
        "page_size": 100,
        "maximum_pages": 50,
        "persistent_session_id": session["persistent_session_id"],
        "explicit_confirmation": True,
    })
    job_id = str(job["sync_job_id"])
    last = job
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        time.sleep(3)
        status = api.call("GET", "/api/v1/log-sync/persistent/status")
        # ``active_job`` is intentionally a compact projection; the full
        # pagination counters live on ``latest_job``.
        last = status.get("latest_job") or status.get("active_job") or {}
        state = str(last.get("state") or "")
        pages = int(last.get("pages_read") or 0)
        current_page = int(last.get("current_page") or 1)
        if state in {"failed", "stopped", "completed"}:
            break
        # A complete pass ends after the final non-full Provider page and the
        # worker resets its next page to 1.  Five pages is a conservative floor
        # for the current real source while still relying on source has_more.
        if pages >= 5 and current_page == 1:
            last = api.call("POST", f"/api/v1/log-sync/persistent/jobs/{job_id}/stop", {})
            break
    else:
        last = api.call("POST", f"/api/v1/log-sync/persistent/jobs/{job_id}/stop", {})
        raise RuntimeError("provider_sync_timeout")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    evidence = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "sync_job_id": job_id,
        "state": last.get("state"),
        "pages_read": last.get("pages_read"),
        "records_observed": last.get("collected_count"),
        "records_inserted": last.get("inserted_count"),
        "records_deduplicated": last.get("duplicate_count"),
        "error_code": last.get("error_code"),
        "watermark_utc": last.get("watermark_utc"),
        "source_cursor": last.get("source_cursor"),
    }
    path = OUTPUT / f"{job_id}.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
