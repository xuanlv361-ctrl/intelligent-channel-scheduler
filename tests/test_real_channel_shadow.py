import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import run_real_channel_shadow as shadow  # noqa: E402

UTF8_SUBPROCESS_ENV = {**os.environ, "PYTHONUTF8": "1"}


@pytest.fixture
def inputs():
    return shadow.read_csv(shadow.REQUESTS_PATH), shadow.read_csv(shadow.CATALOG_PATH), shadow.load_json(shadow.CONFIG_PATH)


@pytest.fixture
def decisions(inputs):
    return shadow.run_shadow_decisions(*inputs)


def by_id(decisions, request_id):
    return next(row for row in decisions if row["request_id"] == request_id)


def test_reads_five_real_channels_and_requests(inputs):
    requests, catalog, _ = inputs
    assert len(requests) == len(catalog) == 5


def test_scenario_outcomes_and_recommendations(decisions):
    assert [(row["request_id"], row["outcome"], row["recommended_candidate"]) for row in decisions] == [
        ("SH001", "selected", "REAL-CHANNEL-19"),
        ("SH002", "selected", "REAL-CHANNEL-19"),
        ("SH003", "selected", "REAL-CHANNEL-19"),
        ("SH004", "selected", "REAL-CHANNEL-19"),
        ("SH005", "unroutable", None),
    ]


def test_sh001_uses_observed_mean_latency(decisions):
    result = by_id(decisions, "SH001")
    assert result["ranking"][0]["selection_metric"] == "latency_mean_ms"
    assert result["ranking"][0]["selection_value"] == 732.0


def test_sh002_detects_complete_price_tie(decisions):
    result = by_id(decisions, "SH002")
    assert result["price_tie"] is True
    assert {row["estimated_cost"] for row in result["ranking"]} == {0.002}


def test_sh003_detects_observed_success_tie(decisions):
    result = by_id(decisions, "SH003")
    assert result["observed_success_tie"] is True
    assert {row["observed_success_rate"] for row in result["ranking"]} == {1.0}


def test_sh004_calls_existing_strategy_engine(monkeypatch, inputs):
    requests, catalog, config = inputs
    original = shadow.strategy_engine.strategy_decision
    calls = []
    def recording_call(**kwargs):
        calls.append(kwargs["strategy_name"])
        return original(**kwargs)
    monkeypatch.setattr(shadow.strategy_engine, "strategy_decision", recording_call)
    shadow.run_shadow_decisions(requests, catalog, config)
    assert calls == ["fastest_first", "cheapest_first", "reliability_first", "confidence_aware_v2", "confidence_aware_v2"]


def test_sh004_score_is_engine_derived(decisions):
    result = by_id(decisions, "SH004")
    winner = result["ranking"][0]
    assert result["recommended_candidate"] == "REAL-CHANNEL-19"
    assert winner["selection_metric"] == "confidence_aware_final_score"
    assert winner["final_score"] == pytest.approx(0.20045714285714286)


def test_sh005_unroutable_and_all_stream_unknown(decisions):
    result = by_id(decisions, "SH005")
    assert result["eligible_count"] == 0 and result["excluded_count"] == 5
    assert {row["exclusion_reason"] for row in result["excluded_candidates"]} == {"stream_capability_unknown"}


def test_no_execution_or_routing_is_allowed(decisions):
    assert all(row["execution_attempted"] is False for row in decisions)
    assert all(row["routing_allowed"] is False for row in decisions)
    assert all(row["recommendation_scope"] == "offline_shadow_recommendation_only" for row in decisions)


def test_runner_does_not_import_or_call_channel_adapter():
    source = (ROOT / "src" / "run_real_channel_shadow.py").read_text(encoding="utf-8")
    assert "channel_adapter" not in source
    assert ".dispatch(" not in source


def test_no_network_clients_or_credentials():
    for name in ("real_channel_shadow_adapter.py", "run_real_channel_shadow.py"):
        source = (ROOT / "src" / name).read_text(encoding="utf-8")
        assert not any(token in source for token in ("import requests", "import httpx", "import urllib", "import socket", "aiohttp", "Authorization", "API_KEY"))


def test_required_json_structure(decisions):
    required = {"shadow_decision_id", "shadow_version", "request_id", "mode", "catalog_version", "policy_version", "strategy_catalog_version", "strategy", "requested_model", "stream_required", "candidate_count", "shadow_eligible_count", "eligible_count", "excluded_count", "outcome", "recommended_candidate", "execution_attempted", "routing_allowed", "recommendation_scope", "ranking", "excluded_candidates", "limitations", "input_catalog_sha256"}
    assert all(required <= set(row) for row in decisions)


def test_ranking_structure_and_real_markers(decisions):
    required = {"rank", "candidate_id", "channel_id", "channel_name", "eligible", "exclusion_reason", "final_score", "latency_mean_ms", "latency_p50_ms", "observed_success_rate", "measurement_count", "estimated_cost", "confidence_level", "routing_eligible", "routing_block_reason", "source_type", "is_mock", "selection_metric", "selection_value"}
    for decision in decisions:
        assert all(required <= set(row) for row in decision["ranking"])
        assert all(row["source_type"] == "measured" and row["is_mock"] is False for row in decision["ranking"])


def test_write_outputs_json_csv_and_jsonl(tmp_path, decisions):
    shadow.write_outputs(decisions, tmp_path)
    payload = json.loads((tmp_path / shadow.JSON_NAME).read_text(encoding="utf-8"))
    with (tmp_path / shadow.CSV_NAME).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        summary = list(reader)
    logs = [json.loads(line) for line in (tmp_path / shadow.JSONL_NAME).read_text(encoding="utf-8").splitlines()]
    assert len(payload) == len(summary) == len(logs) == 5
    assert reader.fieldnames == shadow.SUMMARY_COLUMNS
    assert payload == logs


def test_shadow_decision_ids_are_unique_and_queryable(decisions):
    ids = [row["shadow_decision_id"] for row in decisions]
    assert ids == [f"shadow-v1-SH{i:03d}" for i in range(1, 6)]
    assert len(ids) == len(set(ids))


def test_repeated_outputs_are_byte_stable(tmp_path, decisions):
    shadow.write_outputs(decisions, tmp_path)
    first = [(tmp_path / name).read_bytes() for name in (shadow.JSON_NAME, shadow.CSV_NAME, shadow.JSONL_NAME)]
    shadow.write_outputs(decisions, tmp_path)
    assert first == [(tmp_path / name).read_bytes() for name in (shadow.JSON_NAME, shadow.CSV_NAME, shadow.JSONL_NAME)]


def test_dry_run_does_not_write(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src" / "run_real_channel_shadow.py"), "--dry-run", "--output-dir", str(tmp_path)], capture_output=True, text=True, encoding="utf-8", env=UTF8_SUBPROCESS_ENV)
    assert result.returncode == 0 and not list(tmp_path.iterdir())


def test_custom_output_does_not_pollute_default(tmp_path):
    formal = [shadow.DEFAULT_OUTPUT_DIR / name for name in (shadow.JSON_NAME, shadow.CSV_NAME, shadow.JSONL_NAME)]
    before = {path: path.read_bytes() if path.exists() else None for path in formal}
    result = subprocess.run([sys.executable, str(ROOT / "src" / "run_real_channel_shadow.py"), "--output-dir", str(tmp_path)], capture_output=True)
    assert result.returncode == 0
    assert all((tmp_path / name).exists() for name in (shadow.JSON_NAME, shadow.CSV_NAME, shadow.JSONL_NAME))
    assert before == {path: path.read_bytes() if path.exists() else None for path in formal}


def test_known_request_filter_and_unknown_request_error(tmp_path):
    good = subprocess.run([sys.executable, str(ROOT / "src" / "run_real_channel_shadow.py"), "--request-id", "SH001", "--output-dir", str(tmp_path / "good")], capture_output=True, text=True, encoding="utf-8", env=UTF8_SUBPROCESS_ENV)
    assert good.returncode == 0
    assert len(json.loads((tmp_path / "good" / shadow.JSON_NAME).read_text(encoding="utf-8"))) == 1
    bad = subprocess.run([sys.executable, str(ROOT / "src" / "run_real_channel_shadow.py"), "--request-id", "NOPE", "--output-dir", str(tmp_path / "bad")], capture_output=True, text=True, encoding="utf-8", env=UTF8_SUBPROCESS_ENV)
    assert bad.returncode != 0 and not (tmp_path / "bad").exists()


def test_external_working_directory(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src" / "run_real_channel_shadow.py"), "--dry-run", "--request-id", "SH001"], cwd=tmp_path, capture_output=True)
    assert result.returncode == 0


def test_protected_file_hashes_unchanged_by_run(tmp_path):
    paths = [
        ROOT / "data" / "real_channel_measurements_v1.csv",
        ROOT / "data" / "channel_catalog_real_v1.csv",
        ROOT / "output" / "real_channel_measurement_summary_v1.json",
        ROOT / "data" / "candidate_channels_v1.csv",
        ROOT / "data" / "mock_channel_catalog_v3.csv",
        ROOT / "config" / "strategy_catalog_v2.json",
        ROOT / "config" / "decision_policy_v1.json",
    ]
    digest = lambda path: hashlib.sha256(path.read_bytes()).digest()
    before = {path: digest(path) for path in paths}
    result = subprocess.run([sys.executable, str(ROOT / "src" / "run_real_channel_shadow.py"), "--output-dir", str(tmp_path)], capture_output=True)
    assert result.returncode == 0
    assert before == {path: digest(path) for path in paths}


def test_outputs_do_not_contain_secrets_or_execution_response(tmp_path, decisions):
    shadow.write_outputs(decisions, tmp_path)
    combined = "".join((tmp_path / name).read_text(encoding="utf-8") for name in (shadow.JSON_NAME, shadow.CSV_NAME, shadow.JSONL_NAME))
    assert not any(token in combined for token in ("Authorization", "api_key", "API Key", "execution_response"))
