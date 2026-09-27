import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from concurrent.futures import ThreadPoolExecutor
from backend.traffic_change_governance_service import TrafficChangeGovernanceError, TrafficChangeGovernanceService

ROOT=Path(__file__).resolve().parents[1]; NOW=datetime(2026,8,1,tzinfo=timezone.utc)
def service(tmp_path, clock=lambda:NOW, **overrides):
    p=json.loads((ROOT/'config/traffic_change_governance_policy_v1.json').read_text()); p.update({'enabled':True,**overrides})
    path=tmp_path/'f.json'; path.write_text(json.dumps(p)); return TrafficChangeGovernanceService(tmp_path/'f.db',path,clock=clock)
def proposal(s): return s.propose(environment_id='e',channel_id='c',model_id='m',change={'weight':.1},proposer_id='p',actor_role='proposer',rollout_percent=5,ttl_seconds=60,idempotency_key='new')

def test_rbac_separation_occ_idempotency_and_sandbox_lifecycle(tmp_path):
    s=service(tmp_path); p=proposal(s); assert proposal(s)==p
    with pytest.raises(TrafficChangeGovernanceError,match='separation'): s.approve(p['proposal_id'],approver_id='p',actor_role='approver',expected_revision=0,idempotency_key='a0')
    a=s.approve(p['proposal_id'],approver_id='a',actor_role='approver',expected_revision=0,idempotency_key='a1')
    with pytest.raises(TrafficChangeGovernanceError,match='revision_conflict'): s.activate(p['proposal_id'],operator_id='o',actor_role='operator',expected_revision=0,gate_results={x:True for x in s.policy['required_gates']},idempotency_key='x')
    active=s.activate(p['proposal_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],gate_results={x:True for x in s.policy['required_gates']},idempotency_key='go')
    assert active['state']=='ACTIVE' and active['real_execution_allowed'] is False and active['adapter']=='offline_sandbox'
    rolled=s.terminate(p['proposal_id'],actor_id='breakglass',actor_role='emergency',expected_revision=active['revision'],reason='incident',emergency=True)
    assert rolled['state']=='ROLLED_BACK'; assert len(s.get(p['proposal_id'])['approvals'])==1; assert s.list_audit(p['proposal_id'])

def test_gates_kill_switch_expiry_and_restart_recovery(tmp_path):
    s=service(tmp_path); p=proposal(s); a=s.approve(p['proposal_id'],approver_id='a',actor_role='approver',expected_revision=0,idempotency_key='a')
    bad={x:True for x in s.policy['required_gates']}; bad['health']=False
    with pytest.raises(TrafficChangeGovernanceError,match='gate_failed'): s.activate(p['proposal_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],gate_results=bad,idempotency_key='bad')
    s.set_kill_switch(scope_type='environment',scope_id='e',active=True,reason='stop',actor_id='x',actor_role='emergency')
    with pytest.raises(TrafficChangeGovernanceError,match='kill_switch'): s.activate(p['proposal_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],gate_results={x:True for x in s.policy['required_gates']},idempotency_key='blocked')
    s.set_kill_switch(scope_type='environment',scope_id='e',active=False,reason='clear',actor_id='x',actor_role='emergency')
    active=s.activate(p['proposal_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],gate_results={x:True for x in s.policy['required_gates']},idempotency_key='ok')
    later=lambda:NOW+timedelta(seconds=61); restarted=service(tmp_path,clock=later)
    assert restarted.get(p['proposal_id'])['state']=='ROLLED_BACK'

def test_default_policy_allows_only_offline_sandbox_activation(tmp_path):
    path=ROOT/'config/traffic_change_governance_policy_v1.json'; s=TrafficChangeGovernanceService(tmp_path/'x.db',path,clock=lambda:NOW)
    p=proposal(s); a=s.approve(p['proposal_id'],approver_id='a',actor_role='approver',expected_revision=0,idempotency_key='a')
    active=s.activate(p['proposal_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],gate_results={x:True for x in s.policy['required_gates']},idempotency_key='x')
    assert active['state']=='ACTIVE' and active['real_execution_allowed'] is False
    assert active['adapter']=='offline_sandbox'

def test_concurrent_approval_has_single_occ_winner(tmp_path):
    s=service(tmp_path);p=proposal(s)
    def attempt(index):
        try:return s.approve(p['proposal_id'],approver_id=f'a{index}',actor_role='approver',expected_revision=0,idempotency_key=f'a{index}')['state']
        except TrafficChangeGovernanceError:return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,(1,2)))
    assert results.count('APPROVED')==1 and len(s.get(p['proposal_id'])['approvals'])==1
