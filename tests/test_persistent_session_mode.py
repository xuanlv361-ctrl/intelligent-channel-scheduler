import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.encrypted_session_vault import (
    APPROVED_HOSTS, EncryptedSessionVault, VaultError,
)
from backend.persistent_session_service import (
    PersistentSessionError, PersistentSessionService,
)
from backend.persistent_session_routes import (
    PersistentJob, build_persistent_session_router,
)
from backend.realtime_log_sync_service import (
    RealtimeLogSyncError, RealtimeLogSyncService,
)
from collector.persistent_browser_session import (
    _wait_until_operator_closes, handle_log_api_route,
    is_approved_log_api_read, next_poll_deadline,
    read_public_billing_context, retry_backoff_seconds, retryable_read_error,
)


class FakeDPAPI:
    def protect(self, plaintext: bytes) -> bytes:
        return b"FAKE-DPAPI:" + bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"FAKE-DPAPI:"):
            raise VaultError("persistent_session_decryption_failed")
        return bytes(value ^ 0xA5 for value in ciphertext[11:])


class OtherUserDPAPI(FakeDPAPI):
    def unprotect(self, ciphertext: bytes) -> bytes:
        raise VaultError("persistent_session_decryption_failed")


class Runtime:
    def active(self, environment, key):
        return None


class BillingContextPage:
    def __init__(self, response):
        self.response = response

    def evaluate(self, _script):
        return self.response


def test_public_billing_context_uses_provider_display_settings():
    context = read_public_billing_context(BillingContextPage({
        "status": 200,
        "quota_per_unit": 500000,
        "quota_display_type": "CNY",
        "usd_exchange_rate": 7.3,
    }))
    assert context == {
        "quota_per_unit": 500000,
        "quota_display_type": "CNY",
        "usd_exchange_rate": 7.3,
    }


@pytest.mark.parametrize("response", [
    {"status": 503},
    {"status": 200, "quota_per_unit": 0,
     "quota_display_type": "CNY", "usd_exchange_rate": 7.3},
])
def test_public_billing_context_fails_closed(response):
    with pytest.raises(PersistentSessionError):
        read_public_billing_context(BillingContextPage(response))


def setup(tmp_path, enabled=True, protector=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    execution = tmp_path / "executions.jsonl"
    execution.write_text("", encoding="utf-8")
    realtime = RealtimeLogSyncService(
        tmp_path / "database.sqlite3", execution, Runtime())
    vault = EncryptedSessionVault(
        tmp_path / "outside-repository-vault", protector or FakeDPAPI())
    service = PersistentSessionService(
        realtime, vault, enabled=enabled,
        default_ttl_hours=8, maximum_ttl_hours=24)
    return service, vault


def cookie(domain="uat.weimeta.cn", value="plain-cookie-secret"):
    return {
        "name": "session", "value": value, "domain": domain, "path": "/",
        "secure": True, "httpOnly": True, "sameSite": "Lax",
    }


def paired(tmp_path, protector=None):
    service, vault = setup(tmp_path, protector=protector)
    pairing = service.start_pairing("china_uat", 8, True)
    service.update_pairing(
        pairing["pairing_id"], state="waiting_for_confirmation",
        current_safe_url="https://uat.weimeta.cn/console/billing/logs")
    service.request_confirmation(pairing["pairing_id"], True)
    session = service.complete_pairing(
        pairing["pairing_id"],
        [cookie(), cookie("evil.test", "unrelated-secret")])
    return service, vault, session


def test_persistent_mode_disabled_by_default_and_explicit_opt_in(tmp_path):
    service, _ = setup(tmp_path, enabled=False)
    with pytest.raises(PersistentSessionError, match="not_supported"):
        service.start_pairing("china_uat", 8, True)
    enabled, _ = setup(tmp_path / "enabled", enabled=True)
    with pytest.raises(PersistentSessionError, match="explicit_confirmation"):
        enabled.start_pairing("china_uat", 8, False)


@pytest.mark.parametrize("environment", ["overseas", "production", "all"])
def test_only_china_uat_is_accepted(tmp_path, environment):
    service, _ = setup(tmp_path)
    with pytest.raises(PersistentSessionError, match="environment_mismatch"):
        service.start_pairing(environment, 8, True)


def test_pairing_remains_active_until_confirmation_and_failed_state_saves_nothing(tmp_path):
    service, vault = setup(tmp_path)
    pairing = service.start_pairing("china_uat", 8, True)
    assert pairing["state"] == "browser_starting"
    service.update_pairing(
        pairing["pairing_id"], state="authentication_validation_failed",
        browser_state="visible_manual_login")
    assert not list(vault.directory.glob("*"))
    with pytest.raises(PersistentSessionError, match="invalid_state"):
        service.complete_pairing(pairing["pairing_id"], [cookie()])


def test_successful_pairing_keeps_visible_browser_until_operator_closes():
    class Browser:
        connected = True

        def is_connected(self):
            return self.connected

    class Page:
        def __init__(self, browser):
            self.browser = browser
            self.waits = 0

        def is_closed(self):
            return False

        def wait_for_timeout(self, _milliseconds):
            self.waits += 1
            if self.waits == 4:
                self.browser.connected = False

    browser = Browser()
    page = Page(browser)
    _wait_until_operator_closes(page, browser, poll_ms=1)
    assert page.waits == 4


def test_completed_pairing_keeps_formal_chrome_running(tmp_path):
    service, _vault = setup(tmp_path)
    pairing = service.start_pairing("china_uat", 8, True)
    service.update_pairing(
        pairing["pairing_id"], state="waiting_for_confirmation",
        current_safe_url="https://uat.weimeta.cn/console/billing/logs")
    service.request_confirmation(pairing["pairing_id"], True)
    service.complete_pairing(pairing["pairing_id"], [cookie()])
    completed = service.get_pairing(pairing["pairing_id"])
    assert completed["state"] == "completed"
    assert completed["browser_state"] == "authenticated_chrome_running"


def test_remote_expiry_caps_persisted_session_before_local_ttl(tmp_path):
    service, _vault = setup(tmp_path)
    pairing = service.start_pairing("china_uat", 8, True)
    service.update_pairing(
        pairing["pairing_id"], state="waiting_for_confirmation",
        current_safe_url="https://uat.weimeta.cn/console/billing/logs")
    service.request_confirmation(pairing["pairing_id"], True)
    remote_expiry = datetime.now(timezone.utc) + timedelta(minutes=45)
    session = service.complete_pairing(
        pairing["pairing_id"], [cookie()],
        remote_expires_at=remote_expiry.isoformat())
    actual = datetime.fromisoformat(session["expires_at"])
    assert actual <= remote_expiry
    assert actual > datetime.now(timezone.utc)


def test_expired_remote_session_is_rejected_without_replacing_vault(tmp_path):
    service, vault = setup(tmp_path)
    pairing = service.start_pairing("china_uat", 8, True)
    service.update_pairing(
        pairing["pairing_id"], state="waiting_for_confirmation",
        current_safe_url="https://uat.weimeta.cn/console/billing/logs")
    service.request_confirmation(pairing["pairing_id"], True)
    with pytest.raises(PersistentSessionError, match="remote_session_expired"):
        service.complete_pairing(
            pairing["pairing_id"], [cookie()], remote_expires_at=(
                datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert list(vault.directory.glob("*")) == []


def test_backend_startup_reconciles_pairing_with_dead_worker(tmp_path, monkeypatch):
    service, vault = setup(tmp_path)
    pairing = service.start_pairing("china_uat", 8, True)
    service.update_pairing(
        pairing["pairing_id"], state="waiting_for_confirmation",
        browser_state="visible_manual_login", worker_pid=424242)
    monkeypatch.setattr(
        PersistentSessionService, "_process_is_alive", staticmethod(lambda _pid: False))
    restarted = PersistentSessionService(
        service.realtime, vault, enabled=True,
        default_ttl_hours=8, maximum_ttl_hours=24)
    reconciled = restarted.get_pairing(pairing["pairing_id"])
    assert reconciled["state"] == "stopped"
    assert reconciled["failure_code"] == "persistent_pairing_worker_not_running"


def test_reauthentication_is_independent_and_preserves_old_vault_until_success(tmp_path):
    service, vault, session = paired(tmp_path)
    private = service.get_session(
        session["persistent_session_id"], private=True)
    old_reference = private["vault_reference_id"]
    old_files = sorted(path.name for path in vault.directory.glob("*"))
    with service.connect() as db:
        before_jobs = db.execute(
            "SELECT COUNT(*) FROM realtime_log_sync_jobs").fetchone()[0]
    pairing = service.start_reauthentication("china_uat", 8, True)
    with service.connect() as db:
        after_jobs = db.execute(
            "SELECT COUNT(*) FROM realtime_log_sync_jobs").fetchone()[0]
    assert pairing["state"] == "browser_starting"
    assert before_jobs == after_jobs == 0
    assert vault.load(old_reference, "china_uat")["cookies"]
    assert sorted(path.name for path in vault.directory.glob("*")) == old_files


def test_reauthentication_api_opens_formal_chrome_without_log_read(tmp_path):
    service, _vault = setup(tmp_path)
    actions = []

    class Chrome:
        def open_or_focus(self):
            actions.append("open")
            return {"state": "waiting_for_operator", "running": True,
                    "browser_family": "Google Chrome", "action": "opened_new"}

        def status(self):
            return {"state": "waiting_for_operator", "running": True,
                    "browser_family": "Google Chrome"}

    app = FastAPI()
    app.include_router(build_persistent_session_router(
        service,
        launch_pairing=lambda _pairing_id: (_ for _ in ()).throw(
            AssertionError("legacy pairing worker must not launch")),
        launch_persistent=lambda _job_id: (_ for _ in ()).throw(
            AssertionError("log-sync worker must not launch")),
        stop_worker=lambda _job_id: True,
        secure_request=lambda _request: None,
        secure_read_only_request=lambda _request: "http://127.0.0.1:5174",
        chrome_manager=Chrome(),
    ))
    response = TestClient(app).post(
        "/api/v1/log-sync/persistent/reauthentication/start", json={
            "environment_id": "china_uat", "ttl_hours": 8,
            "explicit_confirmation": True,
        })
    assert response.status_code == 200
    assert response.json()["billing_log_read_issued"] is False
    assert response.json()["synchronization_job_created"] is False
    assert actions == ["open"]
    with service.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM realtime_log_sync_jobs").fetchone()[0] == 0


def test_only_approved_cookies_are_encrypted_and_plaintext_never_written(tmp_path):
    service, vault, session = paired(tmp_path)
    files = list(vault.directory.glob("*"))
    combined = b"".join(path.read_bytes() for path in files)
    assert b"plain-cookie-secret" not in combined
    assert b"unrelated-secret" not in combined
    private = service.get_session(session["persistent_session_id"], private=True)
    payload = vault.load(private["vault_reference_id"], "china_uat")
    assert len(payload["cookies"]) == 1
    assert payload["cookies"][0]["domain"] == "uat.weimeta.cn"
    metadata = json.loads(next(vault.directory.glob("*.metadata.json")).read_text())
    assert "cookies" not in metadata and "plain-cookie-secret" not in json.dumps(metadata)
    assert metadata["cookie_count"] == 1
    assert metadata["encryption"] == "AES-256-GCM"
    assert metadata["key_protection"] == "DPAPI-CurrentUser"


def test_ciphertext_differs_and_sha_is_validated(tmp_path):
    service, vault, session = paired(tmp_path)
    private = service.get_session(session["persistent_session_id"], private=True)
    cipher = next(vault.directory.glob("*.dpapi")).read_bytes()
    assert cipher != b"plain-cookie-secret"
    assert private["ciphertext_sha256"] == __import__("hashlib").sha256(cipher).hexdigest()
    cipher_path = next(vault.directory.glob("*.dpapi"))
    cipher_path.write_bytes(cipher + b"corrupt")
    with pytest.raises(VaultError, match="corrupted"):
        vault.load(private["vault_reference_id"], "china_uat")
    assert not vault.exists(private["vault_reference_id"])


def test_another_windows_user_decryption_failure_is_safe(tmp_path):
    service, vault, session = paired(tmp_path)
    private = service.get_session(session["persistent_session_id"], private=True)
    other = EncryptedSessionVault(vault.directory, OtherUserDPAPI())
    with pytest.raises(VaultError, match="decryption_failed"):
        other.load(private["vault_reference_id"], "china_uat")
    assert not other.exists(private["vault_reference_id"])


def test_expired_session_is_deleted(tmp_path):
    vault = EncryptedSessionVault(tmp_path / "vault", FakeDPAPI())
    now = datetime.now(timezone.utc)
    metadata = vault.save(
        "china_uat", (now - timedelta(hours=2)).isoformat(),
        (now - timedelta(hours=1)).isoformat(), [cookie()])
    with pytest.raises(VaultError, match="expired"):
        vault.load(metadata["vault_reference_id"], "china_uat")
    assert not vault.exists(metadata["vault_reference_id"])


def test_ttl_bounds_environment_and_host_mismatch(tmp_path):
    service, vault = setup(tmp_path)
    for ttl in (0, -1, 25):
        with pytest.raises(PersistentSessionError, match="ttl_invalid"):
            service.start_pairing("china_uat", ttl, True)
    now = datetime.now(timezone.utc)
    metadata = vault.save(
        "china_uat", now.isoformat(), (now + timedelta(hours=1)).isoformat(),
        [cookie()])
    with pytest.raises(VaultError, match="environment_mismatch"):
        vault.load(metadata["vault_reference_id"], "overseas")


def test_session_metadata_contains_no_secret_fields(tmp_path):
    service, _, session = paired(tmp_path)
    rendered = json.dumps(service.status(), ensure_ascii=False)
    for prohibited in (
            "plain-cookie-secret", "ciphertext_sha256",
            "vault_reference_id", "Authorization", "password"):
        assert prohibited not in rendered
    assert service.status()["session"]["storage_types"] == ["cookies"]
    assert session["auto_resume_enabled"] is False


def test_persistent_job_reuses_range_and_requires_confirmation(tmp_path):
    service, _, session = paired(tmp_path)
    body = {
        "environment_id": "china_uat",
        "date_from": "2026-07-29T00:00:00Z",
        "date_to": "2026-07-29T01:00:00Z",
        "timezone": "Asia/Shanghai", "sync_interval_seconds": 7,
        "maximum_records": 10,
        "persistent_session_id": session["persistent_session_id"],
        "explicit_confirmation": False,
    }
    with pytest.raises(PersistentSessionError, match="explicit_confirmation"):
        service.create_job(body)
    body["explicit_confirmation"] = True
    job = service.create_job(body)
    assert job["sync_mode"] == "persistent_encrypted_session"
    assert job["date_from_utc"] == "2026-07-29T00:00:00+00:00"
    assert job["state"] == "created"


def test_status_counts_only_exact_correlations_with_platform_actual_cost(tmp_path):
    service, _, session = paired(tmp_path)
    now = datetime.now(timezone.utc)
    job = service.create_job({
        "environment_id": "china_uat",
        "date_from": (now - timedelta(hours=1)).isoformat(),
        "date_to": now.isoformat(),
        "timezone": "Asia/Shanghai", "sync_interval_seconds": 7,
        "maximum_records": 10,
        "persistent_session_id": session["persistent_session_id"],
        "explicit_confirmation": True,
    })
    with service.realtime.connect() as db:
        db.execute("""INSERT INTO realtime_log_evidence(
          evidence_sha256,sync_job_id,environment_id,sanitized_payload,
          schema_summary,source_url,created_at) VALUES(?,?,?,?,?,?,?)""", (
            "a" * 64, job["sync_job_id"], "china_uat", "{}", "{}",
            "https://api-uat.weimeta.cn/api/log/self", now.isoformat()))
        db.execute("""INSERT INTO realtime_log_records(
          record_id,identity_key,environment_id,platform_log_id,request_id,
          response_id,observed_at,platform_created_at,source_type,sync_job_id,
          evidence_sha256,normalized_json,created_at,updated_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            "record-exact-cost", "request_id:req-1", "china_uat", "log-1",
            "req-1", None, now.isoformat(), now.isoformat(),
            "measured_uat_realtime_browser_sync", job["sync_job_id"],
            "a" * 64, "{}", now.isoformat(), now.isoformat()))
        db.execute("""INSERT INTO realtime_log_correlations(
          correlation_id,execution_id,record_id,environment_id,state,method,
          created_at,details_json) VALUES(?,?,?,?,?,?,?,?)""", (
            "correlation-exact-cost", "execution-1", "record-exact-cost",
            "china_uat", "exact_request_id", "exact_request_id",
            now.isoformat(), '{"actual_cost":0.012}'))
    active = service.status()["active_job"]
    assert active["actual_cost_enriched_count"] == 1
    assert "0.012" not in json.dumps(active)


def test_revocation_stops_jobs_and_removes_ciphertext(tmp_path):
    service, vault, session = paired(tmp_path)
    private = service.get_session(session["persistent_session_id"], private=True)
    stopped = []
    result = service.revoke(
        session["persistent_session_id"],
        "我确认清除本机保存的UAT登录状态。",
        lambda job_id: stopped.append(job_id))
    assert result["local_encrypted_session_removed"] is True
    assert result["platform_session_revoked"] is False
    assert not vault.exists(private["vault_reference_id"])
    assert "cookies" not in json.dumps(result)


def test_auto_resume_default_and_valid_session_only(tmp_path):
    service, _, session = paired(tmp_path)
    assert service.auto_resume_candidates() == []
    with pytest.raises(
            PersistentSessionError,
            match="persistent_auto_resume_not_safely_supported"):
        service.set_auto_resume(
            session["persistent_session_id"], True, True)
    updated = service.set_auto_resume(
        session["persistent_session_id"], False, True)
    assert updated["auto_resume_enabled"] is False
    assert service.auto_resume_candidates() == []


def test_worker_source_enforces_fresh_context_headless_and_no_storage_file():
    source = (Path(__file__).parents[1] / "collector" /
              "persistent_browser_session.py").read_text(encoding="utf-8")
    assert "chromium.launch(headless=True)" in source
    assert "browser.new_context()" in source
    assert "browser.new_context(storage_state=official_state)" in source
    assert "context.add_init_script(" in source
    assert "location.origin" in source
    assert "user_data_dir" not in source
    assert "reauthentication_required" in source


def test_events_contain_no_cookie_material(tmp_path):
    service, _, session = paired(tmp_path)
    events = json.dumps(service.realtime.events(), ensure_ascii=False)
    assert "plain-cookie-secret" not in events
    assert '"cookies"' not in events
    assert session["persistent_session_id"] in events


def test_existing_temporary_mode_remains_available(tmp_path):
    service, _ = setup(tmp_path)
    job = service.realtime.create_job(
        "china_uat", "2026-07-29T00:00:00Z",
        "2026-07-29T01:00:00Z", "Asia/Shanghai", 7, 10)
    assert job["sync_mode"] == "temporary"
    assert job["persistent_session_id"] is None


def persistent_job_body(session_id):
    now = datetime.now(timezone.utc)
    return {
        "environment_id": "china_uat",
        "date_from": (now - timedelta(hours=1)).isoformat(),
        "date_to": now.isoformat(),
        "timezone": "Asia/Shanghai",
        "sync_interval_seconds": 7,
        "maximum_records": 10,
        "persistent_session_id": session_id,
        "explicit_confirmation": True,
    }


def test_poll_deadline_starts_after_completion_without_catch_up():
    assert next_poll_deadline(105.0, 7) == 112.0
    # A 12-second poll begun at t=100 does not schedule against its old start.
    assert next_poll_deadline(112.0, 7) == 119.0


def test_retry_backoff_is_bounded_and_authentication_is_not_retried():
    assert retry_backoff_seconds(1, 0) == 0.75
    assert retry_backoff_seconds(2, 1) == 2.5
    assert retryable_read_error("log_sync_http_503") is True
    assert retryable_read_error("log_sync_http_429") is True
    assert retryable_read_error(
        "persistent_session_reauthentication_required") is False


@pytest.mark.parametrize("url", [
    "https://uat.weimeta.cn/api/log/self",
    "https://admin-uat.weimeta.cn/api/log/self",
    "https://uat.weimeta.cn:443/api/log/self?range=bounded",
])
def test_exact_billing_api_reads_are_intercepted(url):
    assert is_approved_log_api_read(url)


@pytest.mark.parametrize("url", [
    "http://uat.weimeta.cn/api/log/self",
    "https://user:password@uat.weimeta.cn/api/log/self",
    "https://uat.weimeta.cn:444/api/log/self",
    "https://evil.test/api/log/self",
    "https://uat.weimeta.cn/api/log/other",
])
def test_non_exact_or_credential_bearing_reads_are_not_authorized(url):
    assert not is_approved_log_api_read(url)


class _FakeRoute:
    def __init__(self, url):
        self.request = type("Request", (), {"url": url})()
        self.continued = 0
        self.aborted = 0

    def continue_(self):
        self.continued += 1

    def abort(self):
        self.aborted += 1


def test_exact_two_read_route_budget_continues_twice_and_aborts_third():
    decisions = iter([
        {"authorized": True, "http_read_count": 1},
        {"authorized": True, "http_read_count": 2},
        {"authorized": False, "reason": "bounded_limit_reached",
         "http_read_count": 2, "terminalized": True},
    ])
    routes = [
        _FakeRoute("https://uat.weimeta.cn/api/log/self")
        for _ in range(3)]
    for route in routes:
        handle_log_api_route(route, lambda: next(decisions))
    assert [(route.continued, route.aborted) for route in routes] == [
        (1, 0), (1, 0), (0, 1)]


def test_log_api_lookalike_is_aborted_without_consuming_authorization():
    called = 0

    def authorize():
        nonlocal called
        called += 1
        return {"authorized": True}

    route = _FakeRoute("https://evil.test/api/log/self")
    result = handle_log_api_route(route, authorize)
    assert result == {"authorized": False, "reason": "source_not_allowed"}
    assert (route.continued, route.aborted, called) == (0, 1, 0)


def test_persistent_job_request_exposes_independent_bounded_limits():
    body = persistent_job_body("PSESSION-SYNTHETIC")
    body.update({
        "maximum_http_reads": 2,
        "maximum_records_observed": 3,
        "maximum_records_accepted": 2,
        "maximum_elapsed_seconds": 60,
    })
    parsed = PersistentJob(**body)
    assert parsed.maximum_http_reads == 2
    assert parsed.maximum_records_observed == 3
    assert parsed.maximum_records_accepted == 2
    assert parsed.maximum_elapsed_seconds == 60
    with pytest.raises(ValueError):
        PersistentJob(**{**body, "maximum_http_reads": 51})


def test_one_shot_contract_requires_exactly_one_read_and_finalizes_once(tmp_path):
    service, _, session = paired(tmp_path)
    body = persistent_job_body(session["persistent_session_id"])
    body.update({
        "periodic_polling": False,
        "maximum_http_reads": 1,
        "maximum_records_observed": 2,
        "maximum_records_accepted": 2,
        "maximum_elapsed_seconds": 60,
        "page_size": 2,
        "maximum_pages": 1,
    })
    job = service.create_job(body)
    generation = int(job["lease_generation"])
    service.realtime.update_job(
        job["sync_job_id"], state="synchronizing",
        started_at=job["created_at"])
    assert service.authorize_http_read(
        job["sync_job_id"], generation)["authorized"]
    first = service.finalize_one_shot(job["sync_job_id"], generation)
    second = service.finalize_one_shot(job["sync_job_id"], generation)
    assert first["state"] == second["state"] == "stopped"
    assert first["stop_reason"] == "one_shot_completed"
    assert first["http_read_count"] == 1
    assert service.status()["session"]["usage_state"] == "available"
    events = [
        item for item in service.realtime.events("china_uat")
        if item["sync_job_id"] == job["sync_job_id"]
        and item["event_type"] == "persistent_one_shot_finalized"
    ]
    assert len(events) == 1


def test_one_shot_rejects_more_than_one_http_read_before_job_creation(tmp_path):
    service, _, session = paired(tmp_path)
    body = persistent_job_body(session["persistent_session_id"])
    body.update({"periodic_polling": False, "maximum_http_reads": 2})
    with pytest.raises(
            PersistentSessionError,
            match="log_sync_one_shot_requires_one_http_read"):
        service.create_job(body)


def test_service_rejects_accepted_limit_above_observed_limit(tmp_path):
    service, _, session = paired(tmp_path)
    body = persistent_job_body(session["persistent_session_id"])
    body.update({
        "maximum_http_reads": 2,
        "maximum_records_observed": 2,
        "maximum_records_accepted": 3,
        "maximum_elapsed_seconds": 60,
    })
    with pytest.raises(
            PersistentSessionError,
            match="log_sync_maximum_records_accepted_invalid"):
        service.create_job(body)


def test_authoritative_lease_controls_in_use_and_start_stop(tmp_path):
    service, _, session = paired(tmp_path)
    session_id = session["persistent_session_id"]
    assert service.status()["session"]["usage_state"] == "available"
    job = service.create_job(persistent_job_body(session_id))
    status = service.status()
    assert status["session"]["state"] == "in_use"
    assert status["session"]["usage_state"] == "in_use"
    assert status["active_job"]["sync_job_id"] == job["sync_job_id"]
    assert status["session"]["lease"]["lease_owner_job_id"] == job["sync_job_id"]
    stopped = service.realtime.stop(job["sync_job_id"])
    assert stopped["state"] == "stopped"
    status = service.status()
    assert status["session"]["state"] == "active"
    assert status["session"]["usage_state"] == "available"
    assert status["active_job"] is None


@pytest.mark.parametrize("terminal", ["completed", "failed", "blocked"])
def test_terminal_job_releases_lease(tmp_path, terminal):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    service.realtime.update_job(
        job["sync_job_id"], state=terminal,
        stopped_at=datetime.now(timezone.utc).isoformat())
    assert service.status()["active_job"] is None
    assert service.status()["session"]["usage_state"] == "available"
    with service.connect() as db:
        lease = db.execute("""SELECT state,release_reason
          FROM persistent_session_leases
          WHERE lease_owner_job_id=?""", (job["sync_job_id"],)).fetchone()
    assert lease["state"] == "released"


def test_repeated_stop_is_idempotent_and_terminal_job_cannot_resurrect(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    first = service.realtime.stop(job["sync_job_id"])
    second = service.realtime.stop(job["sync_job_id"])
    assert first["state"] == second["state"] == "stopped"
    with service.connect() as db:
        releases = db.execute("""SELECT COUNT(*) FROM realtime_log_sync_events
          WHERE sync_job_id=? AND event_type='persistent_session_lease_released'""",
                              (job["sync_job_id"],)).fetchone()[0]
    assert releases == 1
    with pytest.raises(
            RealtimeLogSyncError, match="invalid_state_transition"):
        service.realtime.update_job(
            job["sync_job_id"], state="decrypting_session")


def test_competing_job_is_rejected_without_orphan_job(tmp_path):
    service, _, session = paired(tmp_path)
    body = persistent_job_body(session["persistent_session_id"])
    first = service.create_job(body)
    with pytest.raises(PersistentSessionError, match="in_use|already_active"):
        service.create_job(body)
    with service.connect() as db:
        assert db.execute("""SELECT COUNT(*) FROM realtime_log_sync_jobs
          WHERE persistent_session_id=?""",
                          (session["persistent_session_id"],)).fetchone()[0] == 1
        assert db.execute("""SELECT COUNT(*) FROM persistent_session_leases
          WHERE persistent_session_id=? AND state='active'""",
                          (session["persistent_session_id"],)).fetchone()[0] == 1
    service.realtime.stop(first["sync_job_id"])


def test_restart_reconciles_expired_lease_and_legacy_stale_in_use(tmp_path):
    service, vault, session = paired(tmp_path)
    session_id = session["persistent_session_id"]
    job = service.create_job(persistent_job_body(session_id))
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with service.connect() as db:
        db.execute("""UPDATE persistent_session_leases SET expires_at=?
          WHERE lease_owner_job_id=?""", (past, job["sync_job_id"]))
        db.execute("""UPDATE persistent_browser_sessions SET state='in_use'
          WHERE persistent_session_id=?""", (session_id,))
    restarted = PersistentSessionService(
        service.realtime, vault, enabled=True,
        default_ttl_hours=8, maximum_ttl_hours=24)
    status = restarted.status()
    assert status["active_job"] is None
    assert status["session"]["usage_state"] == "available"
    assert status["session"]["state"] == "active"
    assert restarted.realtime.get_job(job["sync_job_id"])["state"] == "failed"


def test_stale_generation_cannot_heartbeat_or_release(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    generation = int(job["lease_generation"])
    assert service.heartbeat_lease(job["sync_job_id"], generation) is True
    assert service.heartbeat_lease(job["sync_job_id"], generation + 1) is False
    assert service.release_lease(
        job["sync_job_id"], "stale_worker", generation + 1) is False
    assert service.status()["session"]["usage_state"] == "in_use"
    service.realtime.stop(job["sync_job_id"])


def test_stale_generation_cannot_rotate_vault_or_ingest(tmp_path):
    service, vault, session = paired(tmp_path)
    session_id = session["persistent_session_id"]
    first = service.create_job(persistent_job_body(session_id))
    first_generation = int(first["lease_generation"])
    service.realtime.stop(first["sync_job_id"])
    second = service.create_job(persistent_job_body(session_id))
    before = service.get_session(session_id, private=True)
    with pytest.raises(
            PersistentSessionError, match="persistent_session_lease_lost"):
        service.rotate_authentication_state(
            session_id, [cookie(value="replacement-secret")], [], [],
            job_id=first["sync_job_id"],
            lease_generation=first_generation)
    after_rejected = service.get_session(session_id, private=True)
    assert after_rejected["vault_reference_id"] == before["vault_reference_id"]
    assert after_rejected["ciphertext_sha256"] == before["ciphertext_sha256"]
    service.realtime.update_job(
        second["sync_job_id"], state="synchronizing")
    with pytest.raises(
            RealtimeLogSyncError, match="persistent_session_lease_lost"):
        service.realtime.ingest(
            second["sync_job_id"],
            {"success": True, "message": "synthetic", "data": {
                "items": [], "page": 1, "page_size": 10, "total": 0}},
            "https://uat.weimeta.cn/api/log/self",
            lease_generation=int(second["lease_generation"]) + 1)
    rotated = service.rotate_authentication_state(
        session_id, [cookie(value="replacement-secret")], [], [],
        job_id=second["sync_job_id"],
        lease_generation=int(second["lease_generation"]))
    assert service.get_session(
        session_id, private=True)["vault_reference_id"] != \
        before["vault_reference_id"]
    assert not vault.exists(before["vault_reference_id"])
    assert rotated["usage_state"] == "in_use"
    service.realtime.stop(second["sync_job_id"])


def test_expired_lease_cannot_be_revived_by_generic_job_update(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with service.connect() as db:
        db.execute("""UPDATE persistent_session_leases SET expires_at=?
          WHERE lease_owner_job_id=?""", (expired, job["sync_job_id"]))
    service.realtime.update_job(
        job["sync_job_id"], last_poll_at=datetime.now(timezone.utc).isoformat())
    with service.connect() as db:
        lease = db.execute("""SELECT expires_at FROM persistent_session_leases
          WHERE lease_owner_job_id=?""", (job["sync_job_id"],)).fetchone()
    assert lease["expires_at"] == expired
    service.reconcile_leases("test_expired_update")
    assert service.realtime.get_job(job["sync_job_id"])["state"] == "failed"
    assert service.status()["session"]["usage_state"] == "available"


def test_backend_shutdown_terminalizes_jobs_and_releases_leases(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    assert service.shutdown_active_jobs() == 1
    assert service.realtime.get_job(job["sync_job_id"])["state"] == "failed"
    assert service.status()["active_job"] is None
    assert service.status()["session"]["usage_state"] == "available"
    assert service.shutdown_active_jobs() == 0


def test_revoke_deletes_vault_reference_committed_by_concurrent_rotation(
        tmp_path, monkeypatch):
    service, vault, session = paired(tmp_path)
    session_id = session["persistent_session_id"]
    job = service.create_job(persistent_job_body(session_id))
    generation = int(job["lease_generation"])
    original_stop = service.realtime.stop
    rotated = []

    def rotate_then_stop(job_id):
        rotated.append(service.rotate_authentication_state(
            session_id, [cookie(value="replacement-secret")], [], [],
            job_id=job_id, lease_generation=generation))
        return original_stop(job_id)

    monkeypatch.setattr(service.realtime, "stop", rotate_then_stop)
    result = service.revoke(
        session_id, "我确认清除本机保存的UAT登录状态。",
        lambda _job_id: True)
    assert rotated
    assert result["local_encrypted_session_removed"] is True
    assert not list(vault.directory.glob("*.dpapi"))
    assert not list(vault.directory.glob("*.metadata.json"))


def test_backend_shutdown_does_not_close_active_authentication_browser(tmp_path):
    service, _ = setup(tmp_path)
    pairing = service.start_pairing("china_uat", 8, True)
    assert pairing["state"] == "browser_starting"
    assert service.shutdown_active_jobs() == 0
    preserved = service.get_pairing(pairing["pairing_id"])
    assert preserved["state"] == "browser_starting"
    assert preserved["browser_state"] == "launch_requested"
    with pytest.raises(
            PersistentSessionError, match="persistent_session_already_active"):
        service.start_pairing("china_uat", 8, True)


def test_stop_exposes_stopping_before_worker_converges(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    observed = []

    def stop_worker(job_id):
        status = service.status()
        observed.append((
            service.realtime.get_job(job_id)["state"],
            status["active_job"]["state"],
            status["session"]["usage_state"]))
        return True

    stopped = service.stop_job(job["sync_job_id"], stop_worker)
    assert observed == [("stopping", "stopping", "in_use")]
    assert stopped["state"] == "stopped"
    assert service.status()["active_job"] is None
    assert service.status()["session"]["usage_state"] == "available"


def test_stop_failure_keeps_stopping_lease_for_reaper_retry(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    with pytest.raises(
            PersistentSessionError, match="persistent_worker_stop_failed"):
        service.stop_job(job["sync_job_id"], lambda _job_id: False)
    assert service.realtime.get_job(job["sync_job_id"])["state"] == "stopping"
    assert service.status()["active_job"]["state"] == "stopping"
    assert service.status()["session"]["usage_state"] == "in_use"
    service.realtime.stop(job["sync_job_id"])


def test_competing_job_does_not_decrypt_or_touch_session_before_lease(
        tmp_path, monkeypatch):
    service, vault, session = paired(tmp_path)
    body = persistent_job_body(session["persistent_session_id"])
    first = service.create_job(body)
    private_before = service.get_session(
        session["persistent_session_id"], private=True)
    decrypted = []
    original_load = vault.load

    def tracked_load(*args, **kwargs):
        decrypted.append(True)
        return original_load(*args, **kwargs)

    monkeypatch.setattr(vault, "load", tracked_load)
    with pytest.raises(PersistentSessionError, match="in_use|already_active"):
        service.create_job(body)
    private_after = service.get_session(
        session["persistent_session_id"], private=True)
    assert not decrypted
    assert private_after["revision"] == private_before["revision"]
    assert private_after["last_used_at"] == private_before["last_used_at"]
    service.realtime.stop(first["sync_job_id"])


def test_generic_active_job_update_cannot_renew_worker_lease(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    with service.connect() as db:
        before = dict(db.execute("""SELECT heartbeat_at,expires_at,version
          FROM persistent_session_leases WHERE lease_owner_job_id=?""",
                                 (job["sync_job_id"],)).fetchone())
    service.realtime.update_job(
        job["sync_job_id"], last_poll_at=datetime.now(timezone.utc).isoformat())
    with service.connect() as db:
        after = dict(db.execute("""SELECT heartbeat_at,expires_at,version
          FROM persistent_session_leases WHERE lease_owner_job_id=?""",
                                (job["sync_job_id"],)).fetchone())
    assert after == before
    service.realtime.stop(job["sync_job_id"])


def test_authentication_invalidation_disables_session_and_releases_job(
        tmp_path):
    service, vault, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    service.invalidate_reauthentication(session["persistent_session_id"])
    private = service.get_session(
        session["persistent_session_id"], private=True)
    assert private["state"] == "disabled"
    assert private["authentication_status"] == "invalid"
    assert not vault.exists(private["vault_reference_id"])
    assert service.realtime.get_job(job["sync_job_id"])["state"] == "failed"


def test_running_missing_lease_fails_but_stopping_heartbeat_is_harmless(
        tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    generation = int(job["lease_generation"])
    assert service.release_lease(
        job["sync_job_id"], "synthetic_loss", generation)
    assert service.heartbeat_lease_outcome(
        job["sync_job_id"], generation) == "lease_lost"
    failed = service.realtime.get_job(job["sync_job_id"])
    assert failed["state"] == "failed"
    assert failed["error_code"] == "persistent_session_lease_lost"

    next_job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    next_generation = int(next_job["lease_generation"])
    service.realtime.update_job(
        next_job["sync_job_id"], state="stopping",
        stop_reason="operator_stop")
    assert service.release_lease(
        next_job["sync_job_id"], "operator_stop", next_generation)
    assert service.heartbeat_lease_outcome(
        next_job["sync_job_id"], next_generation) == "stopping"
    stopped = service.finalize_stop(
        next_job["sync_job_id"], next_generation)
    assert stopped["state"] == "stopped"
    assert stopped["stop_reason"] == "operator_stop"


def test_queued_heartbeat_after_stop_cannot_overwrite_terminal_state(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    generation = int(job["lease_generation"])
    service.stop_job(job["sync_job_id"], lambda _job_id: True)
    assert service.heartbeat_lease_outcome(
        job["sync_job_id"], generation) == "terminal"
    assert service.realtime.get_job(job["sync_job_id"])["state"] == "stopped"
    service.realtime.update_job(
        job["sync_job_id"], state="failed",
        error_code="persistent_session_lease_lost")
    assert service.realtime.get_job(job["sync_job_id"])["state"] == "stopped"


def test_stale_generation_is_audited_without_mutation(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    generation = int(job["lease_generation"])
    assert service.heartbeat_lease_outcome(
        job["sync_job_id"], generation + 1) == "stale_generation"
    assert service.realtime.get_job(job["sync_job_id"])["state"] == "created"
    events = service.realtime.events("china_uat")
    assert any(
        item["event_type"] ==
        "persistent_session_stale_heartbeat_ignored"
        for item in events)
    service.realtime.stop(job["sync_job_id"])


def test_wrong_owner_heartbeat_is_rejected_without_corrupting_current_job(
        tmp_path):
    service, _, session = paired(tmp_path)
    first = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    first_generation = int(first["lease_generation"])
    service.realtime.stop(first["sync_job_id"])
    second = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    second_generation = int(second["lease_generation"])
    service.realtime.stop(second["sync_job_id"])
    future = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    with service.connect() as db:
        db.execute("""UPDATE realtime_log_sync_jobs SET state='created'
          WHERE sync_job_id=?""", (first["sync_job_id"],))
        db.execute("""UPDATE persistent_session_leases SET
          state='active',released_at=NULL,release_reason=NULL,expires_at=?
          WHERE lease_owner_job_id=? AND generation=?""", (
            future, second["sync_job_id"], second_generation))
        db.execute("""UPDATE persistent_browser_sessions SET state='in_use'
          WHERE persistent_session_id=?""",
                   (session["persistent_session_id"],))
    assert service.heartbeat_lease_outcome(
        first["sync_job_id"], first_generation) == "wrong_owner"
    assert service.realtime.get_job(first["sync_job_id"])["state"] == "created"
    with service.connect() as db:
        owner = db.execute("""SELECT lease_owner_job_id
          FROM persistent_session_leases
          WHERE persistent_session_id=? AND state='active'""",
                           (session["persistent_session_id"],)).fetchone()[0]
    assert owner == second["sync_job_id"]


def test_restart_while_stopping_with_released_lease_converges_to_stopped(
        tmp_path):
    service, vault, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    generation = int(job["lease_generation"])
    service.realtime.update_job(
        job["sync_job_id"], state="stopping",
        stop_reason="operator_stop")
    service.release_lease(job["sync_job_id"], "operator_stop", generation)
    restarted = PersistentSessionService(
        service.realtime, vault, enabled=True,
        default_ttl_hours=8, maximum_ttl_hours=24)
    assert restarted.realtime.get_job(job["sync_job_id"])["state"] == "stopped"
    assert restarted.status()["session"]["usage_state"] == "available"


def test_stop_terminal_state_is_written_and_audited_once(tmp_path):
    service, _, session = paired(tmp_path)
    job = service.create_job(
        persistent_job_body(session["persistent_session_id"]))
    generation = int(job["lease_generation"])
    service.realtime.update_job(
        job["sync_job_id"], state="stopping",
        stop_reason="operator_stop")
    service.finalize_stop(job["sync_job_id"], generation)
    service.finalize_stop(job["sync_job_id"], generation)
    events = service.realtime.events("china_uat")
    finalized = [
        item for item in events
        if item["sync_job_id"] == job["sync_job_id"] and
        item["event_type"] == "persistent_session_stop_finalized"
    ]
    assert len(finalized) == 1
    assert service.status()["session"]["usage_state"] == "available"


def _bounded_job(service, session_id, **overrides):
    body = persistent_job_body(session_id)
    body.update({
        "maximum_http_reads": 2,
        "maximum_records_observed": 2,
        "maximum_records_accepted": 2,
        "maximum_elapsed_seconds": 60,
    })
    body.update(overrides)
    job = service.create_job(body)
    service.realtime.update_job(
        job["sync_job_id"], state="synchronizing",
        started_at=job["created_at"])
    return service.realtime.get_job(job["sync_job_id"], private=True)


def test_two_empty_http_reads_reach_bound_without_third_authorization(tmp_path):
    service, _, session = paired(tmp_path)
    job = _bounded_job(service, session["persistent_session_id"])
    generation = int(job["lease_generation"])
    first = service.authorize_http_read(job["sync_job_id"], generation)
    second = service.authorize_http_read(job["sync_job_id"], generation)
    assert first["authorized"] and second["authorized"]
    bounded = service.check_bounded_limits(
        job["sync_job_id"], generation)
    assert bounded["bounded"]
    assert bounded["limit_type"] == "maximum_http_reads"
    third = service.authorize_http_read(job["sync_job_id"], generation)
    assert third["authorized"] is False
    final = service.realtime.get_job(job["sync_job_id"])
    assert final["http_read_count"] == 2
    assert final["collected_count"] == 0
    assert final["inserted_count"] == 0
    assert final["state"] == "stopped"
    assert final["stop_reason"] == "bounded_limit_reached"


def test_record_and_read_limits_are_independent(tmp_path):
    service, _, session = paired(tmp_path)
    job = _bounded_job(
        service, session["persistent_session_id"],
        maximum_http_reads=5, maximum_records_observed=2,
        maximum_records_accepted=1)
    generation = int(job["lease_generation"])
    assert service.authorize_http_read(
        job["sync_job_id"], generation)["authorized"]
    service.realtime.update_job(
        job["sync_job_id"], collected_count=2, inserted_count=0)
    bounded = service.check_bounded_limits(
        job["sync_job_id"], generation)
    assert bounded["limit_type"] == "maximum_records_observed"
    final = service.realtime.get_job(job["sync_job_id"])
    assert final["http_read_count"] == 1
    assert final["inserted_count"] == 0


def test_accepted_record_limit_stops_independently(tmp_path):
    service, _, session = paired(tmp_path)
    job = _bounded_job(
        service, session["persistent_session_id"],
        maximum_http_reads=5, maximum_records_observed=5,
        maximum_records_accepted=2)
    generation = int(job["lease_generation"])
    service.realtime.update_job(
        job["sync_job_id"], collected_count=4, inserted_count=2)
    bounded = service.check_bounded_limits(
        job["sync_job_id"], generation)
    assert bounded["limit_type"] == "maximum_records_accepted"


def test_injectable_clock_enforces_elapsed_limit(tmp_path):
    service, _, session = paired(tmp_path)
    job = _bounded_job(
        service, session["persistent_session_id"],
        maximum_http_reads=5, maximum_records_observed=5,
        maximum_records_accepted=5, maximum_elapsed_seconds=60)
    started = datetime.fromisoformat(job["started_at"])
    service._clock = lambda: started + timedelta(seconds=60)
    bounded = service.check_bounded_limits(
        job["sync_job_id"], int(job["lease_generation"]))
    assert bounded["bounded"]
    assert bounded["limit_type"] == "maximum_elapsed_seconds"
