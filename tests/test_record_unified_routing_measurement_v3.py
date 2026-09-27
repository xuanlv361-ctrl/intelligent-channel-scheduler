import csv, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import record_unified_routing_measurement_v3 as m

def setup(tmp_path):
    plan=tmp_path/"plan.csv"; ev=tmp_path/"api.txt"; ev.write_text("redacted")
    with plan.open("w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=["plan_id","campaign_version","session_id","round_id","request_profile_id","requested_model","stream","max_tokens"]); w.writeheader()
        w.writerow(dict(plan_id="UR-V3-001",campaign_version="unified-routing-v3.0.0",session_id="S",round_id="R01",request_profile_id="P01",requested_model="deepseek-v4-flash",stream="FALSE",max_tokens="128"))
    actual=dict(measured_at="2026-07-27T09:01:00+08:00",result="success",http_status="200",total_latency_ms=100,apifox_evidence_path=str(ev))
    return plan,ev,actual

def test_dry_run_first_write_duplicate_and_amend(tmp_path):
    plan,ev,actual=setup(tmp_path); results=tmp_path/"results.csv"; backups=tmp_path/"backups"
    out=m.record_result(plan_path=plan,results_path=results,backup_dir=backups,plan_id="UR-V3-001",actual=actual,dry_run=True)
    assert not out["written"] and not results.exists()
    m.record_result(plan_path=plan,results_path=results,backup_dir=backups,plan_id="UR-V3-001",actual=actual)
    assert results.exists()
    with pytest.raises(m.MeasurementRecordError): m.record_result(plan_path=plan,results_path=results,backup_dir=backups,plan_id="UR-V3-001",actual=actual)
    m.record_result(plan_path=plan,results_path=results,backup_dir=backups,plan_id="UR-V3-001",actual=actual,amend=True)
    assert list(backups.glob("*before_amend*"))

def test_channel_requires_backend_log_and_unknown_stays_blank(tmp_path):
    plan,ev,actual=setup(tmp_path)
    record=m.build_record(m.read_csv(plan)[0],actual,tmp_path); assert not record["actual_channel_id"]
    actual["actual_channel_id"]="19"; actual["actual_channel_evidence_status"]="backend_log_confirmed"
    with pytest.raises(m.MeasurementRecordError): m.build_record(m.read_csv(plan)[0],actual,tmp_path)

def test_non_stream_ttft_rejected(tmp_path):
    plan,ev,actual=setup(tmp_path); actual["ttft_ms"]=1
    with pytest.raises(m.MeasurementRecordError): m.build_record(m.read_csv(plan)[0],actual,tmp_path)
