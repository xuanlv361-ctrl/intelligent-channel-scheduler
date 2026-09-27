import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from backend.scheduler_governance_bridge import SchedulerGovernanceBridge
from decision_logger import DecisionLogger
from scheduler import Scheduler


def request(operation=None, **metadata):
    values = {"environment_id": "china_uat", **metadata}
    if operation is not None:
        values["governance_operation"] = operation
    return SimpleNamespace(metadata=values, requested_model="deepseek-v4-flash")


class Traffic:
    def __init__(self, state="ACTIVE"):
        self.state = state

    def get(self, _identifier):
        return {"state": self.state, "environment_id": "china_uat",
                "channel_id": "M001", "model_id": "deepseek-v4-flash",
                "policy_version": "traffic-v1", "real_execution_allowed": False}


class Probe:
    def get_lease(self, _identifier):
        return {"state": "ACTIVE", "environment_id": "china_uat",
                "channel_id": "M001", "model_id": "deepseek-v4-flash",
                "policy_version": "probe-v1", "network_called": False}


class HighCost:
    def get(self, _identifier):
        return {"state": "RUNNING", "environment_id": "china_uat",
                "model_id": "deepseek-v4-flash", "policy_version": "cost-v1",
                "real_execution_allowed": False}


def test_bridge_fails_closed_for_unknown_missing_and_wrong_scope():
    bridge = SchedulerGovernanceBridge()
    candidate = {"candidate_id": "MOCK", "channel_id": "M001"}
    assert bridge.evaluate(request=request(), candidate=candidate)["allowed"] is True
    unknown = bridge.evaluate(request=request("invented"), candidate=candidate)
    assert unknown["allowed"] is False and unknown["fail_closed"] is True
    missing = bridge.evaluate(request=request(
        "traffic_change", traffic_change_proposal_id="TC-1"), candidate=candidate)
    assert missing["reason"] == "traffic_change_service_unavailable"
    wrong = SchedulerGovernanceBridge(traffic_change=Traffic()).evaluate(
        request=request("traffic_change", traffic_change_proposal_id="TC-1"),
        candidate={"channel_id": "M002"})
    assert wrong["reason"] == "traffic_change_scope_or_state_invalid"


def test_bridge_accepts_only_preexisting_offline_governance_state():
    candidate = {"channel_id": "M001"}
    bridge = SchedulerGovernanceBridge(
        traffic_change=Traffic(), probe=Probe(), high_cost_test=HighCost())
    cases = [
        request("traffic_change", traffic_change_proposal_id="TC-1"),
        request("probe", probe_lease_id="PL-1"),
        request("high_cost_test", high_cost_test_id="HC-1"),
    ]
    results = [bridge.evaluate(request=item, candidate=candidate) for item in cases]
    assert all(item["allowed"] is True for item in results)
    assert all(item["real_execution_allowed"] is False for item in results)
    assert {item["capability_id"] for item in results} == {
        "ADV-017", "ADV-018", "ADV-019"
    }


def test_scheduler_filters_each_candidate_through_governance_bridge(tmp_path):
    class OneChannelOnly:
        def evaluate(self, *, request, candidate):
            allowed = candidate.get("channel_id") == "M001"
            return {"operation": "probe", "allowed": allowed,
                    "reason": None if allowed else "probe_scope_or_lease_invalid",
                    "capability_id": "ADV-018", "real_execution_allowed": False,
                    "network_called": False}

    scheduler = Scheduler(
        logger=DecisionLogger(tmp_path / "decisions.jsonl"),
        id_generator=lambda _request, _index: "GOV-1",
        governance_bridge=OneChannelOnly())
    result = scheduler.route({
        "request_id": "GOV-REQ", "requested_model": "deepseek-v4-flash",
        "stream": False, "input_tokens": 10, "output_tokens": 10,
        "currency": "CNY", "strategy": "confidence_aware_v2",
        "mode": "simulation", "metadata": {
            "environment_id": "china_uat", "governance_operation": "probe"},
    })
    assert result["recommended_candidate"] == "MOCK-DS-FAST"
    assert result["safety_governance"]["execution_governance"]
    assert all(item["real_execution_allowed"] is False
               for item in result["safety_governance"]["execution_governance"])
    assert {row["routing_block_reason"] for row in result["excluded_candidates"]
            if row["candidate_id"] != "MOCK-DS-FAST"} == {
        "probe_scope_or_lease_invalid"
    }
