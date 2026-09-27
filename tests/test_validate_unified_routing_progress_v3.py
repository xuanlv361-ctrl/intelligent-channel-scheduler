import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from validate_unified_routing_progress_v3 import build_progress

def test_empty_progress_and_unequal_routes_allowed():
    plans=[{"plan_id":f"P{i}","session_id":"S","request_profile_id":"P01","round_id":"R01"} for i in range(3)]
    report=build_progress(plans,[])
    assert report["completed"]==0 and report["pending"]==3
    assert report["equal_channel_counts_required"] is False

def test_progress_reports_missing_and_routing(tmp_path):
    ev=tmp_path/"a"; ev.write_text("x")
    plans=[{"plan_id":"P1","session_id":"S","request_profile_id":"P04","round_id":"R01"}]
    rows=[{"plan_id":"P1","measurement_id":"M1","actual_channel_id":"19","actual_channel_name":"",
      "result":"success","apifox_evidence_path":str(ev),"backend_log_evidence_path":"",
      "http_status":"","actual_model":"","request_id":"","ttft_ms":"","sse_complete":"FALSE","done_received":"FALSE"}]
    r=build_progress(plans,rows,tmp_path,True)
    assert r["completed"]==1 and r["actual_observed_routing_counts_by_channel"]=={"19":1}
    assert r["missing_ttft_for_p04"]==1 and r["missing_backend_channel_evidence"]==1
