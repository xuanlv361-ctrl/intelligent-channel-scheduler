import copy
import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import strategy_engine as engine  # noqa: E402


@pytest.fixture
def catalog():
    return engine.load_json(engine.CATALOG_PATH)


@pytest.fixture
def policy():
    return engine.load_json(engine.POLICY_PATH)


@pytest.fixture
def sample():
    return engine.sample_request_and_candidates()


def decide(sample, name, **kwargs):
    request, candidates = sample
    return engine.strategy_decision(**request, candidates=candidates, strategy_name=name, **kwargs)


def test_first_class_and_legacy_strategies_registered(catalog):
    assert catalog["supported_strategies"] == [
        "latency_first", "cost_first",
        "random_baseline", "round_robin", "fastest_first", "cheapest_first",
        "reliability_first", "weighted_v1", "confidence_aware_v2",
        "confidence_aware_v3",
    ]
    for name in ("latency_first", "cost_first"):
        definition = catalog["strategies"][name]
        assert definition["type"] == "versioned_weighted_score"
        assert definition["policy_strategy_id"] == name
        assert definition["legacy_alias"] is False
        assert definition["configuration_source"] == "config/decision_policy_v2.json"


def test_experimental_weight_metadata(catalog):
    config = catalog["strategies"]["confidence_aware_v2"]
    assert config["weight_set_id"] == "experimental_default_001"
    assert config["weight_status"] == "experimental_default"
    assert config["weight_source"] == "manual_hypothesis"
    assert config["optimization_status"] == "not_tuned"
    assert config["validation_scope"] == "offline_simulation_only"


def test_catalog_and_policy_versions_are_distinct_and_present(sample):
    result = decide(sample, "confidence_aware_v2")
    assert result["catalog_version"] == "v3.0.0"
    assert result["policy_version"] == "decision-policy-v2.0.0"
    required = {"experiment_id", "run_id", "strategy_definition_version", "weight_set_id", "candidate_snapshot_version", "simulation_config_version", "generated_at", "is_mock_evaluation", "data_source"}
    assert required.issubset(result)


def test_unknown_strategy_errors(sample):
    with pytest.raises(ValueError, match="unknown strategy"):
        decide(sample, "missing")


@pytest.mark.parametrize("name", ["latency_first", "cost_first", "random_baseline", "round_robin", "fastest_first", "cheapest_first", "reliability_first", "weighted_v1", "confidence_aware_v2"])
def test_every_strategy_filters_ineligible_candidates(sample, name):
    request, candidates = sample
    rows = copy.deepcopy(candidates)
    rows[0]["availability_status"] = "unavailable"
    result = engine.strategy_decision(**request, candidates=rows, strategy_name=name)
    assert "REAL-DS-48" not in [r["candidate_id"] for r in result["ranked_candidates"]]
    assert result["excluded_candidates"][0]["exclusion_reason"] == "availability_unavailable"


def test_confidence_v3_requires_versioned_ready_score_and_preserves_v2(sample):
    request, candidates = sample
    catalog_v3 = engine.load_json(ROOT / "config" / "strategy_catalog_v3.json")
    blocked = engine.strategy_decision(
        **request, candidates=candidates, strategy_name="confidence_aware_v3",
        catalog=catalog_v3,
    )
    assert blocked["selected_candidate"] is None
    assert {
        row["exclusion_reason"] for row in blocked["excluded_candidates"]
    } == {"statistical_confidence_not_ready"}
    ready = copy.deepcopy(candidates)
    for index, row in enumerate(ready):
        row.update(
            statistical_confidence_state="ready",
            statistical_confidence_version="weighted_wilson_v1",
            confidence_effective_sample_size="20",
            confidence_adjusted_success_score=str(0.9 - index * 0.05),
        )
    v2_before = decide(sample, "confidence_aware_v2")
    v3 = engine.strategy_decision(
        **request, candidates=ready, strategy_name="confidence_aware_v3",
        catalog=catalog_v3,
    )
    v2_after = decide(sample, "confidence_aware_v2")
    assert v2_before == v2_after
    selected = v3["ranked_candidates"][0]
    weights = catalog_v3["strategies"][
        "confidence_aware_v3"
    ]["weights"]
    assert selected["final_score"] == pytest.approx(
        selected["latency_norm"] * weights["latency"]
        + selected["cost_norm"] * weights["cost"]
        + selected["confidence_adjusted_failure_risk"] * weights["failure_risk"]
    )


def test_expected_fields_do_not_affect_selection(sample):
    baseline = decide(sample, "fastest_first")["selected_candidate"]
    request, candidates = sample
    tampered = copy.deepcopy(candidates)
    for row in tampered:
        row.update(expected_selected="TRUE", expected_eligible="FALSE", expected_exclusion_reason="fake")
    assert engine.strategy_decision(**request, candidates=tampered, strategy_name="fastest_first")["selected_candidate"] == baseline


def test_random_same_seed_is_reproducible(sample):
    assert decide(sample, "random_baseline", random_seed=42) == decide(sample, "random_baseline", random_seed=42)


def test_random_records_seed(sample):
    assert decide(sample, "random_baseline", random_seed=42)["random_seed"] == 42


def test_random_never_selects_excluded(sample):
    request, candidates = sample
    rows = copy.deepcopy(candidates)
    rows[1]["requested_model"] = "other"
    for seed in range(10):
        result = engine.strategy_decision(**request, candidates=rows, strategy_name="random_baseline", random_seed=seed)
        assert result["selected_candidate"] != "MOCK-DS-FAST"


@pytest.mark.parametrize("index,expected", [(0, "REAL-DS-48"), (1, "MOCK-DS-FAST"), (2, "MOCK-DS-CHEAP"), (3, "REAL-DS-48")])
def test_round_robin_cycle(sample, index, expected):
    assert decide(sample, "round_robin", request_index=index)["selected_candidate"] == expected


def test_round_robin_unroutable(sample):
    request, candidates = sample
    rows = copy.deepcopy(candidates)
    for row in rows:
        row["availability_status"] = "unavailable"
    result = engine.strategy_decision(**request, candidates=rows, strategy_name="round_robin")
    assert result["outcome"] == "unroutable" and result["selected_candidate"] is None


def test_fastest_selects_minimum_latency(sample):
    assert decide(sample, "fastest_first")["selected_candidate"] == "MOCK-DS-FAST"


def test_cheapest_cost_calculation(sample):
    result = decide(sample, "cheapest_first")
    costs = {row["candidate_id"]: row["estimated_cost"] for row in result["ranked_candidates"]}
    assert costs == pytest.approx({"REAL-DS-48": 0.002, "MOCK-DS-FAST": 0.003, "MOCK-DS-CHEAP": 0.001})


def test_cheapest_selects_minimum_cost(sample):
    assert decide(sample, "cheapest_first")["selected_candidate"] == "MOCK-DS-CHEAP"


def test_latency_first_is_weighted_strategy_not_fastest_alias(sample):
    request, candidates = sample
    rows = copy.deepcopy(candidates[1:])
    rows[0].update(latency_ms="300", success_rate="0.0", sample_size="100", confidence_level="medium")
    rows[1].update(latency_ms="600", success_rate="1.0", sample_size="100", confidence_level="medium")
    fastest = engine.strategy_decision(
        **request, candidates=rows, strategy_name="fastest_first")
    weighted = engine.strategy_decision(
        **request, candidates=rows, strategy_name="latency_first")
    assert fastest["selected_candidate"] == rows[0]["candidate_id"]
    assert weighted["selected_candidate"] == rows[1]["candidate_id"]
    assert all(item["final_score"] is not None for item in weighted["ranked_candidates"])


def test_cost_first_is_weighted_strategy_not_cheapest_alias(sample):
    request, candidates = sample
    rows = copy.deepcopy(candidates[1:])
    rows[0].update(input_price_per_1m="0", output_price_per_1m="0", success_rate="0.0")
    rows[1].update(input_price_per_1m="0.5", output_price_per_1m="1", success_rate="1.0")
    cheapest = engine.strategy_decision(
        **request, candidates=rows, strategy_name="cheapest_first")
    weighted = engine.strategy_decision(
        **request, candidates=rows, strategy_name="cost_first")
    assert cheapest["selected_candidate"] == rows[0]["candidate_id"]
    assert weighted["selected_candidate"] == rows[1]["candidate_id"]
    assert all(item["final_score"] is not None for item in weighted["ranked_candidates"])


def test_reliability_selects_lowest_raw_risk(sample):
    assert decide(sample, "reliability_first")["selected_candidate"] == "REAL-DS-48"


def test_reliability_prefers_larger_sample_on_equal_risk(sample):
    request, candidates = sample
    rows = copy.deepcopy(candidates[:2])
    rows[0]["success_rate"], rows[1]["success_rate"] = "0.97", "0.97"
    result = engine.strategy_decision(**request, candidates=rows, strategy_name="reliability_first")
    assert result["selected_candidate"] == "MOCK-DS-FAST"


@pytest.mark.parametrize("mode,expected", [("latency_first", "MOCK-DS-FAST"), ("cost_first", "MOCK-DS-CHEAP")])
def test_weighted_v1_selection_matches_legacy(sample, mode, expected):
    result = decide(sample, "weighted_v1", strategy_config={"weighted_substrategy": mode})
    assert result["selected_candidate"] == expected


@pytest.mark.parametrize(
    "mode,candidate_id,expected",
    [
        ("latency_first", "MOCK-DS-FAST", 0.1945), ("latency_first", "MOCK-DS-CHEAP", 0.4115),
        ("latency_first", "REAL-DS-48", 0.5188), ("cost_first", "MOCK-DS-CHEAP", 0.1865),
        ("cost_first", "MOCK-DS-FAST", 0.2395), ("cost_first", "REAL-DS-48", 0.4504),
    ],
)
def test_weighted_v1_manual_baselines(sample, mode, candidate_id, expected):
    result = decide(sample, "weighted_v1", strategy_config={"weighted_substrategy": mode})
    row = next(r for r in result["ranked_candidates"] if r["candidate_id"] == candidate_id)
    assert row["final_score"] == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("n,rate,expected", [(1, 1.0, 1 / 3), (100, 0.97, 4 / 102), (100, 0.99, 2 / 102)])
def test_beta_examples(n, rate, expected):
    assert engine.beta_risks(n, rate)["bayesian_failure_risk"] == pytest.approx(expected, abs=1e-9)


def test_beta_records_counts():
    result = engine.beta_risks(100, 0.97)
    assert result["success_count"] == pytest.approx(97)
    assert result["failure_count"] == pytest.approx(3)
    assert result["raw_failure_risk"] == pytest.approx(0.03)


def test_confidence_aware_does_not_double_penalize(sample):
    result = decide(sample, "confidence_aware_v2")
    real = next(r for r in result["ranked_candidates"] if r["candidate_id"] == "REAL-DS-48")
    assert real["small_sample_penalty"] == real["confidence_penalty"] == real["evidence_penalty"] == 0
    assert real["final_score"] == pytest.approx(real["latency_norm"] * 0.4 + real["cost_norm"] * 0.3 + real["bayesian_failure_risk"] * 0.3)


def test_confidence_aware_records_required_details(sample):
    row = decide(sample, "confidence_aware_v2")["ranked_candidates"][0]
    required = {"sample_size", "success_rate", "success_count", "failure_count", "raw_failure_risk", "bayesian_failure_risk", "latency_norm", "cost_norm", "final_score"}
    assert required.issubset(row)


def test_confidence_v2_candidate_schema_is_golden_legacy_contract(sample):
    row = decide(sample, "confidence_aware_v2")["ranked_candidates"][0]
    assert set(row) == {
        "candidate_id", "channel_id", "eligible", "exclusion_reason",
        "priority", "rank", "is_mock", "data_source", "latency_ms",
        "estimated_cost", "sample_size", "success_rate", "success_count",
        "failure_count", "raw_failure_risk", "bayesian_failure_risk",
        "latency_norm", "cost_norm", "small_sample_penalty",
        "confidence_penalty", "evidence_penalty", "final_score",
    }


def test_tie_breakers_priority_then_channel_id(sample):
    request, candidates = sample
    rows = copy.deepcopy(candidates[1:])
    for row in rows:
        row["latency_ms"] = "600"
        row["priority"] = "10"
    rows[0]["channel_id"], rows[1]["channel_id"] = "102", "101"
    assert engine.strategy_decision(**request, candidates=rows, strategy_name="fastest_first")["selected_candidate"] == "MOCK-DS-CHEAP"


@pytest.mark.parametrize(
    "patch,request_patch,reason",
    [
        ({"latency_ms": ""}, {}, "missing_latency"),
        ({"input_price_per_1m": ""}, {}, "missing_price"),
        ({"metrics_updated_at": "2026-07-10 00:00:00"}, {}, "stale_metrics"),
        ({"currency": "USD"}, {}, "currency_mismatch"),
        ({"supports_stream": "pending_confirmation"}, {"stream_required": True}, "stream_capability_unknown"),
    ],
)
def test_filter_edge_cases(sample, patch, request_patch, reason):
    request, candidates = sample
    request = {**request, **request_patch}
    rows = [copy.deepcopy(candidates[0])]
    rows[0].update(patch)
    result = engine.strategy_decision(**request, candidates=rows, strategy_name="fastest_first")
    assert result["excluded_candidates"][0]["exclusion_reason"] == reason


def test_dry_run_does_not_write(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src" / "strategy_engine.py"), "--dry-run", "--output-dir", str(tmp_path)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0 and not list(tmp_path.iterdir())


def test_custom_output_does_not_pollute_default(tmp_path):
    formal = [engine.DEFAULT_OUTPUT_DIR / engine.CSV_NAME, engine.DEFAULT_OUTPUT_DIR / engine.JSON_NAME]
    before = {p: p.read_bytes() if p.exists() else None for p in formal}
    engine.write_outputs(engine.run_comparison(), tmp_path)
    assert all((tmp_path / name).exists() for name in (engine.CSV_NAME, engine.JSON_NAME))
    assert before == {p: p.read_bytes() if p.exists() else None for p in formal}


def test_repeated_output_is_stable(tmp_path):
    engine.write_outputs(engine.run_comparison(), tmp_path)
    before = [(tmp_path / name).read_bytes() for name in (engine.CSV_NAME, engine.JSON_NAME)]
    engine.write_outputs(engine.run_comparison(), tmp_path)
    assert before == [(tmp_path / name).read_bytes() for name in (engine.CSV_NAME, engine.JSON_NAME)]


def test_input_hashes_unchanged(tmp_path):
    paths = [engine.POLICY_PATH, engine.CANDIDATES_PATH, engine.SCENARIOS_PATH, ROOT / "data" / "scenario_candidates_v1.csv", ROOT / "data" / "manual_scoring_baseline_v1.csv", ROOT / "src" / "offline_decision_engine.py", ROOT / "tests" / "test_offline_decision_engine.py"]
    digest = lambda p: hashlib.sha256(p.read_bytes()).digest()
    before = {p: digest(p) for p in paths}
    engine.write_outputs(engine.run_comparison(), tmp_path)
    assert before == {p: digest(p) for p in paths}


def test_csv_output_structure(tmp_path):
    engine.write_outputs(engine.run_comparison(), tmp_path)
    with (tmp_path / engine.CSV_NAME).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == engine.CSV_COLUMNS and len(rows) == 10
    assert all(row["is_mock_evaluation"] == "TRUE" for row in rows)


def test_json_output_structure(tmp_path):
    engine.write_outputs(engine.run_comparison(), tmp_path)
    payload = json.loads((tmp_path / engine.JSON_NAME).read_text(encoding="utf-8"))
    assert payload["evaluation_type"] == "offline_mock_strategy_comparison"
    assert payload["is_mock_evaluation"] is True and len(payload["results"]) == 10


def test_external_working_directory(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src" / "strategy_engine.py"), "--dry-run"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0


def test_unknown_strategy_cli_nonzero():
    result = subprocess.run([sys.executable, str(ROOT / "src" / "strategy_engine.py"), "--strategy", "unknown"], capture_output=True)
    assert result.returncode != 0


def test_source_has_no_network_calls():
    source = (ROOT / "src" / "strategy_engine.py").read_text(encoding="utf-8")
    assert all(token not in source for token in ("requests", "urllib", "http.client", "socket", "aiohttp"))


def test_original_fifteen_scenarios_still_match():
    import offline_decision_engine
    results = offline_decision_engine.run()
    assert len(results) == 15 and all(row["decision_matches_expected"] for row in results)
