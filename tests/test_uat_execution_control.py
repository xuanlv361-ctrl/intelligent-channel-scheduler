from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend import app as _application  # noqa: F401
from backend.tenant_security import TenantScope
from backend.uat_execution_control_service import (
    UatExecutionControlError, UatExecutionControlService,
)
from backend.uat_service import UatSettings, UatStore, validate


MODELS = [
    "deepseek-v4-flash", "glm-5.2", "claude-sonnet-5", "gpt-5.6-terra",
    "kimi-k2.7-code", "doubao-seed-2-0-mini-260215",
]
CHANNEL = "unified-routing"


def body(now=None, **overrides):
    now = now or datetime.now(timezone.utc)
    value = {
        "environment_id": "china_uat", "allowed_models": MODELS,
        "allowed_channels": [CHANNEL], "max_requests": 24,
        "max_total_cost": "3.00", "cost_currency": "CNY",
        "max_duration_seconds": 1200, "max_concurrency": 1,
        "max_attempts_per_request": 1,
        "expires_at": (now + timedelta(minutes=19)).isoformat(),
        "explicit_confirmation": True,
    }
    value.update(overrides)
    return value


def activate(service, payload=None):
    created = service.create(payload or body())
    task_id = created["task"]["task_id"]
    service.approve(task_id, approval_reference="stage4-approved",
                    approval_expires_at=(service._now() + timedelta(minutes=18)).isoformat())
    return service.activate(task_id)


def test_task_draft_approval_and_activation_are_distinct(tmp_path):
    service = UatExecutionControlService(tmp_path / "control.sqlite3")
    assert service.status()["status"] == "DISABLED"
    draft = service.create(body())
    assert draft["status"] == "DRAFT" and not draft["execution_ready"]
    task_id = draft["task"]["task_id"]
    approved = service.approve(task_id, approval_reference="approved-change",
        approval_expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat())
    assert approved["status"] == "APPROVED" and not approved["execution_ready"]
    active = service.activate(task_id)
    assert active["status"] == "ACTIVE" and active["execution_ready"]
    assert [item["event_type"] for item in active["task"]["audit_log"]][:3] == [
        "execution_control_activated", "execution_control_approved",
        "execution_control_draft_created"]


def test_attempt_limit_is_required_bounded_persisted_and_audited(tmp_path):
    service = UatExecutionControlService(tmp_path / "control.sqlite3")
    missing = body()
    missing.pop("max_attempts_per_request")
    with pytest.raises(UatExecutionControlError, match="execution_limits_invalid"):
        service.create(missing)
    with pytest.raises(UatExecutionControlError, match="execution_limits_invalid"):
        service.create(body(max_attempts_per_request=4))
    draft = service.create(body(max_attempts_per_request=2))
    assert draft["task"]["max_attempts_per_request"] == 2
    created = next(item for item in draft["task"]["audit_log"]
                   if item["event_type"] == "execution_control_draft_created")
    assert created["details"]["max_attempts_per_request"] == 2


def test_model_channel_concurrency_budget_and_kill_fail_closed(tmp_path):
    service = UatExecutionControlService(tmp_path / "control.sqlite3")
    activate(service, body(max_total_cost="0.01"))
    with pytest.raises(UatExecutionControlError, match="model_not_approved"):
        service.reserve("forged", CHANNEL, Decimal("0.001"))
    with pytest.raises(UatExecutionControlError, match="channel_not_approved"):
        service.reserve(MODELS[0], "forged", Decimal("0.001"))
    reservation = service.reserve(MODELS[0], CHANNEL, Decimal("0.005"))
    with pytest.raises(UatExecutionControlError, match="concurrency_limit"):
        service.reserve(MODELS[0], CHANNEL, Decimal("0.001"))
    service.complete(reservation["reservation_id"], "success", Decimal("0.005"))
    with pytest.raises(UatExecutionControlError, match="budget_exceeded"):
        service.reserve(MODELS[0], CHANNEL, Decimal("0.006"))
    killed = service.stop("operator_kill_switch", kill_switch=True)
    assert killed["status"] == "KILLED" and killed["task"]["kill_switch_active"]


def test_expiry_request_and_cost_terminal_states(tmp_path):
    current = [datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)]
    service = UatExecutionControlService(tmp_path / "control.sqlite3", clock=lambda: current[0])
    active = activate(service, body(current[0], max_duration_seconds=300,
        expires_at=(current[0]+timedelta(minutes=5)).isoformat()))
    current[0] = datetime.fromisoformat(active["task"]["expires_at"])
    assert service.status()["status"] == "EXPIRED"
    current[0] += timedelta(seconds=1)
    activate(service, body(current[0], max_requests=1, max_duration_seconds=300,
        expires_at=(current[0]+timedelta(minutes=5)).isoformat()))
    reservation = service.reserve(MODELS[0], CHANNEL, Decimal("0.001"))
    service.complete(reservation["reservation_id"], "success", Decimal("0.001"))
    assert service.status()["task"]["stopped_reason"] == "request_budget_exhausted"
    current[0] += timedelta(seconds=1)
    activate(service, body(current[0], max_total_cost="0.01", max_duration_seconds=300,
        expires_at=(current[0]+timedelta(minutes=5)).isoformat()))
    reservation = service.reserve(MODELS[0], CHANNEL, Decimal("0.01"))
    service.complete(reservation["reservation_id"], "success", Decimal("0.01"))
    assert service.status()["status"] == "BUDGET_EXHAUSTED"


def test_approval_expiry_restart_recovery_and_tenant_isolation(tmp_path):
    current = [datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)]
    path = tmp_path / "control.sqlite3"
    service = UatExecutionControlService(path, clock=lambda: current[0])
    draft = service.create(body(current[0]))
    service.approve(draft["task"]["task_id"], approval_reference="short",
        approval_expires_at=(current[0]+timedelta(seconds=1)).isoformat())
    current[0] += timedelta(seconds=1)
    with pytest.raises(UatExecutionControlError, match="approval_expired"):
        service.activate(draft["task"]["task_id"])
    recovered = UatExecutionControlService(path, clock=lambda: current[0])
    assert recovered.status()["status"] == "EXPIRED"
    other = UatExecutionControlService(path, TenantScope("tenant-other", "workspace-other"),
                                       clock=lambda: current[0])
    assert other.status()["status"] == "DISABLED"


def settings(tmp_path):
    return UatSettings(environment="uat", base_url="https://api-uat.weimeta.cn",
        api_key="test-not-a-real-key", enabled=True,
        allowed_hosts=("api-uat.weimeta.cn",), timeout_seconds=30,
        max_tokens=None, daily_request_limit=None, daily_budget_cny=None,
        max_request_cost_cny=3.0), UatStore(tmp_path / "uat.sqlite3")


def request(max_tokens, stream=False):
    return {"mode":"real_uat_execute","confirmation":{"confirmed":True,
      "confirmation_text":"I understand this will call the Weimeta UAT API and may incur UAT cost."},
      "request":{"requested_model":MODELS[0],"channel_id":CHANNEL,
      "messages":[{"role":"user","content":"safe fixed prompt"}],
      "stream":stream,"max_tokens":max_tokens}}


def capability(model=20000, channel=18000, context=30000):
    return {MODELS[0]: {"confirmed_max_output_tokens":model,
      "confirmed_channel_max_output_tokens":channel,"max_context_tokens":context,
      "max_input_tokens":context,
      "evidence_source":"reviewed_configuration","evidence_version":"v1"}}


@pytest.mark.parametrize("maximum", [2048,4096,8192,12288,16384,17777])
def test_user_token_value_is_not_clamped_and_effective_value_is_request(tmp_path, maximum):
    configured, store = settings(tmp_path)
    result = validate(request(maximum), configured, store, capability(),
                      {"remaining_budget_cny":"3.00"})
    assert result["valid"] is True
    assert result["token_boundary"]["user_requested_max_tokens"] == maximum
    assert result["token_boundary"]["effective_max_tokens"] == maximum


@pytest.mark.parametrize("stream", [False, True])
def test_stream_and_non_stream_share_token_boundary(tmp_path, stream):
    configured, store = settings(tmp_path)
    result = validate(request(4096, stream), configured, store, capability(),
                      {"remaining_budget_cny":"3.00"})
    assert result["token_boundary"]["effective_max_tokens"] == 4096
    if stream:
        assert "stream_execution_not_ready" in result["blocking_reasons"]


def test_each_boundary_and_unknown_evidence_fail_closed(tmp_path):
    configured, store = settings(tmp_path)
    unknown = validate(request(4096), configured, store, {MODELS[0]: {}},
                       {"remaining_budget_cny":"3.00"})
    assert {"model_output_limit_unconfirmed","channel_output_limit_unconfirmed",
            "context_capacity_unconfirmed"} <= set(unknown["blocking_reasons"])
    assert "model_output_limit_exceeded" in validate(request(12288), configured, store,
      capability(model=8192), {"remaining_budget_cny":"3.00"})["blocking_reasons"]
    assert "channel_output_limit_exceeded" in validate(request(12288), configured, store,
      capability(channel=8192), {"remaining_budget_cny":"3.00"})["blocking_reasons"]
    assert "context_capacity_exceeded" in validate(request(12288), configured, store,
      capability(context=10000), {"remaining_budget_cny":"3.00"})["blocking_reasons"]
    assert "execution_budget_output_limit_exceeded" in validate(request(4096), configured,
      store, capability(), {"remaining_budget_cny":"0.000001"})["blocking_reasons"]


def test_non_positive_and_missing_runtime_budget_are_rejected(tmp_path):
    configured, store = settings(tmp_path)
    assert "max_tokens_must_be_positive" in validate(request(0), configured, store,
      capability(), {"remaining_budget_cny":"3.00"})["blocking_reasons"]
    assert "execution_budget_unconfirmed" in validate(request(2048), configured, store,
      capability(), {})["blocking_reasons"]
