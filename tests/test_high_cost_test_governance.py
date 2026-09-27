import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from concurrent.futures import ThreadPoolExecutor
from backend.high_cost_test_governance_service import HighCostTestGovernanceError,HighCostTestGovernanceService
ROOT=Path(__file__).resolve().parents[1];NOW=datetime(2026,8,1,tzinfo=timezone.utc)
def service(tmp_path,clock=lambda:NOW,enabled=True,limits=None):
 p=json.loads((ROOT/'config/high_cost_test_governance_policy_v1.json').read_text());p['enabled']=enabled
 if limits:p['limits']=limits
 path=tmp_path/'h.json';path.write_text(json.dumps(p));return HighCostTestGovernanceService(tmp_path/'h.db',path,clock=clock)
def propose(s,key='p',cost=10,currency='CNY'):return s.propose(environment_id='e',model_id='m',currency=currency,estimated_cost=cost,proposer_id='p',actor_role='proposer',ttl_seconds=60,idempotency_key=key)
def approve(s,p):return s.approve(p['test_id'],approver_id='a',actor_role='approver',expected_revision=0)

def test_unknown_currency_separation_reservation_and_reconciliation(tmp_path):
 s=service(tmp_path)
 with pytest.raises(HighCostTestGovernanceError,match='unknown_currency'):propose(s,currency='XYZ')
 p=propose(s);assert propose(s)==p
 with pytest.raises(HighCostTestGovernanceError,match='separation'):s.approve(p['test_id'],approver_id='p',actor_role='approver',expected_revision=0)
 a=approve(s,p);r=s.reserve(p['test_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],idempotency_key='r',gate_results={'budget':True,'circuit':True})
 running=s.start_mock(p['test_id'],operator_id='o',actor_role='operator',expected_revision=r['revision'])
 done=s.reconcile(p['test_id'],operator_id='o',actor_role='operator',expected_revision=running['revision'],actual_cost=8,currency='CNY')
 assert done['variance']==-2 and done['real_execution_allowed'] is False and len(s.get(p['test_id'])['approvals'])==1 and s.list_audit(p['test_id'])

def test_atomic_scope_budget_kill_and_expiry_recovery(tmp_path):
 limits={x:{'CNY':15,'USD':15} for x in ('global','environment','model')};clock=[NOW]
 s=service(tmp_path,clock=lambda:clock[0],limits=limits);p=propose(s);a=approve(s,p);r=s.reserve(p['test_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],idempotency_key='r',gate_results={'budget':True})
 p2=propose(s,key='p2',cost=10);a2=approve(s,p2)
 with pytest.raises(HighCostTestGovernanceError,match='budget_exhausted'):s.reserve(p2['test_id'],operator_id='o',actor_role='operator',expected_revision=a2['revision'],idempotency_key='r2',gate_results={'budget':True})
 s.set_kill_switch(scope_type='environment',scope_id='e',active=True,reason='incident');assert s.get(p['test_id'])['state']=='EMERGENCY_STOPPED'
 clock[0]+=timedelta(seconds=61);restarted=service(tmp_path,clock=lambda:clock[0],limits=limits);assert restarted.get(p2['test_id'])['state']=='EXPIRED'

def test_default_policy_blocks_start_and_cancel_releases(tmp_path):
 s=service(tmp_path,enabled=False);p=propose(s);a=approve(s,p);r=s.reserve(p['test_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],idempotency_key='r',gate_results={'budget':True})
 with pytest.raises(HighCostTestGovernanceError,match='disabled'):s.start_mock(p['test_id'],operator_id='o',actor_role='operator',expected_revision=r['revision'])
 assert s.terminate(p['test_id'],actor_id='o',actor_role='operator',expected_revision=r['revision'],reason='cancel')['reservation_released']

def test_concurrent_reservations_cannot_overspend(tmp_path):
 limits={x:{'CNY':15,'USD':15} for x in ('global','environment','model')};s=service(tmp_path,limits=limits)
 items=[]
 for key in ('a','b'):
  p=propose(s,key=key,cost=10);items.append((p,approve(s,p)))
 def attempt(item):
  p,a=item
  try:return s.reserve(p['test_id'],operator_id='o',actor_role='operator',expected_revision=a['revision'],idempotency_key='r'+p['test_id'],gate_results={'budget':True})['state']
  except HighCostTestGovernanceError:return 'rejected'
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,items))
 assert results.count('RESERVED')==1
