import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import generate_simulation_workload as workload  # noqa: E402


def test_mock_catalog_has_eight_complete_candidates():
    with (ROOT / "data" / "mock_channel_catalog_v3.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len({r["candidate_id"] for r in rows}) == 8
    required = {"candidate_id", "channel_name", "requested_model", "latency_ms", "input_price_per_1m",
                "output_price_per_1m", "success_probability_prior", "confidence_level", "supports_text",
                "supports_stream", "availability_status", "priority", "is_mock", "data_source"}
    assert required <= set(rows[0])
    assert all(r["is_mock"] == r["is_mock_evaluation"] == "TRUE" and r["data_source"] == "offline_simulation_v3" for r in rows)
    assert all(r["parameter_type"] == "simulation_parameter" for r in rows)


def test_v3_workload_shape_shared_outcomes_and_reproducibility():
    first = workload.generate(); second = workload.generate()
    assert first == second
    requests, outcomes = first
    assert len(requests) == len({r["request_id"] for r in requests}) == 5000
    assert len(outcomes) == 40000
    assert set(Counter(r["request_id"] for r in outcomes).values()) == {8}
    assert len({r["candidate_id"] for r in outcomes}) == 8


def test_hidden_future_fields_never_enter_candidate_snapshot():
    config = workload.load_json(workload.CONFIG_PATH)
    catalog = workload.load_csv(workload.CANDIDATES_PATH)
    for regime in config["regimes"].values():
        snapshot = workload.build_candidate_snapshot(catalog, regime)
        assert all("latent_success_probability" not in row and "realized_success" not in row
                   and "realized_latency_ms" not in row and "realized_cost" not in row for row in snapshot)


def test_candidate_failure_is_configured_mock_failure():
    config = workload.load_json(workload.CONFIG_PATH)
    regime = config["regimes"]["candidate_failure"]
    override = regime["candidate_overrides"]["MOCK-GPT-STABLE"]
    assert regime["simulation_parameter"] is True
    assert override["availability_probability"] == 0.0
