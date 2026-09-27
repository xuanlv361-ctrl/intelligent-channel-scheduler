import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_logger import DecisionLogger, content_sha256  # noqa: E402


def decision(n=1):
    return {"decision_id": f"DEC-{n}", "request_id": f"REQ{n}", "outcome": "selected", "is_mock": True}


def test_jsonl_logger_appends_one_decision_per_line(tmp_path):
    logger = DecisionLogger(tmp_path / "nested" / "decisions.jsonl")
    logger.log(decision(1))
    logger.log(decision(2))
    assert logger.read_all() == [decision(1), decision(2)]
    assert len(logger.path.read_text(encoding="utf-8").splitlines()) == 2


def test_logger_output_is_valid_json_and_deterministic_key_order(tmp_path):
    logger = DecisionLogger(tmp_path / "decisions.jsonl")
    logger.log(decision())
    line = logger.path.read_text(encoding="utf-8").strip()
    assert json.loads(line) == decision()
    assert line.startswith('{"decision_id"')


def test_logger_rejects_incomplete_or_non_mock_decisions(tmp_path):
    logger = DecisionLogger(tmp_path / "decisions.jsonl")
    with pytest.raises(ValueError, match="missing fields"):
        logger.log({"decision_id": "DEC"})
    with pytest.raises(ValueError, match="is_mock"):
        logger.log({**decision(), "is_mock": False})
    assert not logger.path.exists()


def test_read_missing_log_is_empty(tmp_path):
    assert DecisionLogger(tmp_path / "missing.jsonl").read_all() == []


def test_runtime_log_redacts_and_supports_queries(tmp_path):
    logger = DecisionLogger(tmp_path / "runtime.jsonl")
    row = {"decision_id": "D1", "runtime_version": "v1", "request_id": "R1", "mode": "simulation", "strategy": "x", "metadata": {"api_key": "bad", "safe": "ok"}}
    logger.log_runtime(row)
    stored = logger.find_by_decision_id("D1")
    assert stored["metadata"]["api_key"] == "[REDACTED]"
    assert logger.find_by_request_id("R1") == [stored]


def test_non_secret_token_measurements_remain_auditable(tmp_path):
    logger = DecisionLogger(tmp_path / "runtime.jsonl")
    logger.log_runtime({
        "decision_id": "D-TOKENS", "runtime_version": "v1",
        "request_id": "R-TOKENS", "mode": "simulation", "strategy": "x",
        "input_tokens": 120, "output_tokens": 30,
        "request_constraints": {"context_tokens": 32768},
        "metadata": {"access_token": "must-be-redacted"},
    })
    stored = logger.read_all()[0]
    assert stored["input_tokens"] == 120
    assert stored["output_tokens"] == 30
    assert stored["request_constraints"]["context_tokens"] == 32768
    assert stored["metadata"]["access_token"] == "[REDACTED]"


def test_all_log_paths_redact_protected_content_and_browser_credentials(tmp_path):
    logger = DecisionLogger(tmp_path / "decision.jsonl")
    secret = "protected-material-must-not-be-persisted"  # secret-scan: allow
    logger.log({
        "decision_id": "D-PROTECTED",
        "request_id": "R-PROTECTED",
        "outcome": "blocked",
        "is_mock": True,
        "metadata": {
            "prompt": secret,
            "Cookie": secret,
            "nested": {"response_body": secret, "safe_id": "SAFE-1"},
        },
    })
    raw = (tmp_path / "decision.jsonl").read_text(encoding="utf-8")
    assert secret not in raw
    stored = logger.read_all()[0]
    assert stored["metadata"]["prompt"] == "[REDACTED]"
    assert stored["metadata"]["Cookie"] == "[REDACTED]"
    assert stored["metadata"]["nested"]["response_body"] == "[REDACTED]"
    assert stored["metadata"]["nested"]["safe_id"] == "SAFE-1"


def test_runtime_jsonl_accepts_real_shadow_record(tmp_path):
    logger = DecisionLogger(tmp_path / "runtime.jsonl")
    logger.log_runtime({"decision_id": "D", "runtime_version": "v1", "request_id": "R", "mode": "real_shadow", "strategy": "x", "is_mock": False})
    assert logger.read_all()[0]["is_mock"] is False


def test_unique_decision_lookup_rejects_conflicting_reuse(tmp_path):
    logger = DecisionLogger(tmp_path / "runtime.jsonl")
    first = {"decision_id": "D", "runtime_version": "v1", "request_id": "R", "mode": "simulation", "strategy": "x"}
    logger.log_runtime(first)
    logger.log_runtime({**first, "strategy": "different"})
    with pytest.raises(ValueError, match="decision_id_conflict"):
        logger.find_unique_by_decision_id("D")


def test_content_hash_is_order_independent_and_value_sensitive():
    assert content_sha256({"a": 1, "b": 2}) == content_sha256({"b": 2, "a": 1})
    assert content_sha256({"a": 1}) != content_sha256({"a": 2})
