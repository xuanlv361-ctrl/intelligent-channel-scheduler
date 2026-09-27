from __future__ import annotations

import sys
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from formal_agent_skill_registry import FormalAgentSkillError, FormalAgentSkillRegistry
from formal_agent_skill_security import FormalAgentSkillSecurity
from backend.formal_agent_skill_bindings import FormalAgentSkillBindings


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def security(clock=None):
    registry = FormalAgentSkillRegistry()
    return FormalAgentSkillSecurity(
        allowed_origins={"local-agent://runtime", "http://127.0.0.1:5174"},
        allowed_skill_ids=set(registry.skills), clock=clock or Clock(),
        grant_ttl_seconds=2, session_ttl_seconds=4)


def valid_preconditions():
    return {
        "budget": {"enabled": True, "allowed": True, "version": "budget-v1"},
        "circuit": {"enabled": True, "state": "CLOSED", "version": "circuit-v1"},
        "capability": {"enabled": True, "allowed": True, "status": "confirmed",
                       "version": "capability-v1"},
        "kill_switch": {"enforcement_enabled": True, "active": False,
                        "version": "kill-v1"},
    }


def injected_security(provider):
    registry = FormalAgentSkillRegistry()
    return FormalAgentSkillSecurity(
        allowed_origins={"local-agent://runtime"},
        allowed_skill_ids=set(registry.skills), clock=Clock(),
        precondition_provider=provider)


def grant(sec):
    session = sec.open_session(actor_id="reasoning-agent", origin="local-agent://runtime")
    lease = sec.acquire_lease(session_id=session, skill_id="inspect-circuit-breaker")
    return sec.issue_grant(session_id=session, lease_id=lease,
                           skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")


def test_origin_session_lease_and_operation_are_enforced():
    clock = Clock(); sec = security(clock)
    with pytest.raises(FormalAgentSkillError, match="origin_rejected"):
        sec.open_session(actor_id="agent", origin="http://127.0.0.1:5174.evil.test")
    session = sec.open_session(actor_id="agent", origin="local-agent://runtime")
    lease = sec.acquire_lease(session_id=session, skill_id="inspect-circuit-breaker")
    with pytest.raises(FormalAgentSkillError, match="operation_forbidden"):
        sec.issue_grant(session_id=session, lease_id=lease,
                        skill_id="inspect-circuit-breaker", binding="circuit_breaker.write")
    clock.advance(5)
    with pytest.raises(FormalAgentSkillError, match="session_invalid_or_expired"):
        sec.issue_grant(session_id=session, lease_id=lease,
                        skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")


def test_grant_is_sealed_one_use_and_binding_scoped():
    sec = security(); value = grant(sec)
    sec.consume_grant(value, skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")
    with pytest.raises(FormalAgentSkillError, match="invalid_expired_or_reused"):
        sec.consume_grant(value, skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")
    forged = replace(grant(sec), binding="exploration_governance.read")
    with pytest.raises(FormalAgentSkillError, match="invalid_expired_or_reused"):
        sec.consume_grant(forged, skill_id="inspect-circuit-breaker", binding="exploration_governance.read")


def test_direct_binding_call_requires_valid_grant():
    sec = security()
    bindings = FormalAgentSkillBindings({
        "circuit_breaker.read": lambda args: {"state": "OPEN", "network_called": False}})
    with pytest.raises(FormalAgentSkillError, match="grant_required"):
        bindings.invoke(skill_id="inspect-circuit-breaker", binding="circuit_breaker.read",
                        arguments={}, grant=None, security=sec)
    with pytest.raises(FormalAgentSkillError, match="binding_configuration_invalid"):
        FormalAgentSkillBindings({"uat.execute": lambda args: {}})


@pytest.mark.parametrize(
    "mutation,error",
    [
        (("missing", "budget", None), "preconditions_missing"),
        (("budget", "enabled", False), "budget_precondition_denied"),
        (("budget", "allowed", False), "budget_precondition_denied"),
        (("circuit", "state", "UNKNOWN"), "circuit_precondition_denied"),
        (("circuit", "enabled", False), "circuit_precondition_denied"),
        (("capability", "status", "unknown"), "capability_precondition_denied"),
        (("capability", "enabled", False), "capability_precondition_denied"),
        (("kill_switch", "active", True), "kill_switch_precondition_denied"),
        (("kill_switch", "enforcement_enabled", False), "kill_switch_precondition_denied"),
    ],
)
def test_missing_unknown_denied_or_disabled_preconditions_fail_closed(mutation, error):
    state = valid_preconditions()
    if mutation[0] == "missing":
        state.pop(mutation[1])
    else:
        state[mutation[0]][mutation[1]] = mutation[2]
    sec = injected_security(lambda _context: state)
    session = sec.open_session(actor_id="agent", origin="local-agent://runtime")
    lease = sec.acquire_lease(session_id=session, skill_id="inspect-circuit-breaker")
    with pytest.raises(FormalAgentSkillError, match=error):
        sec.issue_grant(session_id=session, lease_id=lease,
                        skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")


def test_grant_seal_binds_precondition_versions_and_drift_invalidates_one_use_grant():
    state = valid_preconditions()
    sec = injected_security(lambda _context: deepcopy(state))
    value = grant(sec)
    assert value.precondition_sha256
    assert value.budget_policy_version == "budget-v1"
    forged = replace(value, circuit_policy_version="circuit-forged")
    with pytest.raises(FormalAgentSkillError, match="invalid_expired_or_reused"):
        sec.consume_grant(
            forged, skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")

    state["circuit"]["state"] = "OPEN"
    with pytest.raises(FormalAgentSkillError, match="circuit_precondition_denied"):
        sec.consume_grant(
            value, skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")
    state["circuit"]["state"] = "CLOSED"
    with pytest.raises(FormalAgentSkillError, match="invalid_expired_or_reused"):
        sec.consume_grant(
            value, skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")


def test_version_only_drift_is_rejected_and_grant_cannot_be_restored():
    state = valid_preconditions()
    sec = injected_security(lambda _context: deepcopy(state))
    value = grant(sec)
    state["budget"]["version"] = "budget-v2"
    with pytest.raises(FormalAgentSkillError, match="precondition_state_drift"):
        sec.validate_grant(
            value, skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")
    state["budget"]["version"] = "budget-v1"
    with pytest.raises(FormalAgentSkillError, match="invalid_expired_or_reused"):
        sec.consume_grant(
            value, skill_id="inspect-circuit-breaker", binding="circuit_breaker.read")
