from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from backend.dynamic_metrics_service import DynamicMetricsService
from backend.tenant_security import TenantScope


SCOPE = TenantScope("tenant_local_dev_v1", "workspace_local_dev_v1")


def prepare(path):
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE standardized_call_logs(
          cursor_id INTEGER PRIMARY KEY,record_id TEXT,occurred_at TEXT,request_id TEXT,
          environment_id TEXT,requested_model TEXT,actual_model TEXT,channel_id TEXT,
          channel_name TEXT,endpoint_type TEXT,stream INTEGER,request_status TEXT,
          http_status INTEGER,error_category TEXT,total_latency_ms REAL,
          first_token_latency_ms REAL,input_tokens INTEGER,cached_input_tokens INTEGER,
          output_tokens INTEGER,cost_amount TEXT,currency TEXT,configuration_version TEXT,
          source_type TEXT,duplicate_of TEXT,strategy_variant TEXT,cost_source TEXT,
          cost_type TEXT,provider_cost_amount_exact TEXT,cost_status TEXT,
          fallback_used INTEGER,traffic_class TEXT,tenant_id TEXT,workspace_id TEXT
        );
        CREATE TABLE metric_snapshots(
          snapshot_id TEXT,model TEXT,channel TEXT,data_state TEXT,sample_count INTEGER,
          last_evidence_at TEXT,evidence_age_seconds REAL,metrics_json TEXT,window_name TEXT,
          environment_id TEXT,tenant_id TEXT,workspace_id TEXT
        );
        """)
    return DynamicMetricsService(path, SCOPE)


def insert(path, *, cursor, at, model="model-a", traffic="business", status="success"):
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO standardized_call_logs VALUES(
          ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
          cursor,f"LOG-{cursor}",at,f"REQ-{cursor}","china_uat",model,model,None,None,
          "chat",0,status,200 if status=="success" else 500,None,100+cursor,20,
          10,2,5,"0.01","CNY","cfg-v1","realtime_execution",None,"baseline",
          "provider_actual","provider_actual","0.01","provider_actual",0,traffic,
          SCOPE.tenant_id,SCOPE.workspace_id))


def test_rolling_windows_and_timezone_normalization(tmp_path):
    path=tmp_path/"metrics.sqlite3"; service=prepare(path); now=datetime.now(timezone.utc)
    insert(path,cursor=1,at=(now-timedelta(minutes=2)).isoformat())
    insert(path,cursor=2,at=(now-timedelta(minutes=30)).astimezone(timezone(timedelta(hours=8))).isoformat())
    insert(path,cursor=3,at=(now-timedelta(hours=2)).replace(tzinfo=None).isoformat())
    assert service.overview(window="5m")["kpis"]["request_count"] == 1
    assert service.overview(window="1h")["kpis"]["request_count"] == 2
    assert service.overview(window="24h")["kpis"]["request_count"] == 3


def test_business_probe_isolation_and_real_aggregates(tmp_path):
    path=tmp_path/"metrics.sqlite3"; service=prepare(path); now=datetime.now(timezone.utc)
    for cursor in range(1,7): insert(path,cursor=cursor,at=(now-timedelta(minutes=cursor)).isoformat())
    insert(path,cursor=7,at=now.isoformat(),traffic="probe",status="failed")
    business=service.overview(window="1h",traffic_class="business")
    probe=service.overview(window="1h",traffic_class="probe")
    assert business["kpis"]["request_count"] == 6
    assert business["kpis"]["success_rate"] == 1
    assert business["kpis"]["total_tokens"] == 102
    assert business["kpis"]["actual_cost"] == .06
    assert business["snapshot_status"]["healthy"] == 1
    assert probe["kpis"]["request_count"] == 1
    assert probe["kpis"]["success_rate"] == 0


def test_ensure_is_persisted_and_idempotent_state_is_queryable(tmp_path):
    path=tmp_path/"metrics.sqlite3"; service=prepare(path); now=datetime.now(timezone.utc)
    insert(path,cursor=1,at=now.isoformat())
    result=service.ensure()
    assert result["status"] == "ready"
    job=service.job(result["job_id"])
    assert job and job["event_count"] == 1 and job["status"] == "ready"
    restarted=DynamicMetricsService(path,SCOPE)
    assert restarted.job(result["job_id"])["source_watermark"] == now.isoformat()
