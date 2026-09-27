"""Regression lessons derived from pinned, read-only GitHub references.

These tests execute local services only.  They perform no network I/O and do
not import or execute code from the temporary reference repositories.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.circuit_breaker_service import CircuitBreakerService  # noqa: E402
from backend.exploration_governance_service import (  # noqa: E402
    ExplorationGovernanceService,
)
from backend.price_catalog_service import PriceCatalogService  # noqa: E402
from decision_logger import DecisionLogger  # noqa: E402
from execution_engine import ExecutionEngine, ScriptedMockExecutor  # noqa: E402
from formal_agent_skill_registry import (  # noqa: E402
    FormalAgentSkillError,
    FormalAgentSkillRegistry,
)
from formal_agent_skill_security import FormalAgentSkillSecurity  # noqa: E402
from retry_policy import RetryPolicy  # noqa: E402
from scheduler import Scheduler  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def retry_policy() -> RetryPolicy:
    return RetryPolicy(json.loads(
        (ROOT / "config" / "retry_policy_v2.json").read_text(encoding="utf-8")
    ))


def run_recovery(results, fallback_order):
    engine = ExecutionEngine(ScriptedMockExecutor(results), retry_policy())
    return engine.execute(
        request=type("Request", (), {"request_id": "GITHUB-REGRESSION"})(),
        decision={
            "recommended_candidate": "primary",
            "fallback_order": fallback_order,
        },
        candidates={
            "primary": {"candidate_id": "primary"},
            "backup": {"candidate_id": "backup"},
            "third": {"candidate_id": "third"},
        },
    )


def circuit_policy(quota=1):
    return {
        "policy_version": "github-reference-regression-v1",
        "enabled": True,
        "failure_window_seconds": 60,
        "failure_threshold": 1,
        "cooldown_seconds": 5,
        "half_open_probe_quota": quota,
        "half_open_success_threshold": 1,
        "probe_lease_seconds": 10,
    }


def open_circuit(service):
    assert service.record_failure("china:channel:model", "failure-1")["state"] == "OPEN"


def price_record():
    return {
        "environment_id": "china_uat",
        "model_id": "model-a",
        "channel_id": "channel-a",
        "currency": "CNY",
        "input_unit_price": "1.25",
        "output_unit_price": "2.50",
        "billing_unit": "per_1m_tokens",
        "effective_from": "2026-07-01T00:00:00Z",
        "effective_until": None,
    }


def test_retry_plus_fallback_cannot_create_unbounded_loop():
    result = run_recovery(
        {
            "primary": {"status": "failed", "error": "upstream_timeout"},
            "backup": {"status": "failed", "error": "service_unavailable"},
            "third": {"status": "success"},
        },
        ["backup", "primary", "third", "backup"],
    )
    assert len(result["attempts"]) == retry_policy().max_attempts == 2
    assert result["network_called"] is False


def test_failed_deployment_is_not_immediately_reused():
    result = run_recovery(
        {
            "primary": {"status": "failed", "error": "upstream_timeout"},
            "backup": {"status": "failed", "error": "service_unavailable"},
        },
        ["backup", "primary", "backup"],
    )
    assert result["fallback_trace"] == ["primary", "backup"]
    assert len(set(result["fallback_trace"])) == len(result["fallback_trace"])


def test_cooldown_state_is_consistent_across_attempts_and_instances(tmp_path):
    clock = Clock()
    database = tmp_path / "circuit.sqlite3"
    first = CircuitBreakerService(database, circuit_policy(), clock=clock)
    second = CircuitBreakerService(database, circuit_policy(), clock=clock)
    open_circuit(first)
    assert second.get_state("china:channel:model")["state"] == "OPEN"
    assert second.before_request("china:channel:model", "other-instance")["allowed"] is False


def test_streaming_and_nonstreaming_share_safety_classification():
    policy = retry_policy()
    common = policy.retry_decision("upstream_timeout", 1, output_started=False)
    streaming_after_output = policy.retry_decision(
        "upstream_timeout", 1, output_started=True
    )
    assert common["allowed"] is True
    assert streaming_after_output == {
        "allowed": False,
        "delay_seconds": 0.0,
        "reason": "output_started_fallback_forbidden",
    }


def test_fallback_does_not_reset_global_attempt_budget():
    result = run_recovery(
        {
            "primary": {"status": "failed", "error": "upstream_timeout"},
            "backup": {"status": "failed", "error": "service_unavailable"},
            "third": {"status": "success"},
        },
        ["backup", "third"],
    )
    assert result["fallback_trace"] == ["primary", "backup"]
    assert "third" not in result["fallback_trace"]
    assert result["stopped_reason"] == "non_retryable_error_or_attempt_limit"


def test_stale_or_missing_price_never_becomes_zero(tmp_path):
    clock = Clock()

    def transport(_source, _timeout):
        return {
            "payload": {
                "schema_version": "price_catalog_v1",
                "records": [price_record()],
            },
            "network_called": False,
        }

    service = PriceCatalogService(
        tmp_path / "prices.sqlite3",
        ROOT / "config" / "price_sync_policy_v1.json",
        transport=transport,
        clock=clock,
        development_mode=True,
    )
    assert service.synchronize("contract_mock")["status"] == "ready"
    clock.advance(2 * 24 * 60 * 60)
    stale = service.resolve(
        environment_id="china_uat", model_id="model-a",
        channel_id="channel-a", currency="CNY",
    )
    missing = service.resolve(
        environment_id="china_uat", model_id="model-a",
        channel_id="missing", currency="CNY",
    )
    assert stale["eligible"] is False and stale["status"] == "stale"
    assert missing["eligible"] is False and missing["status"] == "unavailable"
    assert "input_unit_price" not in missing and "output_unit_price" not in missing


def test_actual_execution_attribution_is_never_inferred_from_intent(tmp_path):
    scheduler = Scheduler(
        logger=DecisionLogger(tmp_path / "decisions.jsonl"),
        id_generator=lambda _request, _index: "GITHUB-ATTRIBUTION-1",
    )
    result = scheduler.route({
        "request_id": "GITHUB-ATTRIBUTION",
        "requested_model": "deepseek-v4-flash",
        "stream": False,
        "input_tokens": 10,
        "output_tokens": 10,
        "currency": "CNY",
        "strategy": "confidence_aware_v2",
        "mode": "simulation",
        "metadata": {"environment_id": "offline_benchmark"},
    })
    assert result["recommended_candidate"] is not None
    assert result["selected_channel_id"] is not None
    assert result["authoritative_actual_channel"] is None
    assert result["attribution_status"] == "authoritative_execution_attribution_missing"


@pytest.mark.parametrize(
    "blocked_gate,expected",
    [
        ("budget", "budget_precondition_denied"),
        ("circuit", "circuit_precondition_denied"),
        ("capability", "capability_precondition_denied"),
        ("kill_switch", "kill_switch_precondition_denied"),
    ],
)
def test_external_configuration_cannot_bypass_safety_gates(blocked_gate, expected):
    state = {
        "budget": {"enabled": True, "allowed": True, "version": "budget-v1"},
        "circuit": {"enabled": True, "state": "CLOSED", "version": "circuit-v1"},
        "capability": {
            "enabled": True, "allowed": True, "status": "confirmed",
            "version": "capability-v1",
        },
        "kill_switch": {
            "enforcement_enabled": True, "active": False, "version": "kill-v1",
        },
    }
    if blocked_gate == "budget":
        state[blocked_gate]["allowed"] = False
    elif blocked_gate == "circuit":
        state[blocked_gate]["state"] = "OPEN"
    elif blocked_gate == "capability":
        state[blocked_gate]["status"] = "unknown"
    else:
        state[blocked_gate]["active"] = True
    registry = FormalAgentSkillRegistry()
    security = FormalAgentSkillSecurity(
        allowed_origins={"local-agent://runtime"},
        allowed_skill_ids=set(registry.skills),
        precondition_provider=lambda _context: state,
    )
    with pytest.raises(FormalAgentSkillError, match="origin_rejected"):
        security.open_session(
            actor_id="caller", origin="local-agent://runtime.evil.example"
        )
    session = security.open_session(
        actor_id="caller", origin="local-agent://runtime"
    )
    lease = security.acquire_lease(
        session_id=session, skill_id="inspect-circuit-breaker"
    )
    with pytest.raises(FormalAgentSkillError, match=expected):
        security.issue_grant(
            session_id=session,
            lease_id=lease,
            skill_id="inspect-circuit-breaker",
            binding="circuit_breaker.read",
        )


def test_concurrent_half_open_probes_cannot_exceed_quota(tmp_path):
    clock = Clock()
    service = CircuitBreakerService(
        tmp_path / "circuit.sqlite3", circuit_policy(quota=1), clock=clock
    )
    open_circuit(service)
    clock.advance(5)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(
            lambda index: service.before_request(
                "china:channel:model", f"probe-{index}"
            ),
            range(12),
        ))
    assert sum(item["allowed"] is True for item in results) == 1
    assert sum(item["reason"] == "probe_quota_exhausted" for item in results) == 11


def test_restart_does_not_reset_circuit_or_exploration_state(tmp_path):
    clock = Clock()
    circuit_db = tmp_path / "circuit.sqlite3"
    circuit = CircuitBreakerService(circuit_db, circuit_policy(), clock=clock)
    open_circuit(circuit)
    assert CircuitBreakerService(
        circuit_db, circuit_policy(), clock=clock
    ).get_state("china:channel:model")["state"] == "OPEN"

    exploration_db = tmp_path / "exploration.sqlite3"
    policy = ROOT / "config" / "exploration_policy_v1.json"
    exploration = ExplorationGovernanceService(
        exploration_db, policy, clock=clock, environ={}
    )
    exploration.set_kill_switch(active=True, reason="incident")
    restarted = ExplorationGovernanceService(
        exploration_db, policy, clock=clock, environ={}
    )
    assert restarted.status()["kill_switches"] == [{
        "scope_type": "global",
        "scope_id": "*",
        "active": 1,
        "updated_at": "2026-08-01T00:00:00Z",
    }]


def test_trace_logging_cannot_leak_protected_content(tmp_path):
    logger = DecisionLogger(tmp_path / "trace.jsonl")
    protected = "github-reference-protected-content"
    logger.log_runtime({
        "decision_id": "TRACE-1",
        "runtime_version": "runtime-v1",
        "request_id": "REQUEST-1",
        "mode": "simulation",
        "strategy": "confidence_aware_v2",
        "metadata": {
            "messages": [{"content": protected}],
            "Cookie": protected,
            "response_body": protected,
            "safe_id": "SAFE-1",
        },
    })
    raw = (tmp_path / "trace.jsonl").read_text(encoding="utf-8")
    assert protected not in raw
    assert logger.read_all()[0]["metadata"]["messages"] == "[REDACTED]"
    assert logger.read_all()[0]["metadata"]["safe_id"] == "SAFE-1"
