from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from backend.circuit_breaker_service import CircuitBreakerService
from backend.capability_evidence_service import CapabilityEvidenceService
from backend.high_cost_test_governance_service import (
    HighCostTestGovernanceError,
    HighCostTestGovernanceService,
)
from backend.exploration_governance_service import ExplorationGovernanceService
from backend.probe_governance_service import ProbeGovernanceError, ProbeGovernanceService
from backend.traffic_change_governance_service import (
    TrafficChangeGovernanceError,
    TrafficChangeGovernanceService,
)
from backend.formal_agent_skill_audit_service import FormalAgentSkillAuditService
from backend.formal_agent_skill_bindings import FormalAgentSkillBindings
from backend.security.authorization import AuthorizationError, AuthorizationService
from backend.security.config import EnterpriseIdentityConfig
from backend.security.identity import PrincipalResolver, VerifiedIdentity
from backend.security.principal import PrincipalType
from decision_logger import DecisionLogger
from formal_agent_skill_runtime import FormalAgentSkillRuntime
from scheduler import Scheduler


def security():
    auth = AuthorizationService(ROOT / "config/rbac_permissions_v1.json")
    config = EnterpriseIdentityConfig.load(ROOT / "config/enterprise_identity_v1.json")
    return auth, PrincipalResolver(config, auth)


def principal(resolver, *, tenant="tenant-a", workspace="workspace-a",
              role="scheduler_service", kind=PrincipalType.SERVICE):
    now = datetime.now(timezone.utc)
    return resolver.resolve_verified(VerifiedIdentity(
        principal_id=f"{kind.value}:{role}", principal_type=kind,
        tenant_id=tenant, workspace_id=workspace, roles=(role,),
        authn_method="verified-test-adapter", issued_at=now,
        expires_at=now + timedelta(minutes=5),
        session_id="SES-human" if kind is PrincipalType.HUMAN else None,
        token_id=f"JTI-{tenant}-{role}", is_development_identity=False,
        security_version=1))


REQUEST = {
    "request_id": "ENT-SCHED-1", "requested_model": "deepseek-v4-flash",
    "stream": False, "input_tokens": 10, "output_tokens": 10,
    "currency": "CNY", "strategy": "confidence_aware_v2",
    "mode": "simulation", "metadata": {
        "environment_id": "offline_benchmark",
        # These fields must never be treated as an identity source.
        "principal_id": "service:scheduler", "roles": ["scheduler_service"],
        "tenant_id": "tenant-forged", "workspace_id": "workspace-forged",
    },
}


def test_scheduler_direct_call_requires_verified_scheduler_service(tmp_path):
    auth, resolver = security()
    scheduler = Scheduler(
        logger=DecisionLogger(tmp_path / "decisions.jsonl"),
        authorization_service=auth)
    with pytest.raises(AuthorizationError, match="missing_or_unverified"):
        scheduler.route(REQUEST)
    human = principal(
        resolver, role="operations_admin", kind=PrincipalType.HUMAN)
    with pytest.raises(AuthorizationError, match="principal_type_mismatch"):
        scheduler.route(REQUEST, principal=human)
    worker = principal(resolver, role="worker_service")
    with pytest.raises(AuthorizationError, match="permission_denied"):
        scheduler.route(REQUEST, principal=worker)

    scheduler_identity = principal(resolver)
    result = scheduler.route(REQUEST, principal=scheduler_identity)
    assert result["principal"]["tenant_id"] == "tenant-a"
    assert result["principal"]["workspace_id"] == "workspace-a"
    assert result["principal"]["tenant_id"] != REQUEST["metadata"]["tenant_id"]


def test_circuit_state_idempotency_and_reads_are_tenant_isolated(tmp_path):
    auth, resolver = security()
    policy = json.loads(
        (ROOT / "config/circuit_breaker_policy_v1.json").read_text())
    policy["failure_threshold"] = 1
    service = CircuitBreakerService(
        tmp_path / "circuits.sqlite3", policy, authorization_service=auth)
    tenant_a = principal(resolver, tenant="tenant-a")
    tenant_b = principal(resolver, tenant="tenant-b")

    assert service.record_failure(
        "shared-circuit", "shared-event", principal=tenant_a)["state"] == "OPEN"
    assert service.get_state(
        "shared-circuit", principal=tenant_b)["state"] == "CLOSED"
    assert service.get_state(
        "shared-circuit", principal=tenant_a)["state"] == "OPEN"
    assert service.list_states(principal=tenant_a)["state_counts"] == {"OPEN": 1}
    assert service.list_states(principal=tenant_b)["state_counts"] == {"CLOSED": 1}


def test_formal_skill_grant_cannot_bypass_rbac_or_change_tenant(tmp_path):
    class Attribution:
        def chain(self, decision_id, *, principal=None):
            return {
                "decision_id": decision_id,
                "tenant_id": principal.tenant_id,
                "authoritative_actual_channel": None,
                "network_called": False,
            }

    auth, resolver = security()
    bindings = FormalAgentSkillBindings.from_services(
        attribution=Attribution(), authorization_service=auth)
    runtime = FormalAgentSkillRuntime.build(
        bindings=bindings,
        audit=FormalAgentSkillAuditService(tmp_path / "skills.sqlite3"))
    with pytest.raises(AuthorizationError, match="missing_or_unverified"):
        runtime.invoke_from_agent(
            skill_id="explain-scheduler-decision", arguments={"decision_id": "D1"},
            decision_id="OUTER")

    read_only = principal(
        resolver, tenant="tenant-b", role="monitoring_service")
    result = runtime.invoke_from_agent(
        skill_id="explain-scheduler-decision", arguments={"decision_id": "D1"},
        decision_id="OUTER", actor_id="security_engineering_owner",
        principal=read_only)
    assert result["data"]["tenant_id"] == "tenant-b"

    scheduler_identity = principal(resolver)
    with pytest.raises(AuthorizationError, match="permission_denied"):
        runtime.invoke_from_agent(
            skill_id="explain-scheduler-decision", arguments={"decision_id": "D2"},
            decision_id="OUTER-2", principal=scheduler_identity)


def test_governance_services_reject_direct_calls_and_actor_role_spoofing(tmp_path):
    auth, _ = security()
    traffic = TrafficChangeGovernanceService(
        tmp_path / "traffic.sqlite3",
        ROOT / "config/traffic_change_governance_policy_v1.json",
        authorization_service=auth)
    probe = ProbeGovernanceService(
        tmp_path / "probe.sqlite3", ROOT / "config/probe_governance_policy_v1.json",
        authorization_service=auth)
    high_cost = HighCostTestGovernanceService(
        tmp_path / "cost.sqlite3",
        ROOT / "config/high_cost_test_governance_policy_v1.json",
        authorization_service=auth)
    capability = CapabilityEvidenceService(
        tmp_path / "capability.sqlite3",
        ROOT / "config/capability_evidence_policy_v1.json",
        authorization_service=auth)

    with pytest.raises(AuthorizationError, match="missing_or_unverified"):
        traffic.propose(
            environment_id="china_uat", channel_id="M001", model_id="model",
            change={"weight": 0.01}, proposer_id="forged-admin",
            actor_role="proposer", rollout_percent=1, ttl_seconds=60,
            idempotency_key="forged-traffic")
    with pytest.raises(AuthorizationError, match="missing_or_unverified"):
        probe.set_kill_switch(
            scope_type="global", scope_id="*", active=True,
            reason="forged-emergency")
    with pytest.raises(AuthorizationError, match="missing_or_unverified"):
        high_cost.propose(
            environment_id="china_uat", model_id="model", currency="CNY",
            estimated_cost=1, proposer_id="forged-admin", actor_role="proposer",
            ttl_seconds=60, idempotency_key="forged-cost")
    with pytest.raises(AuthorizationError, match="missing_or_unverified"):
        capability.list_evidence()


def test_governance_sql_scope_blocks_cross_tenant_read_kill_and_idempotency(tmp_path):
    auth, resolver = security()
    ops_a = principal(resolver, tenant="tenant-a", role="operations_admin",
                      kind=PrincipalType.HUMAN)
    ops_b = principal(resolver, tenant="tenant-b", role="operations_admin",
                      kind=PrincipalType.HUMAN)
    owner_b = principal(resolver, tenant="tenant-b",
                        role="security_engineering_owner", kind=PrincipalType.HUMAN)

    traffic = TrafficChangeGovernanceService(
        tmp_path / "scoped-traffic.sqlite3",
        ROOT / "config/traffic_change_governance_policy_v1.json",
        authorization_service=auth)
    common = dict(
        environment_id="china_uat", channel_id="M001", model_id="model-v1",
        change={"weight": 0.01}, proposer_id="operator", actor_role="proposer",
        rollout_percent=1, ttl_seconds=60, idempotency_key="same-external-key")
    first = traffic.propose(**common, principal=ops_a)
    second = traffic.propose(**common, principal=ops_b)
    assert first["proposal_id"] != second["proposal_id"]
    with pytest.raises(TrafficChangeGovernanceError, match="proposal_not_found"):
        traffic.approve(
            first["proposal_id"], approver_id="other", actor_role="approver",
            expected_revision=0, idempotency_key="cross-tenant-write",
            principal=ops_b)

    capability = CapabilityEvidenceService(
        tmp_path / "scoped-capability.sqlite3",
        ROOT / "config/capability_evidence_policy_v1.json",
        authorization_service=auth)
    evidence = {
        "evidence_id": "SHARED-EVIDENCE", "evidence_type": "contract_test",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "environment_id": "china_uat", "subject_id": "model-v1",
        "subject_version": "v1", "source": "local", "requirement": "text",
        "state": "supported",
    }
    capability.record_evidence(evidence, principal=ops_a)
    capability.record_evidence(evidence, principal=ops_b)
    assert len(capability.list_evidence(principal=ops_a)) == 1
    assert len(capability.list_evidence(principal=ops_b)) == 1
    capability.revoke_evidence("SHARED-EVIDENCE", reason="tenant-b-only", principal=ops_b)
    assert capability.list_evidence(principal=ops_a)[0]["revoked_at"] is None

    exploration = ExplorationGovernanceService(
        tmp_path / "scoped-exploration.sqlite3",
        ROOT / "config/exploration_policy_v1.json", environ={},
        authorization_service=auth)
    exploration.set_kill_switch(
        active=True, reason="tenant-a", principal=ops_a)
    exploration.set_kill_switch(
        active=True, reason="tenant-b", principal=ops_b)
    with pytest.raises(AuthorizationError, match="permission_denied"):
        exploration.set_kill_switch(
            active=False, reason="forged-release", principal=ops_b)
    exploration.set_kill_switch(
        active=False, reason="authorized-release", principal=owner_b)
    assert exploration.status(principal=ops_a)["kill_switches"][0]["active"] == 1
    assert exploration.status(principal=ops_b)["kill_switches"][0]["active"] == 0


def test_probe_and_high_cost_scope_ids_leases_kills_and_idempotency(tmp_path):
    auth, resolver = security()
    ops_a = principal(resolver, tenant="tenant-a", role="operations_admin",
                      kind=PrincipalType.HUMAN)
    ops_b = principal(resolver, tenant="tenant-b", role="operations_admin",
                      kind=PrincipalType.HUMAN)
    scheduler_a = principal(resolver, tenant="tenant-a")
    scheduler_b = principal(resolver, tenant="tenant-b")

    probe_policy = json.loads(
        (ROOT / "config/probe_governance_policy_v1.json").read_text())
    probe_policy["enabled"] = True
    probe_policy_path = tmp_path / "probe-policy.json"
    probe_policy_path.write_text(json.dumps(probe_policy), encoding="utf-8")
    probe = ProbeGovernanceService(
        tmp_path / "scoped-probe.sqlite3", probe_policy_path,
        authorization_service=auth)
    approval_args = dict(
        environment_id="china_uat", channel_id="M001", model_id="model-v1",
        approved_by="operator", starts_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ttl_seconds=300, max_requests=10, max_cost=10)
    probe.approve(**approval_args, principal=ops_a)
    probe.approve(**approval_args, principal=ops_b)
    acquire_args = dict(
        idempotency_key="shared-probe-key", run_id="run-1",
        environment_id="china_uat", channel_id="M001", model_id="model-v1",
        estimated_cost=0.1)
    with pytest.raises(PermissionError, match="scheduler_service_identity_required"):
        probe.acquire(**acquire_args, principal=ops_b)
    lease_a = probe.acquire(**acquire_args, principal=scheduler_a)
    lease_b = probe.acquire(**acquire_args, principal=scheduler_b)
    assert lease_a["lease_id"] != lease_b["lease_id"]
    probe.set_kill_switch(
        scope_type="global", scope_id="*", active=True, reason="tenant-a-only",
        principal=ops_a)
    assert probe.get_lease(lease_a["lease_id"], principal=scheduler_a)["state"] == "STOPPED"
    assert probe.get_lease(lease_b["lease_id"], principal=scheduler_b)["state"] == "ACTIVE"
    with pytest.raises(ProbeGovernanceError, match="probe_lease_not_found"):
        probe.get_lease(lease_a["lease_id"], principal=scheduler_b)

    high_cost = HighCostTestGovernanceService(
        tmp_path / "scoped-cost.sqlite3",
        ROOT / "config/high_cost_test_governance_policy_v1.json",
        authorization_service=auth)
    proposal_args = dict(
        environment_id="china_uat", model_id="model-v1", currency="CNY",
        estimated_cost=1, proposer_id="operator", actor_role="proposer",
        ttl_seconds=300, idempotency_key="shared-cost-key")
    cost_a = high_cost.propose(**proposal_args, principal=ops_a)
    cost_b = high_cost.propose(**proposal_args, principal=ops_b)
    assert cost_a["test_id"] != cost_b["test_id"]
    approved_a = high_cost.approve(
        cost_a["test_id"], approver_id="other", actor_role="approver",
        expected_revision=0, principal=ops_a)
    approved_b = high_cost.approve(
        cost_b["test_id"], approver_id="other", actor_role="approver",
        expected_revision=0, principal=ops_b)
    with pytest.raises(PermissionError, match="scheduler_service_identity_required"):
        high_cost.reserve(
            cost_b["test_id"], operator_id="human", actor_role="operator",
            expected_revision=approved_b["revision"], idempotency_key="human-reserve",
            gate_results={"approval": True}, principal=ops_b)
    high_cost.set_kill_switch(
        scope_type="global", scope_id="*", active=True, reason="tenant-a-only",
        principal=ops_a)
    gates = {"approval": True}
    with pytest.raises(HighCostTestGovernanceError, match="kill_switch_active"):
        high_cost.reserve(
            cost_a["test_id"], operator_id="scheduler", actor_role="operator",
            expected_revision=approved_a["revision"], idempotency_key="reserve",
            gate_results=gates, principal=scheduler_a)
    reserved_b = high_cost.reserve(
        cost_b["test_id"], operator_id="scheduler", actor_role="operator",
        expected_revision=approved_b["revision"], idempotency_key="reserve",
        gate_results=gates, principal=scheduler_b)
    assert reserved_b["state"] == "RESERVED"
    with pytest.raises(HighCostTestGovernanceError, match="high_cost_test_not_found"):
        high_cost.get(cost_a["test_id"], principal=ops_b)
