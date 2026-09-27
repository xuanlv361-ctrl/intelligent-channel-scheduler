import hashlib
import json

import pytest

from backend.environment_runtime_settings import (
    DOMESTIC_UAT_LOG_PAGE_URL, EnvironmentRuntimeSettings,
    canonical_error_code, normalize_domestic_uat_log_url,
    normalize_overseas_log_url,
)
from backend.browser_import_service import BrowserImportService
from backend.uat_service import UatStore
from src.services.console_service import Store


@pytest.mark.parametrize("url", [
    "http://weimeta.ai/logs",
    "https://evil.weimeta.ai/logs",
    "https://weimeta.ai.attacker.com/logs",
    "https://user:password@weimeta.ai/logs",
    "https://weimeta.ai/logs#token",
    "https://weimeta.ai/logs?api_key=secret",
    "https://weimeta.ai/logs?session=secret",
    "javascript:alert(1)",
    "file:///tmp/logs",
    "data:text/plain,logs",
])
def test_unsafe_overseas_log_urls_are_rejected(url):
    with pytest.raises(ValueError, match="overseas_log_page_invalid"):
        normalize_overseas_log_url(url)


def test_uat_execution_switch_is_versioned_scoped_and_persistent(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    UatStore(database)
    service = EnvironmentRuntimeSettings(database)
    assert service.execution_enabled("china_uat") is None

    enabled = service.set_execution_enabled(
        "china_uat", True, "local-operator")
    assert enabled["enabled"] is True
    assert enabled["setting_version"] == "environment_execution_state_v1"
    assert service.execution_enabled("china_uat") is True
    assert service.execution_state("china_uat") == enabled

    restarted = EnvironmentRuntimeSettings(database)
    assert restarted.execution_enabled("china_uat") is True
    disabled = restarted.set_execution_enabled(
        "china_uat", False, "local-operator")
    assert disabled["enabled"] is False
    assert restarted.execution_enabled("china_uat") is False
    assert restarted.execution_state("china_uat") == disabled

    with restarted.connect() as db:
        active = db.execute("""SELECT setting_value FROM environment_runtime_settings
          WHERE environment_id='china_uat' AND setting_name='real_execution_enabled'
          AND active=1""").fetchall()
        audits = db.execute("""SELECT details FROM audit_events
          WHERE event_type='environment_execution_state_changed'""").fetchall()
    assert [row[0] for row in active] == ["false"]
    assert len(audits) == 2
    assert all("api_key" not in row[0].casefold() for row in audits)


def test_uat_execution_switch_rejects_invalid_values_and_other_environments(tmp_path):
    database = tmp_path / "runtime.sqlite3"
    UatStore(database)
    service = EnvironmentRuntimeSettings(database)
    with pytest.raises(ValueError, match="environment_execution_enabled_boolean_required"):
        service.set_execution_enabled("china_uat", 1, "local-operator")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="environment_execution_toggle_not_supported"):
        service.set_execution_enabled("overseas", True, "local-operator")


@pytest.mark.parametrize("url", [
    "http://uat.weimeta.cn/console/billing/logs",
    "https://evil.uat.weimeta.cn/console/billing/logs",
    "https://uat.weimeta.cn.evil.test/console/billing/logs",
    "https://uat.weimeta.cn/console/billing/logs?token=secret",
    "https://uat.weimeta.cn/console/billing/logs/extra",
    "file:///console/billing/logs",
])
def test_domestic_uat_log_page_accepts_only_exact_reviewed_url(url):
    with pytest.raises(ValueError, match="domestic_uat_log_page_invalid"):
        normalize_domestic_uat_log_url(url)
    assert normalize_domestic_uat_log_url(
        DOMESTIC_UAT_LOG_PAGE_URL) == DOMESTIC_UAT_LOG_PAGE_URL


def _runtime(tmp_path, ttl=600):
    path = tmp_path / "runtime.sqlite3"
    UatStore(path)
    return EnvironmentRuntimeSettings(path, ttl), path


def _confirm(runtime, url="https://weimeta.ai/console/usage-logs?page=1"):
    preview = runtime.preview_log_page(url)
    return preview, runtime.confirm_log_page(
        preview["validation_id"], preview["value_sha256"], True, "test_operator")


def test_preview_is_structural_only_and_does_not_persist(tmp_path):
    runtime, _ = _runtime(tmp_path)
    preview = runtime.preview_log_page("https://WEIMETA.AI/console/usage-logs?page=1")
    assert preview["normalized_url"] == "https://weimeta.ai/console/usage-logs?page=1"
    assert preview["validation_id"].startswith("LOGURL-")
    assert preview["live_validation_status"] == "not_attempted"
    assert runtime.active("overseas", "logs_page_url") is None


def test_confirmation_requires_exact_evidence_and_not_expired(tmp_path):
    runtime, _ = _runtime(tmp_path)
    preview = runtime.preview_log_page("https://weimeta.ai/operator-observed")
    with pytest.raises(ValueError, match="explicit_confirmation_required"):
        runtime.confirm_log_page(preview["validation_id"], preview["value_sha256"], False, "x")
    preview = runtime.preview_log_page("https://weimeta.ai/operator-observed")
    with pytest.raises(ValueError, match="sha256_mismatch"):
        runtime.confirm_log_page(preview["validation_id"], "0" * 64, True, "x")
    expired, _ = _runtime(tmp_path / "expired", ttl=-1)
    preview = expired.preview_log_page("https://weimeta.ai/operator-observed")
    with pytest.raises(ValueError, match="preview_expired"):
        expired.confirm_log_page(preview["validation_id"], preview["value_sha256"], True, "x")


def test_confirmed_values_are_persistent_and_amendments_remain_auditable(tmp_path):
    runtime, path = _runtime(tmp_path)
    _, first = _confirm(runtime, "https://weimeta.ai/first-observed")
    _, second = _confirm(runtime, "https://weimeta.ai/second-observed")
    assert first["log_page_status"] == second["log_page_status"] == "operator_confirmed"
    assert second["logs_page_url"].endswith("/second-observed")
    with runtime.connect() as db:
        rows = db.execute("""SELECT active,setting_value,value_sha256
          FROM environment_runtime_settings ORDER BY created_at""").fetchall()
        audits = db.execute("""SELECT details FROM audit_events
          WHERE event_type='environment_runtime_setting_confirmed'""").fetchall()
    assert len(rows) == 2 and [row["active"] for row in rows] == [0, 1]
    assert len(audits) == 2 and json.loads(audits[-1]["details"])["amendment"] is True
    assert "credential" not in path.read_bytes().decode(errors="ignore").casefold()


def test_domestic_uat_confirmation_is_versioned_and_persistent(tmp_path):
    runtime, _ = _runtime(tmp_path)
    assert runtime.status("china_uat")["blocking_reason"] == \
        "log_page_not_confirmed"
    preview = runtime.preview_environment_log_page(
        "china_uat", DOMESTIC_UAT_LOG_PAGE_URL)
    assert preview["allowed_origin"] == "https://uat.weimeta.cn"
    assert preview["path"] == "/console/billing/logs"
    assert runtime.active("china_uat", "logs_page_url") is None
    confirmed = runtime.confirm_environment_log_page(
        "china_uat", preview["validation_id"], preview["value_sha256"],
        True, "test_operator")
    assert confirmed["logs_page_url"] == DOMESTIC_UAT_LOG_PAGE_URL
    assert confirmed["setting_version"] == \
        "environment_log_page_confirmation_v1"
    assert confirmed["allowed_origin"] == "https://uat.weimeta.cn"
    assert confirmed["log_page_path"] == "/console/billing/logs"


def test_source_registry_is_not_rewritten(tmp_path):
    source = "config/platform_environments_v1.json"
    before = hashlib.sha256(open(source, "rb").read()).digest()
    runtime, _ = _runtime(tmp_path)
    _confirm(runtime)
    assert before == hashlib.sha256(open(source, "rb").read()).digest()


def test_collector_requires_and_cannot_override_confirmed_url(tmp_path):
    runtime, path = _runtime(tmp_path)
    store = Store(path)
    service = BrowserImportService(path, store, lambda: runtime)
    with pytest.raises(ValueError, match="overseas_log_page_unconfirmed"):
        service.preview({"environment_id": "overseas", "records": []})
    _confirm(runtime, "https://weimeta.ai/operator-observed")
    rejected = service.preview({"environment_id": "overseas", "records": [{
        "source_url": "https://weimeta.ai/another-page", "request_id": "r1"}]})
    assert rejected["record_count"] == 0 and rejected["rejected_count"] == 1
    accepted = service.preview({"environment_id": "overseas",
        "structure_recognized": True, "records": []})
    assert accepted["source_type"] == "measured_overseas_browser_collector"
    assert runtime.status("overseas")["log_page_status"] == "browser_live_validated"
    assert runtime.status("overseas")["latest_record_count"] == 0


def test_unknown_structure_does_not_become_live_validated(tmp_path):
    runtime, path = _runtime(tmp_path)
    _confirm(runtime, "https://weimeta.ai/operator-observed")
    service = BrowserImportService(path, Store(path), lambda: runtime)
    service.preview({"environment_id": "overseas", "structure_recognized": False, "records": []})
    assert runtime.status("overseas")["log_page_status"] == "operator_confirmed"


def test_legacy_error_codes_map_without_rewriting_historical_values():
    historical = {"error": "overseas_endpoint_unconfirmed"}
    encoded = json.dumps(historical)
    assert canonical_error_code(historical["error"]) == "environment_configuration_incomplete"
    assert canonical_error_code("overseas_execution_not_authorized") == "overseas_completion_not_yet_authorized"
    assert json.dumps(historical) == encoded
