import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.traffic_change_governance_service import (
    TrafficChangeGovernanceError,
    TrafficChangeGovernanceService,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)


def service(tmp_path):
    policy = json.loads((ROOT / "config/traffic_change_governance_policy_v1.json").read_text())
    policy.update({"enabled": True, "maximum_rollout_percent": 100})
    policy_path = tmp_path / "traffic-policy.json"
    policy_path.write_text(json.dumps(policy))
    result = TrafficChangeGovernanceService(tmp_path / "traffic.db", policy_path, clock=lambda: NOW)
    timestamp = NOW.isoformat().replace("+00:00", "Z")
    with result.connect() as db:
        db.executescript("""
        CREATE TABLE configuration_versions(
          configuration_version TEXT,policy_version TEXT,created_at TEXT,is_active INTEGER,
          revision INTEGER,tenant_id TEXT,workspace_id TEXT);
        CREATE TABLE standardized_call_logs(
          record_id TEXT,occurred_at TEXT,request_id TEXT,response_id TEXT,decision_id TEXT,
          environment_id TEXT,requested_model TEXT,actual_model TEXT,provider TEXT,
          channel_id TEXT,channel_name TEXT,stream INTEGER,request_status TEXT,http_status INTEGER,
          total_attempts INTEGER,total_latency_ms REAL,input_tokens INTEGER,cached_input_tokens INTEGER,
          output_tokens INTEGER,cost_amount TEXT,currency TEXT,configuration_version TEXT,
          source_type TEXT,updated_at TEXT,tenant_id TEXT,workspace_id TEXT,
          duplicate_status TEXT,channel_source TEXT,error_category TEXT);
        """)
        db.execute("INSERT INTO configuration_versions VALUES(?,?,?,?,?,?,?)", (
            "decision-policy-v2.0.0", "decision-policy-v2.0.0", timestamp, 1, 1,
            "tenant_local_dev_v1", "workspace_local_dev_v1"))
        for index, status in enumerate(("FAILED", "FAILED"), 1):
            db.execute("INSERT INTO standardized_call_logs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                f"LOG-{index}", timestamp, f"REQ-{index}", None, f"DEC-{index}",
                "china_uat", "model-target", "model-target", "provider-a", "channel-real",
                "真实渠道", 0, status, 500, 1, 1200 + index * 100, 10, 0, 5, "0.01", "CNY",
                "decision-policy-v2.0.0", "realtime_execution", timestamp,
                "tenant_local_dev_v1", "workspace_local_dev_v1", "canonical",
                "scheduler_decision", "provider_5xx"))
    return result


def payload():
    return {
        "environment_mode": "sandbox",
        "source_model_id": "model-old",
        "source_channel_id": "channel-old",
        "target_model_id": "model-target",
        "target_channel_id": "channel-real",
        "source_policy_version": "decision-policy-v2.0.0",
        "target_policy_version": "decision-policy-v2.0.0",
        "rollout_percent": 5,
        "reason": "验证完整流量治理生命周期",
        "observation_seconds": 900,
        "minimum_sample_count": 1,
        "stop_conditions": {"error_rate_above": 0.1, "consecutive_failures": 2},
        "rollback_condition": "错误率超过 10% 时回滚",
    }


def test_current_traffic_uses_real_logs_and_authoritative_channels(tmp_path):
    subject = service(tmp_path)
    current = subject.control_current()
    assert current["configuration_version"] == "decision-policy-v2.0.0"
    assert current["metrics_24h"]["request_count"] == 2
    assert current["current_channel_id"] == "channel-real"
    channels = subject.control_channels()
    assert channels["total"] == 1
    assert channels["items"][0]["channel_source"] == "scheduler_decision"
    assert subject.control_impact(
        target_model_id="model-target", target_channel_id="channel-real")["status"] == "ready"


def test_full_sandbox_lifecycle_auto_stop_rollback_and_restart(tmp_path):
    subject = service(tmp_path)
    draft = subject.create_control_proposal(payload(), actor_id="operator-a")
    assert draft["state"] == "DRAFT"
    validated = subject.control_validate(draft["proposal_id"], actor_id="operator-a")
    submitted = subject.control_submit(draft["proposal_id"], actor_id="operator-a")
    approved = subject.control_approve(
        draft["proposal_id"], actor_id="approver-b", approval_reference="APR-UAT-1")
    active = subject.control_activate(
        draft["proposal_id"], actor_id="operator-a", request_ids=["REQ-1"],
        decision_ids=["DEC-1"])
    assert [validated["state"], submitted["state"], approved["state"], active["state"]] == [
        "VALIDATED", "PENDING_APPROVAL", "APPROVED", "CANARY_RUNNING"]
    stopped = subject.control_metrics(draft["proposal_id"])
    assert stopped["proposal_state"] == "AUTO_STOPPED"
    assert stopped["triggers"]
    rolled = subject.control_rollback(
        draft["proposal_id"], actor_id="operator-a", reason="automatic threshold breached")
    assert rolled["state"] == "ROLLED_BACK"
    assert rolled["request_ids"] == ["REQ-1"]
    assert any(item["event_type"] == "control_auto_stopped"
               for item in subject.control_audit(draft["proposal_id"])["items"])
    restarted = TrafficChangeGovernanceService(
        subject.path, tmp_path / "traffic-policy.json", clock=lambda: NOW)
    assert restarted.control_get(draft["proposal_id"])["state"] == "ROLLED_BACK"


def test_invalid_transition_and_production_without_adapter_are_rejected(tmp_path):
    subject = service(tmp_path)
    draft = subject.create_control_proposal(payload(), actor_id="operator-a")
    with pytest.raises(TrafficChangeGovernanceError, match="state_transition"):
        subject.control_activate(draft["proposal_id"], actor_id="operator-a")
    production = payload() | {"environment_mode": "production"}
    with pytest.raises(TrafficChangeGovernanceError, match="production_adapter"):
        subject.create_control_proposal(production, actor_id="operator-a")


def model_payload():
    value = payload()
    value.pop("target_channel_id")
    value["acceptance_run_id"] = "UAT-RUN-42"
    value["baseline_binding"] = {
        "model_id": "model-old", "policy_version": "decision-policy-v2.0.0"}
    value["candidate_binding"] = {
        "model_id": "model-target", "policy_version": "decision-policy-v2.0.0"}
    return value


def activate_model_proposal(subject):
    draft = subject.create_control_proposal(model_payload(), actor_id="operator-a")
    subject.control_validate(draft["proposal_id"], actor_id="operator-a")
    subject.control_submit(draft["proposal_id"], actor_id="operator-a")
    subject.control_approve(draft["proposal_id"], actor_id="approver-b")
    return subject.control_activate(draft["proposal_id"], actor_id="operator-a")


def test_model_level_canary_assignment_links_and_persists(tmp_path):
    subject = service(tmp_path)
    active = activate_model_proposal(subject)
    assert active["target_channel_id"] is None
    assert active["acceptance_run_id"] == "UAT-RUN-42"
    assert active["impact"]["metrics_scope"] == "model"
    assert subject.control_active_proposal(model_id="model-target")["proposal_id"] == active["proposal_id"]

    first = subject.control_assignment(
        "tenant-1:user-9", proposal_id=active["proposal_id"],
        request_id="REQ-CANARY", decision_id="DEC-CANARY")
    second = subject.control_assignment("tenant-1:user-9", proposal_id=active["proposal_id"])
    assert first == second
    assert first["variant"] in {"baseline", "candidate"}
    linked = subject.control_get(active["proposal_id"])
    assert linked["request_ids"] == ["REQ-CANARY"]
    assert linked["decision_ids"] == ["DEC-CANARY"]
    assert "control_execution_linked" in {
        item["event_type"] for item in subject.control_audit(active["proposal_id"])["items"]}

    restarted = TrafficChangeGovernanceService(
        subject.path, tmp_path / "traffic-policy.json", clock=lambda: NOW)
    assert restarted.control_assignment(
        "tenant-1:user-9", proposal_id=active["proposal_id"])["bucket"] == first["bucket"]


def test_assignment_supports_5_10_25_and_rollback_restores_baseline(tmp_path):
    subject = service(tmp_path)
    active = activate_model_proposal(subject)
    proposal_id = active["proposal_id"]
    buckets = {}
    for percentage in (5, 10, 25):
        if percentage != 5:
            subject.control_adjust(proposal_id, actor_id="operator-a", rollout_percent=percentage)
        assignments = [subject.control_assignment(str(index), proposal_id=proposal_id)
                       for index in range(400)]
        buckets[percentage] = {item["assignment_key"] for item in assignments
                               if item["variant"] == "candidate"}
    assert buckets[5] <= buckets[10] <= buckets[25]
    assert buckets[5] and len(buckets[25]) > len(buckets[5])

    rolled = subject.control_rollback(
        proposal_id, actor_id="operator-a", reason="canary ended")
    assert rolled["restored_binding"] == rolled["baseline_binding"]
    events = {item["event_type"] for item in subject.control_audit(proposal_id)["items"]}
    assert {"control_rollout_adjusted", "control_rolled_back"} <= events


def test_metric_read_is_pure_and_explicit_evaluation_auto_stops_model_canary(tmp_path):
    subject = service(tmp_path)
    active = activate_model_proposal(subject)
    proposal_id = active["proposal_id"]
    before = subject.control_read_metrics(proposal_id)
    assert before["metrics_scope"] == "model"
    assert before["proposal_state"] == "CANARY_RUNNING"
    with subject.connect() as db:
        assert db.execute("SELECT count(*) FROM traffic_change_metric_snapshots").fetchone()[0] == 0
    stopped = subject.control_evaluate_stop(proposal_id)
    assert stopped["proposal_state"] == "AUTO_STOPPED"
    with subject.connect() as db:
        assert db.execute("SELECT count(*) FROM traffic_change_metric_snapshots").fetchone()[0] == 1


def test_model_level_without_authoritative_channel_has_no_fake_channel_or_hhi(tmp_path):
    subject = service(tmp_path)
    with subject.connect() as db:
        db.execute("""UPDATE standardized_call_logs SET channel_id=NULL,channel_name=NULL,
          channel_source='unknown'""")
    impact = subject.control_impact(target_model_id="model-target")
    assert impact["status"] == "ready"
    assert impact["channel_hhi"] is None
    assert subject.control_channels()["items"] == []
