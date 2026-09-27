import csv
import hashlib
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import generate_simulation_workload as workload  # noqa: E402


def test_request_and_outcome_counts():
    requests, outcomes = workload.generate()
    assert len(requests) == 5000 and len(outcomes) == 40000


def test_unique_requests_and_regime_distribution():
    requests, _ = workload.generate()
    assert len({r["request_id"] for r in requests}) == 5000
    assert Counter(r["environment_regime"] for r in requests) == {"normal": 834, "peak_load": 834, "price_spike": 833, "channel_degradation": 833, "cold_start": 833, "candidate_failure": 833}


def test_three_potential_outcomes_per_request():
    _, outcomes = workload.generate()
    assert set(Counter(r["request_id"] for r in outcomes).values()) == {8}


def test_generation_is_reproducible():
    assert workload.generate() == workload.generate()


def test_requests_have_nonconstant_distributions():
    requests, _ = workload.generate()
    assert len({r["input_tokens"] for r in requests}) > 100
    assert len({r["output_tokens"] for r in requests}) > 100
    assert {r["stream_required"] for r in requests} == {"TRUE", "FALSE"}
    assert len({r["budget_cny"] for r in requests}) > 100
    assert len({r["timeout_ms"] for r in requests}) > 100


def test_potential_outcome_columns_and_markers():
    _, outcomes = workload.generate()
    assert set(workload.OUTCOME_COLUMNS) == set(outcomes[0])
    assert all(r["is_mock"] == r["is_mock_evaluation"] == "TRUE" and r["data_source"] == "offline_simulation_v3" for r in outcomes)
    assert all(0 <= r["latent_success_probability"] <= 1 for r in outcomes)


def test_observed_and_hidden_success_are_separated():
    config = workload.load_json(workload.CONFIG_PATH)
    base = workload.load_csv(workload.CANDIDATES_PATH)
    snapshot = workload.build_candidate_snapshot(base, config["regimes"]["normal"])
    real = next(r for r in snapshot if r["candidate_id"] == "REAL-DS-48")
    assert real["observed_success_rate"] == "1.0"
    assert "latent_success_probability" not in real
    _, outcomes = workload.generate(config, base)
    real_outcomes = [r for r in outcomes if r["candidate_id"] == "REAL-DS-48"]
    assert {r["latent_success_probability"] for r in real_outcomes} != {1.0}


def test_requests_marked_mock():
    requests, _ = workload.generate()
    assert all(r["is_mock"] == r["is_mock_evaluation"] == "TRUE" and r["data_source"] == "offline_simulation_v3" for r in requests)


def test_no_negative_cost_or_latency():
    _, outcomes = workload.generate()
    assert all(r["realized_cost"] >= 0 and r["realized_latency_ms"] > 0 for r in outcomes)


def test_dry_run_does_not_write(tmp_path):
    workload.build(tmp_path, dry_run=True)
    assert not list(tmp_path.iterdir())


def test_custom_output_structure(tmp_path):
    workload.build(tmp_path)
    with (tmp_path / workload.REQUESTS_NAME).open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f); requests = list(reader)
        assert reader.fieldnames == workload.REQUEST_COLUMNS
    with (tmp_path / workload.OUTCOMES_NAME).open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f); outcomes = list(reader)
        assert reader.fieldnames == workload.OUTCOME_COLUMNS
    assert len(requests) == 5000 and len(outcomes) == 40000


def test_protected_inputs_unchanged(tmp_path):
    paths = [workload.CONFIG_PATH, workload.CANDIDATES_PATH, ROOT / "config" / "decision_policy_v1.json"]
    digest = lambda p: hashlib.sha256(p.read_bytes()).digest()
    before = {p: digest(p) for p in paths}
    workload.build(tmp_path)
    assert before == {p: digest(p) for p in paths}
