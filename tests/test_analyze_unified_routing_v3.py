import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analyze_unified_routing_v3 import analyze, generate

def row(plan,channel,profile="P01"):
    return {"plan_id":plan,"source_type":"measured_unified_uat","is_mock":"FALSE","actual_channel_id":channel,
      "actual_channel_name":"","request_profile_id":profile,"session_id":"S","round_id":"R01","result":"success",
      "total_latency_ms":"100","cost_cny":"0.1","ttft_ms":"20" if profile=="P04" else "",
      "sse_complete":"TRUE" if profile=="P04" else "","done_received":"TRUE" if profile=="P04" else "",
      "http_status":"200","actual_model":"m","request_id":"r"}

def test_requires_genuine_results():
    with pytest.raises(ValueError): analyze([])

def test_descriptive_metrics_and_outputs(tmp_path):
    rows=[row("UR-V3-001","19"),row("UR-V3-002","19"),row("UR-V3-003","27","P04")]
    s=analyze(rows)
    assert s["maximum_selected_channel_share"]==pytest.approx(2/3)
    assert s["herfindahl_hirschman_index"]==pytest.approx(5/9)
    assert s["causal_channel_superiority_inference_allowed"] is False
