import csv, hashlib, json, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import build_unified_routing_campaign_v3 as module

PROTECTED = [
 "config/real_measurement_campaign_v2.json","config/measurement_execution_capabilities_v2.json",
 "data/request_profiles_v2.csv","output/real_measurement_plan_v2.csv",
 "output/real_measurement_payloads_v2.json","output/measurement_execution_readiness_v2.json",
 "src/record_real_measurement_v2.py","src/validate_real_measurement_progress_v2.py"]

def test_generation_is_deterministic_and_distributed(tmp_path):
    root=Path(__file__).resolve().parents[1]; before={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in PROTECTED}
    args=(module.CONFIG_PATH,module.PROFILES_PATH,tmp_path/"plan.csv",tmp_path/"payload.json",tmp_path/"ready.json")
    module.generate(*args); first=[p.read_bytes() for p in args[2:]]; module.generate(*args)
    assert first == [p.read_bytes() for p in args[2:]]
    with args[2].open(encoding="utf-8") as f: plans=list(csv.DictReader(f))
    payloads=json.loads(args[3].read_text(encoding="utf-8"))
    assert len(plans)==len(payloads)==60
    assert [r["plan_id"] for r in plans]==[f"UR-V3-{i:03d}" for i in range(1,61)]
    assert set(Counter(r["session_id"] for r in plans).values())=={20}
    assert set(Counter(r["request_profile_id"] for r in plans).values())=={15}
    assert all(not r["planned_channel_id"] and not r["actual_channel_id"] for r in plans)
    assert all(p["planned_channel_id"] is None and p["stream"] == (p["request_profile_id"]=="P04") for p in payloads)
    assert not any(term in args[3].read_text(encoding="utf-8").lower() for term in ("authorization","api_key","cookie"))
    assert before=={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in PROTECTED}
