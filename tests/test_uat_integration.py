from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import sqlite3
from pathlib import Path

import pytest
import backend.app as application
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from tests.enterprise_test_client import authenticated_test_client
app = application.app
from backend.uat_service import (
    ERROR_CONTRACT, UatSettings, UatStore, classify_response, correlate,
    execute, reparse_stored_response, runtime_error, status, validate,
)

CONFIRMATION = "I understand this will call the Weimeta UAT API and may incur UAT cost."


def settings(**changes):
    values = dict(
        environment="uat", base_url="https://api-uat.weimeta.cn", api_key="test-key-not-real",
        enabled=True, allowed_hosts=("api-uat.weimeta.cn",), timeout_seconds=2,
        max_tokens=2048, daily_request_limit=None, daily_budget_cny=None, max_request_cost_cny=.2,
    )
    values.update(changes)
    return UatSettings(**values)


def body(**request_changes):
    request = {"requested_model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "用一句话说明水循环。"}], "stream": False, "max_tokens": 128}
    request.update(request_changes)
    return {
        "mode": "real_uat_execute",
        "confirmation": {"confirmed": True, "confirmation_text": CONFIRMATION},
        "request": request,
        "measurement": {"plan_id": "UR-V3-001", "request_profile_id": "P01", "session_id": "SESSION-D1"},
        "shadow": {"run_before_execution": True, "strategy": "confidence_aware_v2"},
    }


def shadow(_):
    return {"catalog_version": "real-v1", "catalog_sha256": "a" * 64, "recommended_candidate": "REAL-CHANNEL-19", "complete_ranking": [], "exclusion_reasons": [], "fallback_order": []}


def confirmed_model_catalog(max_output_tokens: int = 2048):
    """Reviewed capability fixture; production intentionally rejects boolean-only entries."""
    return {"deepseek-v4-flash": {
        "confirmed_max_output_tokens": max_output_tokens,
        "confirmed_channel_max_output_tokens": max_output_tokens,
        "max_context_tokens": max_output_tokens * 4,
        "max_input_tokens": max_output_tokens * 3,
        "evidence_source": "reviewed_test_contract",
        "evidence_version": "test-v1",
        "fresh_until": "2099-01-01T00:00:00+00:00",
    }}


def test_status_missing_key_and_disabled_never_exposes_secret(tmp_path):
    store = UatStore(tmp_path / "db.sqlite3")
    result = status(settings(api_key="", enabled=False), store)
    assert result["blocking_reasons"] == ["uat_key_not_configured", "real_execution_disabled"]
    assert "api_key" not in result and "test-key" not in json.dumps(result)


@pytest.mark.parametrize(("changes", "error"), [
    ({"base_url": "http://api-uat.weimeta.cn"}, "uat_host_not_allowed"),
    ({"base_url": "https://example.com"}, "uat_host_not_allowed"),
    ({"enabled": False}, "real_execution_disabled"),
    ({"api_key": ""}, "uat_key_not_configured"),
])
def test_host_https_and_environment_guards(tmp_path, changes, error):
    result = validate(body(), settings(**changes), UatStore(tmp_path / "db.sqlite3"))
    assert not result["valid"] and error in result["blocking_reasons"]


@pytest.mark.parametrize(("mutation", "error"), [
    (lambda x: x["confirmation"].update(confirmed=False), "explicit_confirmation_required"),
    (lambda x: x["request"].update(max_tokens=4096), "uat_model_catalog_unavailable"),
    (lambda x: x["request"].update(messages=[{"role": "user", "content": "Authorization: Bearer secret-value"}]), "sensitive_input_rejected"),
    (lambda x: x["request"].update(stream=True), "stream_execution_not_ready"),
])
def test_request_guard_rules(tmp_path, mutation, error):
    payload = body()
    mutation(payload)
    result = validate(payload, settings(), UatStore(tmp_path / "db.sqlite3"))
    assert not result["valid"] and error in result["blocking_reasons"]


def test_null_daily_limits_are_unlimited_without_erasing_factual_usage(tmp_path):
    store = UatStore(tmp_path / "db.sqlite3")
    with store.connect() as db:
        db.execute("""INSERT INTO daily_budget_usage(
          usage_date,request_count,estimated_cost_cny) VALUES(?,?,?)""", (
            datetime.now(timezone.utc).date().isoformat(), 31, 2.3456))
    current = status(settings(), store)
    assert current["daily_request_limit"] is None
    assert current["daily_budget_cny"] is None
    assert current["request_limit_enabled"] is False
    assert current["budget_limit_enabled"] is False
    assert current["remaining_budget_cny"] is None
    assert current["requests_used_today"] == 31
    assert current["estimated_cost_used_today"] == pytest.approx(2.3456)
    assert current["execution_ready"] is True
    assert current["blocking_reasons"] == []
    result = validate(body(), settings(), store, confirmed_model_catalog(),
                      {"remaining_budget_cny": "3.00"})
    assert result["valid"] is True
    assert "daily_request_limit_reached" not in result["blocking_reasons"]
    assert "daily_budget_limit_reached" not in result["blocking_reasons"]
    assert "daily_budget_exceeded" not in result["blocking_reasons"]


def test_numeric_zero_limits_are_not_treated_as_null(tmp_path):
    result = validate(
        body(), settings(daily_request_limit=0, daily_budget_cny=0),
        UatStore(tmp_path / "db.sqlite3"))
    assert "daily_request_limit_reached" in result["blocking_reasons"]
    assert "daily_budget_exceeded" in result["blocking_reasons"]


def test_shadow_is_stored_before_mock_transport_and_response_is_redacted(tmp_path):
    store = UatStore(tmp_path / "db.sqlite3")
    transport_calls = 0

    def transport(url, key, payload, timeout):
        nonlocal transport_calls
        transport_calls += 1
        assert url == "https://api-uat.weimeta.cn/v1/chat/completions"
        assert key == "test-key-not-real"
        with store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM shadow_decisions").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM uat_response_evidence").fetchone()[0] == 0
        return {
            "status": 200, "headers": {"content-type": "application/json", "x-request-id": "RID-1"},
            "body": json.dumps({"id": "resp-1", "model": "actual-model", "choices": [{"finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}).encode(),
            "elapsed_ms": 12,
        }

    result = execute(body(), settings(), store, shadow, transport,
        model_catalog=confirmed_model_catalog(),
        runtime_constraints={"remaining_budget_cny": "3.00"})
    assert result["execution"]["status"] == "succeeded"
    assert transport_calls == 1
    assert result["execution"]["local_request_limit_enabled"] is False
    assert result["execution"]["local_budget_limit_enabled"] is False
    assert result["network_execution"]["execution_attempted"] is True
    assert result["network_execution"]["network_called"] is True
    assert result["observed_api_result"]["actual_model"] == "actual-model"
    assert result["execution"]["platform_actual_channel"] is None
    serialized = json.dumps(store.get(result["execution"]["execution_id"]))
    assert "test-key-not-real" not in serialized and "Authorization" not in serialized


@pytest.mark.parametrize(("code", "content_type", "payload", "category"), [
    (400, "application/json", b'{"error":"bad"}', "user_parameter_error"),
    (401, "application/json", b'{"error":"auth"}', "platform_authentication_error"),
    (429, "application/json", b'{"error":"rate"}', "rate_limited"),
    (502, "text/html", b"<html>gateway</html>", "gateway_502"),
    (503, "application/json", b'{"error":"upstream"}', "upstream_5xx"),
    (200, "application/json", b"{bad", "protocol_conversion_error"),
])
def test_http_and_malformed_response_contract(code, content_type, payload, category):
    result = classify_response(code, content_type, payload)
    assert result["error_category"] == category
    assert result["content_type"] == content_type
    if category == "gateway_502":
        assert result["response_body_type"] == "html"


def test_timeout_missing_ids_and_actual_model_remain_null():
    timeout = classify_response(None, "", b"", timed_out=True)
    missing = classify_response(200, "application/json", b'{"choices":[{"finish_reason":"stop"}]}')
    assert timeout["error_category"] == "network_timeout"
    assert missing["response_id"] is None and missing["actual_model"] is None


@pytest.mark.parametrize("content_type", ["application/json", "text/plain", ""])
def test_openai_json_parses_with_json_text_or_missing_content_type(content_type):
    payload = {
        "id": "response-from-evidence", "model": "observed-model", "object": "chat.completion", "created": 123,
        "choices": [{"message": {"content": "answer", "reasoning_content": "reason"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
                  "prompt_tokens_details": {"cached_tokens": 4}, "vendor_field": "preserved"},
    }
    parsed = classify_response(200, content_type, json.dumps(payload).encode())
    assert parsed["parsed_body_type"] == "json"
    assert parsed["response_id"] == "response-from-evidence"
    assert parsed["actual_model"] == "observed-model"
    assert parsed["assistant_content"] == "answer"
    assert parsed["reasoning_content"] == "reason"
    assert parsed["cached_tokens"] == 4
    assert parsed["usage_details"]["vendor_field"] == "preserved"
    assert parsed["response_completeness"] == "complete"
    assert parsed["content_type_mismatch"] is (content_type != "application/json")


def test_missing_request_id_is_optional_evidence_metadata_not_failed():
    payload = {"id": "id", "model": "m", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    parsed = classify_response(200, "application/json", json.dumps(payload).encode())
    assert parsed["response_completeness"] == "complete"
    assert parsed["correlation_ready"] is False


def test_incomplete_choices_and_malformed_text_are_preserved():
    incomplete = classify_response(200, "application/json", b'{"id":"x","model":"m","choices":[],"usage":{}}')
    malformed = classify_response(200, "text/plain", b"{not-json")
    assert incomplete["response_completeness"] == "incomplete"
    assert malformed["parsed_body_type"] == "text" and malformed["safe_response"] == "{not-json"


def make_legacy_evidence(store: UatStore):
    execution = {"execution_id": "E-REPARSE", "local_request_id": "R-REPARSE", "decision_id": "D-REPARSE", "created_at": "2026-07-27T00:00:00+00:00", "status": "succeeded", "estimated_cost_cny": 0.0}
    store.save_execution(execution, {}, {"decision_id": "D-REPARSE"})
    original = {"http_status": 200, "content_type": "unknown", "response_body_type": "text", "response_id": None, "actual_model": None, "input_tokens": None, "output_tokens": None, "total_tokens": None, "finish_reason": None, "response_completeness": "failed", "safe_response": json.dumps({"id": "derived-id", "model": "derived-model", "choices": [{"message": {"content": "derived-content"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}})}
    canonical = json.dumps(original, ensure_ascii=False, sort_keys=True)
    with store.connect() as db:
        db.execute("""INSERT INTO uat_response_evidence(execution_id,evidence_sha256,payload)
          VALUES(?,?,?)""", ("E-REPARSE", __import__("hashlib").sha256(canonical.encode()).hexdigest(), canonical))
    return store.response_evidence("E-REPARSE")[0]


def test_reparse_dry_run_amendment_sha_protection_and_idempotence(tmp_path):
    store = UatStore(tmp_path / "db.sqlite3")
    digest = make_legacy_evidence(store)
    dry = reparse_stored_response(store, "E-REPARSE", digest, confirm=False)
    assert dry["dry_run"] and dry["after"]["response_id"] == "derived-id"
    assert store.get("E-REPARSE")["amendments"] == []
    first = reparse_stored_response(store, "E-REPARSE", digest, confirm=True)["amendment"]
    second = reparse_stored_response(store, "E-REPARSE", digest, confirm=True)["amendment"]
    assert first["amendment_id"] == second["amendment_id"]
    assert len(store.get("E-REPARSE")["amendments"]) == 1
    assert store.response_evidence("E-REPARSE")[0] == digest
    with pytest.raises(ValueError, match="sha256_mismatch"):
        reparse_stored_response(store, "E-REPARSE", "0" * 64, confirm=True)


def test_editor_options_come_from_profiles_and_policy():
    result = authenticated_test_client(
        app,runtime=application.ENTERPRISE_HTTP,
        roles=("domestic_uat_operator",)).get("/api/v1/uat/editor-options").json()
    assert [x["id"] for x in result["profiles"]] == ["P01", "P02", "P03", "P04"]
    assert result["profiles"][2]["default_max_tokens"] == 1200
    assert result["profiles"][3]["status"] == "blocked_capability"
    assert result["models"]["access_mode"] == "uat_discovered_models"
    assert result["models"]["fallback_allowed"] == []
    assert "/v1/models" in result["models"]["explanation"]
    assert result["stream_execution_ready"] is False
    assert result["maximum_max_tokens"] is None
    assert result["token_presets"] == [2048, 4096, 8192, 12288, 16384]
    assert {item["model_id"] for item in result["model_output_capabilities"]} == {
        "deepseek-v4-flash", "glm-5.2", "claude-sonnet-5", "gpt-5.6-terra",
        "kimi-k2.7-code", "doubao-seed-2-0-mini-260215",
    }
    assert all(item["status"] in {"pending_confirmation","capability_pending_confirmation"}
               for item in result["model_output_capabilities"])


def test_control_api_enables_runtime_only_with_key_and_never_calls_transport(
        tmp_path, monkeypatch):
    database = tmp_path / "api.sqlite3"
    monkeypatch.setattr(application, "DB_PATH", database)
    isolated_store = UatStore(database)
    monkeypatch.setattr(application, "STORE", isolated_store)
    monkeypatch.setattr(application, "RUNTIME_SETTINGS", EnvironmentRuntimeSettings(database))
    monkeypatch.setenv("WEIMETA_CHINA_UAT_API_KEY", "test-key-not-real")
    called = 0

    def transport(*_args, **_kwargs):
        nonlocal called
        called += 1
        raise AssertionError("transport_must_not_be_called")

    monkeypatch.setattr(application, "UAT_TRANSPORT", transport)
    monkeypatch.setattr(application, "executable_model_catalog",
                        lambda _settings, *_args: confirmed_model_catalog())
    client = authenticated_test_client(
        app, runtime=application.ENTERPRISE_HTTP,
        roles=("domestic_uat_operator",),base_url="http://127.0.0.1:8000")
    toggled = client.put(
        "/api/v1/environments/china_uat/execution-state",
        json={"enabled": True})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["enabled"] is True
    assert set(toggled.json()) == {
        "environment_id", "enabled", "state_source", "updated_at",
        "updated_by", "setting_version",
    }
    persisted = client.get(
        "/api/v1/environments/china_uat/execution-state")
    assert persisted.status_code == 200
    assert persisted.json()["enabled"] is True
    rejected_mixed_payload = client.put(
        "/api/v1/environments/china_uat/execution-state",
        json={"enabled": False, "max_requests": 1,
              "approval_reference": "must-not-be-accepted"})
    assert rejected_mixed_payload.status_code == 400
    assert client.get(
        "/api/v1/environments/china_uat/execution-state").json()["enabled"] is True
    assert called == 0
    before = client.post("/api/v1/uat/execute", json=body())
    assert before.status_code == 403
    assert called == 0

    control_body = {
        "environment_id": "china_uat",
        "allowed_models": ["deepseek-v4-flash", "glm-5.2", "claude-sonnet-5",
                           "gpt-5.6-terra", "kimi-k2.7-code",
                           "doubao-seed-2-0-mini-260215"],
        "allowed_channels": ["unified-routing"], "max_requests": 6,
        "max_total_cost": "3.00", "cost_currency": "CNY",
        "max_duration_seconds": 600, "max_concurrency": 1,
        "max_attempts_per_request": 1,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=9)).isoformat(),
        "explicit_confirmation": True,
    }
    created = client.post("/api/v1/uat/execution-control", json=control_body)
    assert created.status_code == 200
    assert created.json()["execution_ready"] is False
    task_id=created.json()["task"]["task_id"]
    approved=client.post(f"/api/v1/uat/execution-control/{task_id}/approve",json={
      "approval_reference":"pytest-reviewed-approval",
      "approval_expires_at":(datetime.now(timezone.utc)+timedelta(minutes=8)).isoformat()})
    assert approved.status_code==200 and approved.json()["status"]=="APPROVED"
    activated=client.post(f"/api/v1/uat/execution-control/{task_id}/activate")
    assert activated.status_code==200 and activated.json()["execution_ready"] is True
    assert client.get(
        "/api/v1/environments/china_uat/execution-state").json()["enabled"] is True
    disabled = client.put(
        "/api/v1/environments/china_uat/execution-state",
        json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    assert client.get("/api/v1/uat/execution-control").json()["status"] == "ACTIVE"
    blocked_by_environment = client.post("/api/v1/uat/execute", json=body())
    assert blocked_by_environment.status_code == 403
    assert blocked_by_environment.json()["detail"]["code"] == "real_execution_disabled"
    assert called == 0
    reenabled = client.put(
        "/api/v1/environments/china_uat/execution-state",
        json={"enabled": True})
    assert reenabled.status_code == 200 and reenabled.json()["enabled"] is True
    assert client.get("/api/v1/uat/execution-control").json()["status"] == "ACTIVE"
    current = client.get("/api/v1/uat/status").json()
    assert current["real_execution_enabled"] is True
    assert current["execution_ready"] is True
    assert current["execution_control"]["task"]["cost_currency"] == "CNY"
    assert "test-key-not-real" not in json.dumps(current)
    assert called == 0

    def local_transport(*_args, **_kwargs):
        nonlocal called
        called += 1
        return {"status": 200, "headers": {"content-type": "application/json"},
                "body": json.dumps({"id": "response-local-test",
                                     "model": "deepseek-v4-flash",
                                     "choices": [{"finish_reason": "stop",
                                                  "message": {"content": "safe"}}],
                                     "usage": {"prompt_tokens": 2,
                                               "completion_tokens": 3,
                                               "total_tokens": 5}}).encode(),
                "elapsed_ms": 1}

    monkeypatch.setattr(application, "UAT_TRANSPORT", local_transport)
    monkeypatch.setattr(application.MODEL_CATALOG, "model_ids",
                        lambda *_args, **_kwargs: ["deepseek-v4-flash"])
    monkeypatch.setattr(application, "uat_model_capabilities",
                        lambda *_args: confirmed_model_catalog())
    executed = client.post("/api/v1/environments/china_uat/execute", json=body())
    assert executed.status_code == 200, executed.text
    assert called == 1
    execution = executed.json()["execution"]
    assert execution["user_requested_max_tokens"] == 128
    assert execution["effective_max_tokens"] == 128
    assert execution["actual_output_tokens"] == 3
    assert execution["actual_cost"] is None
    control = client.get("/api/v1/uat/execution-control").json()["task"]
    assert control["used_requests"] == 1
    assert control["active_requests"] == 0


def test_error_contract_has_all_17_categories_and_safe_fields():
    assert len(ERROR_CONTRACT) == 18  # required 17 plus honest unknown
    required = {"error_category", "error_layer", "retryable", "fallback_allowed", "recommended_action", "maximum_total_attempts", "evidence_required", "safe_summary"}
    for name in ERROR_CONTRACT:
        result = runtime_error(name, "Bearer secret-value")
        assert required <= result.keys()
        assert "secret-value" not in result["safe_summary"]


def test_correlation_exact_ambiguous_unmatched_and_never_infers_recommendation():
    execution = {"execution_id": "E1", "response": {"request_id_header": "RID-1"}, "shadow_decision": {"recommended_candidate": "19"}}
    exact = correlate(execution, [{"platform_log_id": "L1", "request_id": "RID-1", "channel_id": "27"}])
    ambiguous = correlate(execution, [{"platform_log_id": "L1", "request_id": "RID-1"}, {"platform_log_id": "L2", "request_id": "RID-1"}])
    unmatched = correlate(execution, [{"platform_log_id": "L3", "request_id": "OTHER"}])
    assert exact["correlation_status"] == "exact_match" and exact["platform_actual_channel"] == "27"
    assert ambiguous["correlation_status"] == "ambiguous" and ambiguous["platform_actual_channel"] is None
    assert unmatched["correlation_status"] == "unmatched" and unmatched["platform_actual_channel"] is None


def test_schema_has_integrity_tables_and_no_secret_columns(tmp_path):
    store = UatStore(tmp_path / "db.sqlite3")
    with store.connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r[1] for table in tables for r in db.execute(f"PRAGMA table_info({table})")}
    assert {"uat_executions", "uat_request_evidence", "uat_response_evidence", "shadow_decisions", "route_correlations", "daily_budget_usage", "audit_events", "evidence_amendments", "imported_log_records"} <= tables
    assert not {"api_key", "authorization", "cookie"} & columns


def test_status_api_has_no_key(monkeypatch):
    monkeypatch.setenv("WEIMETA_UAT_API_KEY", "never-render-this-value")
    response = authenticated_test_client(
        app,runtime=application.ENTERPRISE_HTTP,
        roles=("domestic_uat_operator",)).get("/api/v1/uat/status")
    assert response.status_code == 200 and response.json()["key_configured"] is True
    assert "never-render-this-value" not in response.text
