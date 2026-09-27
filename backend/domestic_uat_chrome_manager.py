"""Independent Google Chrome lifecycle for domestic-UAT manual authentication."""
from __future__ import annotations

import json
import os
import base64
import binascii
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


LOGIN_URL = "https://uat.weimeta.cn/console/billing/logs"
APPROVED_ORIGINS = {
    "https://uat.weimeta.cn", "https://admin-uat.weimeta.cn",
}
ACTIVE_STATES = {
    "browser_starting", "waiting_for_operator", "authentication_in_progress",
    "authenticated", "reconnecting",
}


class DomesticUatChromeError(ValueError):
    pass


class DomesticUatChromeManager:
    """Owns a dedicated Chrome profile without owning Chrome's lifetime.

    Chrome is launched as a detached process.  Short-lived API requests connect
    over loopback CDP and disconnect without closing the browser.  The generated
    CDP port remains private inside the dedicated profile directory.
    """

    def __init__(
        self,
        *,
        local_app_data: str | Path | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        chrome_candidates: list[str | Path] | None = None,
        cdp_connector: Callable[[str], Any] | None = None,
    ):
        configured_base = local_app_data or os.getenv("LOCALAPPDATA")
        if not configured_base:
            raise DomesticUatChromeError("local_app_data_unavailable")
        base = Path(configured_base).resolve()
        self.state_root = base / "IntelligentChannelScheduler"
        self.profile_directory = (
            self.state_root / "browser-profile" / "domestic-uat-chrome")
        self.runtime_directory = self.state_root / "run"
        self.state_file = self.runtime_directory / "domestic-uat-chrome.json"
        self.lock_file = self.runtime_directory / "domestic-uat-chrome.lock"
        self.popen = popen
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.sleep = sleep
        self._thread_lock = threading.Lock()
        self._opener = build_opener(ProxyHandler({}))
        self._cdp_connector = cdp_connector
        self._playwright_driver = None
        self._inspection_browser = None
        self._candidates = [Path(item) for item in (chrome_candidates or [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
            "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) /
            "Google/Chrome/Application/chrome.exe",
            base / "Google/Chrome/Application/chrome.exe",
        ])]

    def chrome_executable(self) -> Path:
        for candidate in self._candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise DomesticUatChromeError("chrome_not_installed")

    @contextmanager
    def _exclusive_launch(self):
        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.lock_file.open("a+b") as handle:
            if self.lock_file.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_state(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: str, **details: Any) -> None:
        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        safe = {
            "schema_version": "domestic_uat_chrome_runtime_v1",
            "state": state,
            "updated_at": self.clock().astimezone(timezone.utc).isoformat(),
            "profile_scope": "domestic-uat-chrome",
            "browser_family": "Google Chrome",
            **{key: value for key, value in details.items()
               if key in {"launcher_pid", "started_at", "last_error_code"}},
        }
        temporary = self.state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(safe, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_file)

    def _devtools_port(self) -> int | None:
        try:
            first = (self.profile_directory / "DevToolsActivePort").read_text(
                encoding="utf-8").splitlines()[0].strip()
            port = int(first)
            return port if 1024 <= port <= 65535 else None
        except (OSError, ValueError, IndexError):
            return None

    def _request_json(self, path: str, method: str = "GET") -> Any:
        port = self._devtools_port()
        if port is None:
            raise DomesticUatChromeError("browser_not_running")
        request = Request(
            f"http://127.0.0.1:{port}{path}", method=method,
            headers={"Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=1.5) as response:
                if int(getattr(response, "status", 0)) != 200:
                    raise DomesticUatChromeError("browser_error")
                return json.loads(response.read(1024 * 1024).decode("utf-8"))
        except DomesticUatChromeError:
            raise
        except Exception as exc:
            raise DomesticUatChromeError("browser_not_running") from exc

    def _request_action(self, path: str, method: str = "GET") -> None:
        """Execute a loopback CDP action whose successful body may be empty.

        Chrome's ``/json/activate`` endpoint returns HTTP 200 with no JSON
        payload.  Keeping action and query handling separate prevents a
        successful focus operation from being misclassified as a dead browser.
        """
        port = self._devtools_port()
        if port is None:
            raise DomesticUatChromeError("browser_not_running")
        request = Request(
            f"http://127.0.0.1:{port}{path}", method=method,
            headers={"Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=1.5) as response:
                if int(getattr(response, "status", 0)) != 200:
                    raise DomesticUatChromeError("browser_error")
                response.read(1024 * 1024)
        except DomesticUatChromeError:
            raise
        except Exception as exc:
            raise DomesticUatChromeError("browser_not_running") from exc

    def _cdp_endpoint(self) -> str:
        version = self._request_json("/json/version")
        websocket = str(version.get("webSocketDebuggerUrl") or "")
        parsed = urlsplit(websocket)
        port = self._devtools_port()
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost"} \
                or parsed.port != port:
            raise DomesticUatChromeError("browser_debug_endpoint_invalid")
        return f"http://127.0.0.1:{port}"

    def create_worker_target(self, marker: str) -> dict[str, str]:
        """Create one isolated, worker-owned tab through loopback CDP.

        Creating the target before Playwright attaches avoids the Chrome 150
        default-context ``new_page`` deadlock while keeping operator tabs out
        of the collector's page-scoped routing.  The returned structure never
        exposes the debugging port or websocket URL.
        """
        if not marker or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in marker):
            raise DomesticUatChromeError("browser_worker_marker_invalid")
        target_url = f"about:blank#ics-worker-{marker}"
        target = self._request_json(
            f"/json/new?{quote(target_url, safe='')}", method="PUT")
        target_id = str(target.get("id") or "") if isinstance(target, dict) else ""
        returned_url = str(target.get("url") or "") if isinstance(target, dict) else ""
        if not target_id or returned_url != target_url:
            raise DomesticUatChromeError("browser_worker_target_create_failed")
        return {"target_id": target_id, "target_url": target_url}

    def close_worker_target(self, target_id: str) -> None:
        """Close only a broker-created internal worker target."""
        if not target_id or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
            for char in target_id
        ):
            raise DomesticUatChromeError("browser_worker_target_id_invalid")
        self._request_action(f"/json/close/{target_id}")

    def cleanup_worker_targets(self) -> int:
        """Remove stale internal tabs without touching operator UAT tabs."""
        targets = self._request_json("/json/list")
        if not isinstance(targets, list):
            raise DomesticUatChromeError("browser_worker_target_list_invalid")
        removed = 0
        for target in targets:
            if not isinstance(target, dict):
                continue
            target_id = str(target.get("id") or "")
            target_url = str(target.get("url") or "")
            if not target_url.startswith("about:blank#ics-worker-"):
                continue
            try:
                self.close_worker_target(target_id)
                removed += 1
            except DomesticUatChromeError:
                continue
        return removed

    def _is_running(self) -> bool:
        try:
            self._cdp_endpoint()
            return True
        except DomesticUatChromeError:
            return False

    def status(self) -> dict[str, Any]:
        try:
            self.chrome_executable()
        except DomesticUatChromeError:
            return self._public_status("chrome_not_installed", False)
        persisted = self._read_state()
        if self._is_running():
            state = str(persisted.get("state") or "waiting_for_operator")
            if state not in ACTIVE_STATES:
                state = "waiting_for_operator"
            return self._public_status(state, True)
        prior = str(persisted.get("state") or "browser_not_running")
        state = "browser_closed" if prior in ACTIVE_STATES else prior
        if state not in {
            "browser_not_running", "browser_closed", "cancelled", "expired",
            "browser_error", "chrome_not_installed",
        }:
            state = "browser_not_running"
        if state == "browser_closed" and prior != "browser_closed":
            self._write_state("browser_closed")
        return self._public_status(state, False)

    @staticmethod
    def _public_status(state: str, running: bool) -> dict[str, Any]:
        return {
            "state": state,
            "running": bool(running),
            "browser_family": "Google Chrome",
            "profile_scope": "domestic-uat-chrome",
            "profile_persistent": True,
            "debug_scope": "127.0.0.1_only",
            "login_url": LOGIN_URL,
        }

    def _focus_existing(self) -> None:
        targets = self._request_json("/json/list")
        approved = [item for item in targets if str(item.get("url") or "").startswith(
            ("https://uat.weimeta.cn/", "https://admin-uat.weimeta.cn/"))]
        if approved:
            target_id = quote(str(approved[0].get("id") or ""), safe="")
            if not target_id:
                raise DomesticUatChromeError("browser_error")
            self._request_action(f"/json/activate/{target_id}")
            return
        self._request_action(
            f"/json/new?{quote(LOGIN_URL, safe=':/')}", method="PUT")

    def open_or_focus(self) -> dict[str, Any]:
        with self._exclusive_launch():
            if self._is_running():
                self._focus_existing()
                current = self._read_state()
                state = str(current.get("state") or "waiting_for_operator")
                if state not in ACTIVE_STATES:
                    state = "waiting_for_operator"
                self._write_state(state, launcher_pid=current.get("launcher_pid"))
                return {**self.status(), "action": "focused_existing"}
            executable = self.chrome_executable()
            self.profile_directory.mkdir(parents=True, exist_ok=True)
            self._write_state("browser_starting")
            flags = 0
            if os.name == "nt":
                flags = (
                    getattr(subprocess, "DETACHED_PROCESS", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
            process = self.popen([
                str(executable),
                f"--user-data-dir={self.profile_directory}",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=0",
                "--remote-allow-origins=http://127.0.0.1",
                "--no-first-run", "--no-default-browser-check",
                "--disable-background-mode",
                "--disable-features=PasswordManagerOnboarding,PasswordLeakDetection",
                "--new-window", LOGIN_URL,
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=False, close_fds=True,
                creationflags=flags)
            started_at = self.clock().astimezone(timezone.utc).isoformat()
            self._write_state(
                "browser_starting", launcher_pid=int(process.pid),
                started_at=started_at)
            deadline = self.monotonic() + 20.0
            while self.monotonic() < deadline:
                if self._is_running():
                    self._write_state(
                        "waiting_for_operator", launcher_pid=int(process.pid),
                        started_at=started_at)
                    return {**self.status(), "action": "opened_new"}
                if process.poll() is not None:
                    self._write_state("browser_error", last_error_code="chrome_start_failed")
                    raise DomesticUatChromeError("chrome_start_failed")
                self.sleep(0.2)
            self._write_state("browser_error", last_error_code="chrome_start_timeout")
            raise DomesticUatChromeError("chrome_start_timeout")

    def mark_authentication_in_progress(self) -> dict[str, Any]:
        if not self._is_running():
            raise DomesticUatChromeError("browser_not_running")
        current = self._read_state()
        self._write_state(
            "authentication_in_progress",
            launcher_pid=current.get("launcher_pid"),
            started_at=current.get("started_at"))
        return self.status()

    def mark_authenticated(self) -> dict[str, Any]:
        if not self._is_running():
            raise DomesticUatChromeError("browser_not_running")
        current = self._read_state()
        self._write_state(
            "authenticated", launcher_pid=current.get("launcher_pid"),
            started_at=current.get("started_at"))
        return self.status()

    def _inspection_connection(self):
        """Reuse a CDP inspection connection without owning Chrome lifetime."""
        if self._inspection_browser is not None:
            try:
                if self._inspection_browser.is_connected():
                    return self._inspection_browser
            except Exception:
                self._inspection_browser = None
        endpoint = self._cdp_endpoint()
        if self._cdp_connector is not None:
            self._inspection_browser = self._cdp_connector(endpoint)
            return self._inspection_browser
        from playwright.sync_api import sync_playwright
        if self._playwright_driver is None:
            self._playwright_driver = sync_playwright().start()
        self._inspection_browser = (
            self._playwright_driver.chromium.connect_over_cdp(endpoint))
        return self._inspection_browser

    def capture_authenticated_session(self) -> dict[str, Any]:
        """Read the approved authenticated tab without closing its Chrome."""
        if not self._is_running():
            raise DomesticUatChromeError("browser_not_running")
        self.mark_authentication_in_progress()
        try:
            from collector.persistent_browser_session import (
                _capture_authentication_state, _diagnostic, _storage_names,
            )
            browser = self._inspection_connection()
            with nullcontext(browser):
                approved_page = None
                approved_context = None
                for context in browser.contexts:
                    for page in context.pages:
                        parsed = urlsplit(page.url)
                        if parsed.scheme == "https" and \
                                (parsed.hostname or "").casefold() == "uat.weimeta.cn" and \
                                parsed.path == "/console/billing/logs" and \
                                not parsed.query and not parsed.fragment:
                            approved_page = page
                            approved_context = context
                            break
                    if approved_page is not None:
                        break
                if approved_page is None or approved_context is None:
                    raise DomesticUatChromeError("authentication_not_confirmed")
                storage_names = _storage_names(approved_page)
                if storage_names["indexed"]:
                    raise DomesticUatChromeError(
                        "persistent_session_requires_unsupported_indexeddb")
                cookies, web_storage, indexed_db = _capture_authentication_state(
                    approved_context, approved_page, capture_indexed_db=False)
                now_epoch = self.clock().astimezone(timezone.utc).timestamp()
                expiries = [
                    float(item.get("expires") or 0) for item in cookies
                    if float(item.get("expires") or 0) > now_epoch
                ]
                for item in cookies:
                    token_expiry = self._jwt_expiry(item.get("value"), now_epoch)
                    if token_expiry is not None:
                        expiries.append(token_expiry)
                for item in web_storage:
                    token_expiry = self._jwt_expiry(item.get("value"), now_epoch)
                    if token_expiry is not None:
                        expiries.append(token_expiry)
                if not expiries:
                    cookies.clear()
                    web_storage.clear()
                    indexed_db.clear()
                    raise DomesticUatChromeError("remote_session_expiry_unavailable")
                diagnostic = _diagnostic(
                    approved_context, approved_page,
                    {"cookies": [], "local": [], "session": [], "indexed": []})
                remote_expires_at = datetime.fromtimestamp(
                    min(expiries), tz=timezone.utc).isoformat()
                return {
                    "cookies": cookies,
                    "web_storage": web_storage,
                    "indexed_db": indexed_db,
                    "diagnostic": diagnostic,
                    "remote_expires_at": remote_expires_at,
                }
        except DomesticUatChromeError:
            current = self._read_state()
            self._write_state(
                "waiting_for_operator", launcher_pid=current.get("launcher_pid"),
                started_at=current.get("started_at"))
            raise
        except Exception as exc:
            current = self._read_state()
            self._write_state(
                "browser_error", launcher_pid=current.get("launcher_pid"),
                started_at=current.get("started_at"),
                last_error_code="authentication_check_failed")
            raise DomesticUatChromeError("authentication_check_failed") from exc

    @staticmethod
    def _jwt_expiry(value: Any, now_epoch: float) -> float | None:
        """Return a JWT-shaped expiry claim without retaining token material."""
        if not isinstance(value, str) or len(value) > 16384:
            return None
        candidates = [value]
        try:
            parsed_json = json.loads(value)
            if isinstance(parsed_json, dict):
                candidates.extend(
                    str(item) for item in parsed_json.values()
                    if isinstance(item, str))
        except (TypeError, ValueError):
            pass
        for candidate in candidates:
            parts = candidate.split(".")
            if len(parts) != 3:
                continue
            try:
                payload = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(
                    payload.encode("ascii")).decode("utf-8"))
                expiry = float(claims.get("exp"))
            except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
                continue
            if now_epoch < expiry <= now_epoch + (366 * 24 * 60 * 60):
                return expiry
        return None

    def explicit_close(self, explicit_confirmation: bool) -> dict[str, Any]:
        if not explicit_confirmation:
            raise DomesticUatChromeError("browser_close_confirmation_required")
        if not self._is_running():
            self._write_state("browser_closed")
            return self.status()
        # Browser.close is deliberately confined to this explicit operator
        # action. Normal API completion, backend shutdown and inspector cleanup
        # only disconnect from CDP and can never terminate Chrome.
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(
                    self._cdp_endpoint())
                browser.close()
        except Exception as exc:
            raise DomesticUatChromeError("browser_close_failed") from exc
        deadline = self.monotonic() + 5.0
        while self.monotonic() < deadline and self._is_running():
            self.sleep(0.1)
        if self._is_running():
            raise DomesticUatChromeError("browser_close_failed")
        self._write_state("cancelled")
        return self.status()
