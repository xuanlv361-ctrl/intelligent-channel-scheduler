import json
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from concurrent.futures import ThreadPoolExecutor
from backend.probe_governance_service import ProbeGovernanceError,ProbeGovernanceService
ROOT=Path(__file__).resolve().parents[1]; NOW=datetime(2026,8,1,tzinfo=timezone.utc)
def service(tmp_path,clock=lambda:NOW,enabled=True,limits=None):
 p=json.loads((ROOT/'config/probe_governance_policy_v1.json').read_text());p['enabled']=enabled
 if limits:p['limits']=limits
 path=tmp_path/'g.json';path.write_text(json.dumps(p));return ProbeGovernanceService(tmp_path/'g.db',path,clock=clock)
def approve(s,max_requests=10,max_cost=10):return s.approve(environment_id='e',channel_id='c',model_id='m',approved_by='a',starts_at=NOW,ttl_seconds=3600,max_requests=max_requests,max_cost=max_cost)
def acquire(s,key='k',cost=1):return s.acquire(idempotency_key=key,run_id='r',environment_id='e',channel_id='c',model_id='m',estimated_cost=cost)

def test_disabled_live_and_circuit_fail_closed(tmp_path):
 s=service(tmp_path,enabled=False);approve(s)
 with pytest.raises(ProbeGovernanceError,match='disabled'):acquire(s)
 s=service(tmp_path,enabled=True)
 with pytest.raises(ProbeGovernanceError,match='live_probe'):s.acquire(idempotency_key='x',run_id='r',environment_id='e',channel_id='c',model_id='m',estimated_cost=0,transport='live')
 with pytest.raises(ProbeGovernanceError,match='circuit'):s.acquire(idempotency_key='y',run_id='r',environment_id='e',channel_id='c',model_id='m',estimated_cost=0,circuit_state='OPEN')

def test_approval_quota_concurrency_idempotency_and_completion(tmp_path):
 s=service(tmp_path);approve(s,max_requests=1,max_cost=1); one=acquire(s);assert acquire(s)==one
 with pytest.raises(ProbeGovernanceError,match='approval_quota'):acquire(s,'two')
 assert s.complete(one['lease_id'],success=True)['state']=='SUCCEEDED'

def test_failure_backoff_stop_kill_and_restart(tmp_path):
 limits={x:{'requests':100,'cost':100,'rate_per_minute':100,'burst':100,'concurrency':10} for x in ('global','environment','channel','model')}
 clock=[NOW];s=service(tmp_path,clock=lambda:clock[0],limits=limits);approve(s,max_requests=20,max_cost=20)
 first=acquire(s,'f1');s.complete(first['lease_id'],success=False)
 with pytest.raises(ProbeGovernanceError,match='backoff'):acquire(s,'f2')
 for index in (2,3):
  clock[0]+=timedelta(seconds=400); lease=acquire(s,f'f{index}');result=s.complete(lease['lease_id'],success=False)
 assert result['stop_triggered']
 s.set_kill_switch(scope_type='global',scope_id='*',active=True,reason='incident')
 assert s.audit()
 clock[0]+=timedelta(seconds=400); restarted=service(tmp_path,clock=lambda:clock[0],limits=limits)
 with pytest.raises(ProbeGovernanceError,match='kill_switch|stop_condition'):acquire(restarted,'later')

def test_concurrent_probe_lease_respects_scope_concurrency(tmp_path):
 limits={x:{'requests':10,'cost':10,'rate_per_minute':10,'burst':10,'concurrency':1} for x in ('global','environment','channel','model')}
 s=service(tmp_path,limits=limits);approve(s)
 def attempt(key):
  try:return acquire(s,key)['state']
  except ProbeGovernanceError:return 'rejected'
 with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,('a','b')))
 assert results.count('ACTIVE')==1
