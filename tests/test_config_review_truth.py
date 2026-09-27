from src.services.console_service import config_review


def test_review_impact_is_computed_from_supplied_evidence_and_never_applied():
    rows = [
        {"request_id": "1", "channel_id": "slow", "result": "success",
         "latency_ms": 500, "cost_cny": "0.001", "source_type": "measured_uat"},
        {"request_id": "2", "channel_id": "fast", "result": "failure",
         "latency_ms": 100, "cost_cny": "0.010", "source_type": "measured_uat"},
    ]
    result = config_review({
        "weights": {"latency": 1.0}, "fallback": "slow", "timeout_ms": 1000,
        "baseline_strategy": "reliability_first", "strategy": "fastest_first",
        "evidence_records": rows,
    })
    assert result["replay_impact"]["evidence_status"] == "computed"
    assert result["replay_impact"]["baseline_selected_candidate"] == "slow"
    assert result["replay_impact"]["proposed_selected_candidate"] == "fast"
    assert result["replay_impact"]["changed_selected_candidate"] is True
    assert result["external_configuration_changed"] is False


def test_review_does_not_fabricate_impact_without_evidence():
    result = config_review({
        "weights": {"latency": 1.0}, "fallback": "x", "timeout_ms": 1000
    })
    impact = result["replay_impact"]
    assert impact["evidence_status"] == "insufficient_evidence"
    assert impact["estimated_cost_difference"] is None
    assert impact["estimated_latency_difference"] is None
