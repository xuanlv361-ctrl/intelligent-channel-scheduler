from src.services.console_service import replay


ROWS = [
    {"request_id": "r1", "channel_id": "a", "result": "success",
     "latency_ms": 200, "cost_cny": "0.020", "source_type": "measured_uat"},
    {"request_id": "r2", "channel_id": "b", "result": "failure",
     "latency_ms": 100, "cost_cny": "0.010", "source_type": "measured_uat"},
]


def test_replay_is_evidence_driven_deterministic_and_network_free():
    fastest = replay("fastest_first", ROWS)
    reliable = replay("reliability_first", ROWS)
    assert fastest == replay("fastest_first", ROWS)
    assert fastest["selected_candidate"] == "b"
    assert reliable["selected_candidate"] == "a"
    assert fastest["network_calls"] == 0
    assert fastest["provenance"]["sample_size"] == 2
    assert len(fastest["provenance"]["evidence_sha256"]) == 64


def test_missing_cost_and_latency_are_not_invented():
    result = replay("cheapest_first", [
        {"request_id": "r", "channel_id": "x", "result": "success",
         "source_type": "measured_uat"}
    ])
    assert result["cost_estimate_cny"] is None
    assert result["latency_estimate_ms"] is None
    assert result["sla_risk_estimate"] is None
    assert result["concentration_risk"] is None


def test_empty_replay_is_honestly_unroutable():
    result = replay("fastest_first", [])
    assert result["selected_candidate"] is None
    assert result["changed_decision_rate"] is None
    assert result["candidate_metrics"] == []
