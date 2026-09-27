from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from exploration_governance_service import (  # noqa: E402
    ExplorationGovernanceError, ExplorationGovernanceService,
)

NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


def service(tmp_path, **patch):
    policy = json.loads((ROOT / "config/exploration_policy_v1.json").read_text())
    policy.update({"allowed_environments": ["prod"], "allowed_channels": ["c1"],
                   "allowed_models": ["m1"],
                   "budgets": {key: {"requests": 2, "cost": 1.0} for key in policy["budgets"]},
                   "stops": {"maximum_failure_rate": .5, "minimum_samples_for_failure_stop": 2,
                             "maximum_latency_ms": 1000, "allowed_health_states": ["healthy"]}, **patch})
    path = tmp_path / "policy.json"; path.write_text(json.dumps(policy))
    return ExplorationGovernanceService(tmp_path / "state.db", path, clock=lambda: NOW,
                                        environ={policy["feature_flag_env"]: "1"})


def health():
    return {"state": "healthy", "samples": 2, "failure_rate": 0, "latency_ms": 100}


def test_disabled_by_default_and_shadow_only(tmp_path):
    policy = ROOT / "config/exploration_policy_v1.json"
    svc = ExplorationGovernanceService(tmp_path / "x.db", policy, clock=lambda: NOW, environ={})
    result = svc.evaluate(run_id="r", environment_id="x", channel_id="x", model_id="x", health=health())
    assert not result["allowed"]
    assert result["real_execution_allowed"] is False
    assert "feature_disabled" in result["reasons"]


def test_approval_determinism_stops_and_persistence(tmp_path):
    svc = service(tmp_path)
    svc.approve(environment_id="prod", channel_id="c1", model_id="m1", approved_by="owner", ttl_seconds=60)
    kwargs = dict(candidates=[{"id": "b", "channel_id": "c1", "trials": 5, "mean_reward": .1},
                              {"id": "a", "channel_id": "c1", "trials": 5, "mean_reward": .9}], request_id="request",
                  run_id="r", environment_id="prod", channel_id="c1", model_id="m1", health=health(),circuit_state="CLOSED")
    assert svc.recommend(**kwargs) == svc.recommend(**kwargs)
    assert svc.recommend(**kwargs)["real_execution_allowed"] is False
    svc.set_kill_switch(active=True, reason="incident", scope_type="channel", scope_id="c1")
    restarted = service(tmp_path)
    assert "kill_switch_active" in restarted.evaluate(run_id="r", environment_id="prod",
        channel_id="c1", model_id="m1", health=health())["reasons"]
    assert restarted.list_audit()


def test_atomic_budget_idempotency_and_expiry(tmp_path):
    svc = service(tmp_path)
    result = svc.consume(idempotency_key="one", run_id="r", environment_id="prod",
                         channel_id="c1", model_id="m1", requests=2, cost=.5)
    assert svc.consume(idempotency_key="one", run_id="r", environment_id="prod",
                       channel_id="c1", model_id="m1", requests=2, cost=.5) == result
    with pytest.raises(ExplorationGovernanceError, match="budget_exhausted"):
        svc.consume(idempotency_key="two", run_id="r", environment_id="prod",
                    channel_id="c1", model_id="m1")
    svc.approve(environment_id="prod", channel_id="c1", model_id="m1", approved_by="x", ttl_seconds=1)
    svc.clock = lambda: NOW + timedelta(seconds=2)
    assert "approval_missing_or_expired" in svc.evaluate(run_id="r2", environment_id="prod",
        channel_id="c1", model_id="m1", health=health())["reasons"]

def test_reserved_decision_checks_alternate_scope_circuit_and_budget(tmp_path):
    svc=service(tmp_path,allowed_channels=["c1","c2"])
    svc.approve(environment_id="prod",channel_id="c1",model_id="m1",approved_by="x",ttl_seconds=60)
    candidates=[{"id":"alt","channel_id":"c2","trials":0,"mean_reward":0}]
    blocked=svc.recommend(candidates=candidates,request_id="q",run_id="r",environment_id="prod",
        channel_id="c1",model_id="m1",health=health(),circuit_state="CLOSED")
    assert blocked["selected"] is None and "selected_candidate:approval_missing_or_expired" in blocked["reasons"]
    svc.approve(environment_id="prod",channel_id="c2",model_id="m1",approved_by="x",ttl_seconds=60)
    kwargs=dict(candidates=candidates,request_id="q",run_id="r",environment_id="prod",
        channel_id="c1",model_id="m1",health=health(),circuit_state="CLOSED")
    decision=svc.reserve_recommendation(idempotency_key="reserve",cost=.25,**kwargs)
    assert decision["selected_channel_id"]=="c2" and decision["execution_authorized"] is False
    assert svc.reserve_recommendation(idempotency_key="reserve",cost=.25,**kwargs)==decision
    assert any(row["event_type"]=="shadow_decision_reserved" for row in svc.list_audit())

def test_circuit_gate_is_fail_closed(tmp_path):
    svc=service(tmp_path);svc.approve(environment_id="prod",channel_id="c1",model_id="m1",approved_by="x",ttl_seconds=60)
    result=svc.evaluate(run_id="r",environment_id="prod",channel_id="c1",model_id="m1",health=health(),circuit_state="OPEN")
    assert not result["allowed"] and "circuit_stop" in result["reasons"]
