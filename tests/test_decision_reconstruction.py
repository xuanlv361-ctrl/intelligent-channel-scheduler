import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_logger import DecisionLogger  # noqa: E402
from backend.decision_reconstruction_service import (  # noqa: E402
    DecisionReconstructionError,
    DecisionReconstructionService,
)
from backend.scheduler_attribution_service import SchedulerAttributionService  # noqa: E402


def runtime_decision(**updates):
    value = {
        "decision_id": "DEC-R1",
        "run_id": "RUN-1",
        "request_id": "REQ-1",
        "runtime_version": "scheduler-runtime-v1",
        "mode": "mock_execute",
        "strategy": "latency_first",
        "requested_model": "model-1",
        "stream": False,
        "catalog_version": "catalog-v1",
        "catalog_sha256": "A" * 64,
        "decision_policy_version": "policy-v1",
        "candidate_count": 2,
        "eligible_count": 1,
        "excluded_count": 1,
        "candidate_ranking": [
            {"candidate_id": "candidate-a", "channel_id": "channel-a", "rank": 1, "eligible": True, "final_score": 0.1},
            {"candidate_id": "candidate-b", "channel_id": "channel-b", "rank": None, "eligible": False, "exclusion_reason": "stale_metrics"},
        ],
        "excluded_candidates": [{"candidate_id": "candidate-b", "exclusion_reason": "stale_metrics"}],
        "selected_target_id": "candidate-a",
        "selected_channel_id": "channel-a",
        "recommended_candidate": "candidate-a",
        "fallback_order": [],
        "attempts": [{"attempt_number": 1, "candidate_id": "candidate-a", "result": "success", "network_called": False}],
        "fallback_executed": False,
        "fallback_trace": ["candidate-a"],
        "outcome": "selected",
        "stopped_reason": "success",
        "network_called": False,
        "metric_snapshot_ids": ["MS-1"],
        "confidence_snapshot_ids": ["CS-1"],
        "capability_evidence_ids": ["CAP-1"],
        "price_record_sha256s": ["B" * 64],
        "request_constraints": {"input_tokens": 20, "output_tokens": 10,
                                "capability_scope": {"required_modalities": ["text"]}},
        "metric_context": {"provider": "incremental_metric_snapshot"},
        "price_context": {"provider": "versioned_price_catalog"},
        "safety_governance": {"capability": {"allowed": True}},
        "acceptance_assertion": {"expected_candidate_id": "candidate-a",
                                 "actual_candidate_id": "candidate-a", "passed": True},
        "governance_assertions": {"execution_network_forbidden": True},
        "metadata": {"api_key": "must-not-appear"},
    }
    value.update(updates)
    return value


def service(tmp_path):
    log_path = tmp_path / "decisions.jsonl"
    attribution = SchedulerAttributionService(tmp_path / "attribution.sqlite3")
    return log_path, attribution, DecisionReconstructionService(log_path, attribution)


def test_reconstruction_is_stable_complete_and_redacted(tmp_path):
    log_path, attribution, reconstruction = service(tmp_path)
    decision = runtime_decision()
    DecisionLogger(log_path).log_runtime(decision)
    attribution.record_scheduler_decision({
        key: value for key, value in decision.items() if key != "metadata"
    })

    first = reconstruction.reconstruct("DEC-R1")
    second = reconstruction.reconstruct("DEC-R1")
    assert first == second
    assert first["status"] == "ready"
    assert first["reconstruction_sha256"] == second["reconstruction_sha256"]
    assert first["policy_binding"]["metric_snapshot_ids"] == ["MS-1"]
    assert first["policy_binding"]["capability_evidence_ids"] == ["CAP-1"]
    assert first["request_classification"]["request_constraints"]["input_tokens"] == 20
    assert first["decision_result"]["acceptance_assertion"]["passed"] is True
    assert first["candidate_evaluation"]["excluded_candidates"][0]["exclusion_reason"] == "stale_metrics"
    assert first["execution_trace"]["attempts"][0]["result"] == "success"
    assert "must-not-appear" not in str(first)


def test_missing_or_incomplete_decision_fails_closed(tmp_path):
    log_path, _attribution, reconstruction = service(tmp_path)
    assert reconstruction.reconstruct("missing")["reason"] == "decision_not_found"
    DecisionLogger(log_path).log_runtime({
        "decision_id": "D2", "runtime_version": "v1", "request_id": "R2",
        "mode": "simulation", "strategy": "x",
    })
    result = reconstruction.reconstruct("D2")
    assert result["status"] == "blocked"
    assert "candidate_ranking" in result["missing_fields"]


def test_conflicting_decision_id_and_non_finite_value_fail_closed(tmp_path):
    log_path, _attribution, reconstruction = service(tmp_path)
    logger = DecisionLogger(log_path)
    logger.log_runtime(runtime_decision())
    logger.log_runtime(runtime_decision(strategy="cost_first"))
    with pytest.raises(DecisionReconstructionError, match="decision_id_conflict"):
        reconstruction.reconstruct("DEC-R1")

    other_path, _other_attr, other = service(tmp_path / "other")
    DecisionLogger(other_path).log_runtime(runtime_decision(candidate_ranking=[{"final_score": float("nan")}]))
    with pytest.raises(DecisionReconstructionError, match="non_finite"):
        other.reconstruct("DEC-R1")


@pytest.mark.parametrize("case,updates", [
    ("success", {}),
    ("filtered", {
        "decision_id": "DEC-FILTERED", "request_id": "REQ-FILTERED",
        "excluded_candidates": [{"candidate_id": "candidate-b",
                                  "exclusion_reason": "required_capability_unconfirmed"}],
    }),
    ("fallback", {
        "decision_id": "DEC-FALLBACK", "request_id": "REQ-FALLBACK",
        "attempts": [
            {"attempt_number": 1, "candidate_id": "candidate-a", "result": "failed",
             "error_category": "upstream_timeout", "network_called": False},
            {"attempt_number": 2, "candidate_id": "candidate-b", "result": "success",
             "network_called": False},
        ],
        "fallback_executed": True, "fallback_trace": ["candidate-a", "candidate-b"],
    }),
    ("all_unavailable", {
        "decision_id": "DEC-NONE", "request_id": "REQ-NONE",
        "eligible_count": 0, "excluded_count": 2,
        "selected_target_id": None, "selected_channel_id": None,
        "recommended_candidate": None, "outcome": "unroutable",
        "attempts": [], "fallback_trace": [], "stopped_reason": "no_eligible_candidates",
    }),
])
def test_representative_decisions_are_all_reconstructable(case, updates, tmp_path):
    log_path, _attribution, reconstruction = service(tmp_path / case)
    decision = runtime_decision(**updates)
    DecisionLogger(log_path).log_runtime(decision)
    result = reconstruction.reconstruct(decision["decision_id"])
    assert result["status"] == "ready"
    assert result["decision_result"]["outcome"] == decision["outcome"]
    assert result["runtime_record_sha256"]
    assert result["reconstruction_sha256"]
