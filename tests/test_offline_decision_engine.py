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
import offline_decision_engine as engine  # noqa: E402


@pytest.fixture
def inputs():
    return engine.load_inputs()


def scenario_and_candidates(inputs, sid="S001"):
    policy, scenarios, candidates = inputs
    scenario = next(row for row in scenarios if row["scenario_id"] == sid)
    rows = [row for row in candidates if row["scenario_id"] == sid]
    return policy, scenario, rows


def test_policy_and_strategy_configuration(inputs):
    policy = inputs[0]
    assert policy["policy_version"] == "v1.0.0"
    assert set(policy["strategies"]) == {"latency_first", "cost_first"}


@pytest.mark.parametrize("strategy", ["latency_first", "cost_first"])
def test_strategy_weights_sum_to_one(inputs, strategy):
    assert sum(inputs[0]["strategies"][strategy]["weights"].values()) == pytest.approx(1)


@pytest.mark.parametrize(
    "field,value,scenario_patch,reason",
    [
        ("requested_model", "wrong", {}, "model_mismatch"),
        ("availability_status", "unavailable", {}, "availability_unavailable"),
        ("supports_text", "FALSE", {}, "text_not_supported"),
        ("supports_non_stream", "FALSE", {}, "non_stream_not_supported"),
        ("supports_stream", "FALSE", {"stream_required": "TRUE"}, "stream_not_supported"),
        ("supports_stream", "pending_confirmation", {"stream_required": "TRUE"}, "stream_capability_unknown"),
        ("latency_ms", "", {}, "missing_latency"),
        ("input_price_per_1m", "", {}, "missing_price"),
        ("currency", "USD", {}, "currency_mismatch"),
        ("metrics_updated_at", "", {}, "missing_metrics_updated_at"),
        ("metrics_updated_at", "2026-07-10 00:00:00", {}, "stale_metrics"),
    ],
)
def test_filters(inputs, field, value, scenario_patch, reason):
    policy, scenario, rows = scenario_and_candidates(inputs)
    candidate = copy.deepcopy(rows[1])
    candidate[field] = value
    scenario = {**scenario, **scenario_patch}
    assert engine.exclusion_reason(candidate, scenario, policy) == reason


def test_first_filter_reason_wins(inputs):
    policy, scenario, rows = scenario_and_candidates(inputs)
    candidate = {**rows[0], "requested_model": "wrong", "availability_status": "unavailable", "latency_ms": ""}
    assert engine.exclusion_reason(candidate, scenario, policy) == "model_mismatch"


def test_estimated_cost_and_normalizations(inputs):
    policy, scenario, rows = scenario_and_candidates(inputs)
    score = engine.calculate_score(rows[1], scenario, policy)
    assert score["estimated_cost"] == pytest.approx(0.003)
    assert score["latency_norm"] == pytest.approx(0.2)
    assert score["cost_norm"] == pytest.approx(0.3)
    assert score["failure_risk"] == pytest.approx(0.03)


def test_small_sample_penalty(inputs):
    policy, scenario, rows = scenario_and_candidates(inputs, "S009")
    score = engine.calculate_score(rows[0], scenario, policy)
    assert score["small_sample_penalty"] == 0.1
    assert score["confidence_penalty"] == 0


def test_low_confidence_penalty(inputs):
    policy, scenario, rows = scenario_and_candidates(inputs, "S010")
    score = engine.calculate_score(rows[0], scenario, policy)
    assert score["small_sample_penalty"] == 0
    assert score["confidence_penalty"] == 0.15


@pytest.mark.parametrize(
    "sid,candidate_id,expected",
    [
        ("S001", "MOCK-DS-FAST", 0.1945), ("S001", "MOCK-DS-CHEAP", 0.4115),
        ("S001", "REAL-DS-48", 0.5188), ("S002", "MOCK-DS-CHEAP", 0.1865),
        ("S002", "MOCK-DS-FAST", 0.2395), ("S002", "REAL-DS-48", 0.4504),
    ],
)
def test_manual_scoring_baseline(inputs, sid, candidate_id, expected):
    policy, scenario, rows = scenario_and_candidates(inputs, sid)
    row = next(row for row in rows if row["candidate_id"] == candidate_id)
    assert engine.calculate_score(row, scenario, policy)["final_score"] == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize(
    "sid,selected",
    [
        ("S001", "MOCK-DS-FAST"), ("S002", "MOCK-DS-CHEAP"),
        ("S003", "MOCK-DS-FAST"), ("S011", "TIE-A"),
        ("S012", "TIE-001"), ("S015", "REAL-DS-48"),
    ],
)
def test_required_scenario_selections(inputs, sid, selected):
    policy, scenario, rows = scenario_and_candidates(inputs, sid)
    result = engine.decide_scenario(scenario, rows, policy)
    assert result["selected_candidate"] == selected


def test_priority_tie_breaker(inputs):
    policy, scenario, rows = scenario_and_candidates(inputs, "S011")
    result = engine.decide_scenario(scenario, rows, policy)
    assert [(c["candidate_id"], c["rank"]) for c in result["candidates"]] == [("TIE-A", 1), ("TIE-B", 2)]


def test_channel_id_tie_breaker(inputs):
    policy, scenario, rows = scenario_and_candidates(inputs, "S012")
    result = engine.decide_scenario(scenario, rows, policy)
    assert [(c["candidate_id"], c["rank"]) for c in result["candidates"]] == [("TIE-001", 1), ("TIE-002", 2)]


@pytest.mark.parametrize("sid", ["S004", "S014"])
def test_unroutable_and_exclusion_evidence(inputs, sid):
    policy, scenario, rows = scenario_and_candidates(inputs, sid)
    result = engine.decide_scenario(scenario, rows, policy)
    assert result["outcome"] == "unroutable" and result["selected_candidate"] is None
    assert result["eligible_count"] == 0
    assert all(c["exclusion_reason"] for c in result["candidates"])


def test_all_fifteen_match_expected():
    results = engine.run()
    assert len(results) == 15
    assert sum(r["outcome"] == "selected" for r in results) == 13
    assert sum(r["outcome"] == "unroutable" for r in results) == 2
    assert all(r["decision_matches_expected"] for r in results)


def test_decision_does_not_depend_on_candidate_expected_fields(inputs):
    policy, scenario, rows = scenario_and_candidates(inputs)
    original = engine.decide_scenario(scenario, rows, policy)["selected_candidate"]
    tampered = copy.deepcopy(rows)
    for row in tampered:
        row["expected_selected"] = "FALSE" if row["expected_selected"] == "TRUE" else "TRUE"
        row["expected_eligible"] = "FALSE"
        row["expected_exclusion_reason"] = "fabricated"
    assert engine.decide_scenario(scenario, tampered, policy)["selected_candidate"] == original


def test_selected_candidates_are_eligible():
    for result in engine.run():
        selected = [c for c in result["candidates"] if c["candidate_id"] == result["selected_candidate"]]
        assert not selected or selected[0]["eligible"]


def test_excluded_scores_are_null_not_zero():
    for result in engine.run():
        for candidate in result["candidates"]:
            if not candidate["eligible"]:
                assert all(candidate[field] is None for field in engine.SCORE_FIELDS)


def test_json_and_csv_output_structure(tmp_path):
    results = engine.run()
    engine.write_outputs(results, tmp_path)
    payload = json.loads((tmp_path / engine.JSON_NAME).read_text(encoding="utf-8"))
    with (tmp_path / engine.CSV_NAME).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        summary = list(reader)
        assert reader.fieldnames == engine.SUMMARY_COLUMNS
    assert len(payload) == len(summary) == 15
    assert set(engine.SUMMARY_COLUMNS).issubset(payload[0])
    assert set(engine.SCORE_FIELDS).issubset(payload[0]["candidates"][0])


def test_repeated_outputs_are_stable(tmp_path):
    engine.write_outputs(engine.run(), tmp_path)
    first = [(tmp_path / name).read_bytes() for name in (engine.JSON_NAME, engine.CSV_NAME)]
    engine.write_outputs(engine.run(), tmp_path)
    assert first == [(tmp_path / name).read_bytes() for name in (engine.JSON_NAME, engine.CSV_NAME)]


def test_dry_run_does_not_write(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src" / "offline_decision_engine.py"), "--dry-run", "--output-dir", str(tmp_path)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0 and not list(tmp_path.iterdir())


def test_custom_output_does_not_pollute_default(tmp_path):
    formal = [ROOT / "output" / engine.JSON_NAME, ROOT / "output" / engine.CSV_NAME]
    before = {p: p.read_bytes() if p.exists() else None for p in formal}
    assert engine.main if True else False  # keep direct API import exercised
    engine.write_outputs(engine.run("S001"), tmp_path)
    assert all((tmp_path / name).exists() for name in (engine.JSON_NAME, engine.CSV_NAME))
    assert before == {p: p.read_bytes() if p.exists() else None for p in formal}


def test_input_hashes_unchanged_by_run(tmp_path):
    paths = [engine.POLICY_PATH, engine.SCENARIOS_PATH, engine.CANDIDATES_PATH, ROOT / "data" / "manual_scoring_baseline_v1.csv", ROOT / "src" / "build_scenario_candidates.py"]
    digest = lambda p: hashlib.sha256(p.read_bytes()).digest()
    before = {p: digest(p) for p in paths}
    engine.write_outputs(engine.run(), tmp_path)
    assert before == {p: digest(p) for p in paths}


def test_single_scenario_cli_and_external_working_directory(tmp_path):
    output = tmp_path / "nested"
    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "offline_decision_engine.py"), "--scenario", "S001", "--output-dir", str(output)],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert len(json.loads((output / engine.JSON_NAME).read_text(encoding="utf-8"))) == 1


def test_unknown_scenario_returns_nonzero(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src" / "offline_decision_engine.py"), "--scenario", "NOPE", "--output-dir", str(tmp_path)], capture_output=True)
    assert result.returncode != 0


def test_standard_library_engine_has_no_network_imports():
    source = (ROOT / "src" / "offline_decision_engine.py").read_text(encoding="utf-8")
    assert all(token not in source for token in ("requests", "urllib", "http.client", "socket", "aiohttp"))
