from datetime import datetime, timezone

import pytest

from backend.scheduler_attribution_service import (
    AttributionError, SchedulerAttributionService,
)


def decision(**updates):
    value = {
        "decision_id": "DEC-1", "run_id": "RUN-1", "request_id": "REQ-1",
        "requested_model": "model-1", "selected_target_id": "candidate-a",
        "selected_channel_id": "channel-a", "decision_policy_version": "policy-v1",
        "metric_snapshot_ids": ["MS-1"], "confidence_snapshot_ids": ["CS-1"],
        "execution_status": "decision_only", "executed_candidate_id": None,
        "executed_channel_id": None,
    }
    value.update(updates)
    return value


def event(**updates):
    value = {
        "decision_id": "DEC-1", "request_id": "REQ-1",
        "downstream_correlation_id": "DOWN-1",
        "authoritative_actual_channel": "actual-channel-9",
        "execution_result": "success", "source_type": "trusted_execution_layer",
        "observed_at": "2026-07-31T14:45:00+00:00",
        "idempotency_key": "ATTR-IDEMP-1",
    }
    value.update(updates)
    return value


def test_missing_actual_attribution_remains_fail_closed(tmp_path):
    service = SchedulerAttributionService(tmp_path / "a.sqlite3")
    chain = service.record_scheduler_decision(decision())
    assert chain["status"] == "authoritative_execution_attribution_missing"
    assert chain["authoritative_actual_channel"] is None
    assert chain["scheduler_decision"]["selected_channel_id"] == "channel-a"


def test_authoritative_event_completes_chain_without_rewriting_intent(tmp_path):
    service = SchedulerAttributionService(tmp_path / "a.sqlite3")
    service.record_scheduler_decision(decision())
    chain = service.record_execution_attribution(event())
    assert chain["status"] == "authoritatively_attributed"
    assert chain["authoritative_actual_channel"] == "actual-channel-9"
    assert chain["scheduler_decision"]["selected_channel_id"] == "channel-a"


def test_untrusted_conflicting_duplicate_and_sensitive_events_fail_closed(tmp_path):
    service = SchedulerAttributionService(tmp_path / "a.sqlite3")
    service.record_scheduler_decision(decision())
    with pytest.raises(AttributionError, match="untrusted"):
        service.record_execution_attribution(event(source_type="scheduler_recommendation"))
    first = service.record_execution_attribution(event())
    assert service.record_execution_attribution(event())["event_count"] == first["event_count"] == 1
    with pytest.raises(AttributionError, match="idempotency_conflict"):
        service.record_execution_attribution(event(authoritative_actual_channel="other"))
    with pytest.raises(AttributionError, match="sensitive"):
        service.record_scheduler_decision(decision(metadata={"api_key": "forbidden"}))


def test_out_of_order_event_is_preserved_pending_decision_and_restart_recovers(tmp_path):
    path = tmp_path / "a.sqlite3"
    service = SchedulerAttributionService(path)
    assert service.record_execution_attribution(event())["status"] == "pending_scheduler_decision"
    recovered = SchedulerAttributionService(path).record_scheduler_decision(decision())
    assert recovered["status"] == "authoritatively_attributed"
    assert recovered["event_count"] == 1


def test_decision_identity_conflict_is_rejected(tmp_path):
    service = SchedulerAttributionService(tmp_path / "a.sqlite3")
    service.record_scheduler_decision(decision())
    with pytest.raises(AttributionError, match="scheduler_decision_conflict"):
        service.record_scheduler_decision(decision(selected_target_id="other"))
