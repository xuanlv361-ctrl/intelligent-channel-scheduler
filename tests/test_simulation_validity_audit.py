import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import generate_simulation_workload as workload  # noqa: E402


def test_validity_audit_answers_all_questions_and_records_original_defect():
    audit = json.loads((ROOT / "output" / "simulation_validity_audit_v2.json").read_text(encoding="utf-8"))
    assert len(audit["questions"]) == 10
    assert audit["questions"][0]["finding"] == "defect_confirmed_and_fixed"
    assert audit["future_information_leakage"] is False
    assert audit["circular_reasoning_risk_found"] is True


def test_latent_truth_never_enters_decision_snapshot():
    config = workload.load_json(workload.CONFIG_PATH)
    snapshot = workload.build_candidate_snapshot(workload.load_csv(workload.CANDIDATES_PATH), config["regimes"]["cold_start"])
    assert all("observed_success_rate" in row for row in snapshot)
    assert all("latent_success_probability" not in row for row in snapshot)


def test_all_audit_artifacts_are_mock_bounded():
    audit = json.loads((ROOT / "output" / "simulation_validity_audit_v2.json").read_text(encoding="utf-8"))
    assert audit["is_mock_evaluation"] is True
    assert audit["real_api_calls"] == 0
