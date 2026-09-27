from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.domestic_uat_chrome_manager import (
    DomesticUatChromeError, DomesticUatChromeManager, LOGIN_URL,
)


class FakeProcess:
    def __init__(self, pid: int = 43120):
        self.pid = pid

    def poll(self):
        return None


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeProcess()


class EmptyActionResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return b""


class EmptyActionOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return EmptyActionResponse()


def manager(tmp_path: Path, *, executable: bool = True):
    chrome = tmp_path / "Chrome" / "chrome.exe"
    if executable:
        chrome.parent.mkdir(parents=True, exist_ok=True)
        chrome.write_bytes(b"fake executable")
    popen = FakePopen()
    value = DomesticUatChromeManager(
        local_app_data=tmp_path / "LocalAppData", popen=popen,
        chrome_candidates=[chrome], sleep=lambda _seconds: None,
    )
    return value, popen, chrome


def test_discovers_reviewed_google_chrome_path(tmp_path):
    value, _, chrome = manager(tmp_path)
    assert value.chrome_executable() == chrome.resolve()


def test_missing_chrome_fails_without_chromium_fallback(tmp_path):
    value, _, _ = manager(tmp_path, executable=False)
    assert value.status()["state"] == "chrome_not_installed"
    with pytest.raises(DomesticUatChromeError, match="chrome_not_installed"):
        value.open_or_focus()


def test_launch_uses_dedicated_persistent_profile_and_returns_while_running(
        tmp_path, monkeypatch):
    value, popen, _ = manager(tmp_path)
    running = iter([False, True, True])
    monkeypatch.setattr(value, "_is_running", lambda: next(running))
    result = value.open_or_focus()
    assert result["state"] == "waiting_for_operator"
    assert result["running"] is True
    assert result["action"] == "opened_new"
    assert len(popen.calls) == 1
    args, kwargs = popen.calls[0]
    assert f"--user-data-dir={value.profile_directory}" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert "--remote-debugging-port=0" in args
    assert "--remote-allow-origins=http://127.0.0.1" in args
    assert "--remote-allow-origins=*" not in args
    assert LOGIN_URL in args
    assert kwargs["shell"] is False
    assert "--incognito" not in args
    assert "Chromium" not in " ".join(args)


def test_repeated_open_focuses_single_existing_instance(tmp_path, monkeypatch):
    value, popen, _ = manager(tmp_path)
    monkeypatch.setattr(value, "_is_running", lambda: True)
    focused = []
    monkeypatch.setattr(value, "_focus_existing", lambda: focused.append(True))
    result = value.open_or_focus()
    assert result["action"] == "focused_existing"
    assert focused == [True]
    assert popen.calls == []


def test_focus_accepts_chrome_activate_success_with_empty_body(
        tmp_path, monkeypatch):
    value, _, _ = manager(tmp_path)
    monkeypatch.setattr(value, "_devtools_port", lambda: 43121)
    monkeypatch.setattr(value, "_request_json", lambda path: [{
        "id": "target-1", "url": LOGIN_URL,
    }] if path == "/json/list" else {})
    opener = EmptyActionOpener()
    value._opener = opener

    value._focus_existing()

    assert len(opener.requests) == 1
    request, timeout = opener.requests[0]
    assert request.full_url == (
        "http://127.0.0.1:43121/json/activate/target-1")
    assert timeout == 1.5


def test_running_status_remains_stable_beyond_fifteen_minutes(
        tmp_path, monkeypatch):
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    value, _, _ = manager(tmp_path)
    value.clock = lambda: now
    value._write_state("waiting_for_operator", launcher_pid=43120)
    monkeypatch.setattr(value, "_is_running", lambda: True)
    now += timedelta(minutes=16)
    assert value.status()["state"] == "waiting_for_operator"
    assert value.status()["running"] is True


def test_manual_close_converges_to_browser_closed_and_profile_is_reused(
        tmp_path, monkeypatch):
    value, _, _ = manager(tmp_path)
    value._write_state("waiting_for_operator", launcher_pid=43120)
    monkeypatch.setattr(value, "_is_running", lambda: False)
    assert value.status()["state"] == "browser_closed"
    restarted, _, _ = manager(tmp_path)
    assert restarted.profile_directory == value.profile_directory
    assert restarted._read_state()["state"] == "browser_closed"


def test_backend_restart_rediscovers_running_chrome_without_process_ownership(
        tmp_path, monkeypatch):
    first, _, _ = manager(tmp_path)
    first._write_state("waiting_for_operator", launcher_pid=43120)
    restarted, _, _ = manager(tmp_path)
    monkeypatch.setattr(restarted, "_is_running", lambda: True)
    status = restarted.status()
    assert status["state"] == "waiting_for_operator"
    assert status["running"] is True


def test_public_status_never_exposes_profile_pid_or_debug_endpoint(
        tmp_path, monkeypatch):
    value, _, _ = manager(tmp_path)
    value._write_state("waiting_for_operator", launcher_pid=43120)
    monkeypatch.setattr(value, "_is_running", lambda: True)
    serialized = json.dumps(value.status())
    for forbidden in ("43120", str(value.profile_directory),
                      "DevToolsActivePort", "webSocketDebuggerUrl"):
        assert forbidden not in serialized


def test_loopback_cdp_validation_rejects_nonlocal_endpoint(tmp_path, monkeypatch):
    value, _, _ = manager(tmp_path)
    monkeypatch.setattr(value, "_devtools_port", lambda: 9222)
    monkeypatch.setattr(value, "_request_json", lambda _path: {
        "webSocketDebuggerUrl": "ws://192.0.2.10:9222/devtools/browser/id"})
    with pytest.raises(DomesticUatChromeError,
                       match="browser_debug_endpoint_invalid"):
        value._cdp_endpoint()


def test_worker_target_is_created_before_playwright_attach(tmp_path, monkeypatch):
    value, _, _ = manager(tmp_path)
    calls = []

    def create(path, method="GET"):
        calls.append((path, method))
        return {
            "id": "worker-target-1",
            "url": "about:blank#ics-worker-lsync-123",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/secret",
        }

    monkeypatch.setattr(value, "_request_json", create)
    result = value.create_worker_target("lsync-123")
    assert result == {
        "target_id": "worker-target-1",
        "target_url": "about:blank#ics-worker-lsync-123",
    }
    assert calls == [
        ("/json/new?about%3Ablank%23ics-worker-lsync-123", "PUT")]
    assert "webSocketDebuggerUrl" not in json.dumps(result)


def test_stale_worker_cleanup_never_closes_operator_tabs(tmp_path, monkeypatch):
    value, _, _ = manager(tmp_path)
    monkeypatch.setattr(value, "_request_json", lambda path: [
        {"id": "worker-1", "url": "about:blank#ics-worker-lsync-old"},
        {"id": "operator-1", "url": LOGIN_URL},
    ] if path == "/json/list" else {})
    closed = []
    monkeypatch.setattr(value, "close_worker_target", closed.append)
    assert value.cleanup_worker_targets() == 1
    assert closed == ["worker-1"]


@pytest.mark.parametrize("marker", ["", "../escape", "UPPER", "has space"])
def test_worker_target_marker_is_strictly_validated(tmp_path, marker):
    value, _, _ = manager(tmp_path)
    with pytest.raises(DomesticUatChromeError,
                       match="browser_worker_marker_invalid"):
        value.create_worker_target(marker)


def test_jwt_expiry_is_parsed_without_returning_token(tmp_path):
    value, _, _ = manager(tmp_path)
    expiry = 2_000_000_000
    encoded = base64.urlsafe_b64encode(
        json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    token = f"header.{encoded}.signature"
    assert value._jwt_expiry(token, expiry - 60) == expiry
    assert value._jwt_expiry("not-a-token", expiry - 60) is None


def test_authentication_inspection_reuses_connection_without_closing_chrome(
        tmp_path, monkeypatch):
    import collector.persistent_browser_session as capture_helpers

    class FakePage:
        url = LOGIN_URL

    class FakeContext:
        pages = [FakePage()]

    class FakeBrowser:
        contexts = [FakeContext()]

        @staticmethod
        def is_connected():
            return True

    connections = []
    value, _, _ = manager(tmp_path)
    value._cdp_connector = lambda endpoint: (
        connections.append(endpoint) or FakeBrowser())
    monkeypatch.setattr(value, "_is_running", lambda: True)
    monkeypatch.setattr(value, "_cdp_endpoint", lambda: "http://127.0.0.1:43121")
    monkeypatch.setattr(
        capture_helpers, "_storage_names",
        lambda _page: {"local": [], "session": [], "indexed": []})
    monkeypatch.setattr(
        capture_helpers, "_capture_authentication_state",
        lambda *_args, **_kwargs: ([{
            "name": "session", "value": "opaque-test-value",
            "expires": 2_000_000_000,
        }], [], []))
    monkeypatch.setattr(
        capture_helpers, "_diagnostic",
        lambda *_args, **_kwargs: {"authentication_origins": [
            "https://uat.weimeta.cn"]})

    first = value.capture_authenticated_session()
    second = value.capture_authenticated_session()

    assert first["remote_expires_at"] == second["remote_expires_at"]
    assert connections == ["http://127.0.0.1:43121"]
    assert value.status()["running"] is True


def test_explicit_close_requires_operator_confirmation(tmp_path, monkeypatch):
    value, _, _ = manager(tmp_path)
    monkeypatch.setattr(value, "_is_running", lambda: True)
    with pytest.raises(DomesticUatChromeError,
                       match="browser_close_confirmation_required"):
        value.explicit_close(False)
