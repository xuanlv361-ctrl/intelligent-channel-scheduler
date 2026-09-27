from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from formal_agent_skill_runtime import FormalAgentSkillRuntime
from backend.formal_agent_skill_audit_service import FormalAgentSkillAuditService
from backend.formal_agent_skill_bindings import FormalAgentSkillBindings


class Attribution:
    mutations = 0

    def chain(self, decision_id):
        return {"decision_id": decision_id, "authoritative_actual_channel": "verified-48"}

    def record(self, *_args, **_kwargs):
        self.mutations += 1


class Circuit:
    mutations = 0

    def get_state(self, circuit_id):
        return {"circuit_id": circuit_id, "state": "OPEN"}

    def list_transition_audits(self, circuit_id):
        return [{"circuit_id": circuit_id, "to_state": "OPEN"}]

    def before_request(self, *_args, **_kwargs):
        self.mutations += 1


class Exploration:
    mutations = 0

    def status(self):
        return {"kill_switches": [{"active": True}], "real_execution_allowed": False}

    def recommend(self, *_args, **_kwargs):
        self.mutations += 1


def test_service_factory_exposes_only_read_methods_and_marks_local_provenance(tmp_path):
    attribution, circuit, exploration = Attribution(), Circuit(), Exploration()
    bindings = FormalAgentSkillBindings.from_services(
        attribution=attribution, circuit=circuit, exploration=exploration)
    runtime = FormalAgentSkillRuntime.build(
        bindings=bindings,
        audit=FormalAgentSkillAuditService(tmp_path / "audit.db"))
    decision = runtime.invoke_from_agent(
        skill_id="explain-scheduler-decision", arguments={"decision_id": "DEC-1"},
        decision_id="OUTER-1")
    breaker = runtime.invoke_from_agent(
        skill_id="inspect-circuit-breaker", arguments={"circuit_id": "local:c1"},
        decision_id="OUTER-2")
    governance = runtime.invoke_from_agent(
        skill_id="inspect-exploration-governance", arguments={}, decision_id="OUTER-3")
    assert decision["data"]["authoritative_actual_channel"] == "verified-48"
    assert breaker["data"]["state"]["state"] == "OPEN"
    assert governance["data"]["kill_switches"][0]["active"] is True
    assert all(item["network_called"] is False for item in (decision, breaker, governance))
    assert attribution.mutations == circuit.mutations == exploration.mutations == 0
