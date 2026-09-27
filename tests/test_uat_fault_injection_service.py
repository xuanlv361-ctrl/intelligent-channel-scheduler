from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from backend.tenant_security import TenantScope
from backend.uat_fault_injection_service import (
    UatFaultInjectionError,
    UatFaultInjectionService,
)


def payload(now, **updates):
    result = {
        "acceptance_run_id": "acceptance-42",
        "environment_id": "china_uat",
        "model": "model-a",
        "error_type": "503",
        "count": 2,
        "delay_ms": 10,
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "error_layer": "pre_request",
        "affects_circuit": True,
    }
    result.update(updates)
    return result


def test_create_is_disabled_and_production_create_and_application_are_rejected(tmp_path):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    service = UatFaultInjectionService(tmp_path / "faults.db", clock=lambda: now)
    rule = service.create(payload(now))
    assert rule["enabled"] is False
    assert service.pre_request_outcome(environment_id="china_uat", model="model-a")["provider_live_passthrough"]
    with pytest.raises(UatFaultInjectionError, match="environment_not_allowed"):
        service.create(payload(now, environment_id="production"))
    with pytest.raises(UatFaultInjectionError, match="environment_not_allowed"):
        service.pre_request_outcome(environment_id="production", model="model-a")


def test_concurrent_consumption_is_exact_and_returns_audit_metadata(tmp_path):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    service = UatFaultInjectionService(tmp_path / "faults.db", clock=lambda: now)
    rule = service.create(payload(now, count=17))
    service.enable(rule["fault_id"])

    def consume(_):
        return service.pre_request_outcome(
            environment_id="china_uat", model="model-a",
            acceptance_run_id="acceptance-42")

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(consume, range(60)))
    injected = [item for item in outcomes if item["is_fault_injected"]]
    passed = [item for item in outcomes if not item["is_fault_injected"]]
    assert len(injected) == 17
    assert len(passed) == 43
    assert all(item["error_source"] == "uat_fault_injection" for item in injected)
    assert all(item["fault_id"] == rule["fault_id"] and item["audit_id"] for item in injected)
    assert service.get(rule["fault_id"])["remaining_count"] == 0
    assert service.get(rule["fault_id"])["enabled"] is False


def test_expiry_cleanup_and_tenant_workspace_isolation(tmp_path):
    current = [datetime(2026, 8, 6, tzinfo=timezone.utc)]
    path = tmp_path / "faults.db"
    first = UatFaultInjectionService(path, TenantScope("tenant-a", "workspace-a"),
                                     clock=lambda: current[0])
    other = UatFaultInjectionService(path, TenantScope("tenant-a", "workspace-b"),
                                     clock=lambda: current[0])
    rule = first.create(payload(current[0], ttl_seconds=2, expires_at=None))
    first.enable(rule["fault_id"])
    assert other.list_rules() == []
    current[0] += timedelta(seconds=3)
    outcome = first.pre_request_outcome(environment_id="china_uat", model="model-a")
    assert outcome["error_source"] == "provider_live"
    assert first.cleanup_by_acceptance_run_id("acceptance-42")["deleted_rule_count"] == 1
    assert first.list_rules() == []
    assert other.list_rules() == []


@pytest.mark.parametrize("secret", [
    {"api_key": "sk-live-value"},
    {"metadata": {"authorization": "Bearer live-value"}},
    {"access_token": "live-value"},
    {"model": "sk-abcdefgh12345678"},
])
def test_sensitive_fields_are_rejected_and_never_persisted_or_logged(tmp_path, secret):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    path = tmp_path / "faults.db"
    service = UatFaultInjectionService(path, clock=lambda: now)
    body = payload(now)
    body.update(secret)
    with pytest.raises(UatFaultInjectionError) as exc:
        service.create(body)
    assert "live-value" not in str(exc.value)
    assert "live-value" not in path.read_bytes().decode("utf-8", errors="ignore")


def test_layer_model_run_matching_and_decorator_outcomes(tmp_path):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    service = UatFaultInjectionService(tmp_path / "faults.db", clock=lambda: now)
    rule = service.create(payload(now, model="*", error_type="sse_malformed",
                                  error_layer="stream", affects_circuit=False))
    service.enable_rule(rule["fault_id"])
    assert service.post_response_outcome(environment_id="china_uat", model="model-z")["provider_live"]
    outcome = service.stream_outcome(environment_id="china_uat", model="model-z",
                                     acceptance_run_id="acceptance-42")
    assert outcome["error_type"] == "sse_malformed"
    assert outcome["error_layer"] == "stream"
    assert outcome["affects_circuit"] is False
