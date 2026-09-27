import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT_PATH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_PATH / "src"))

from backend.capability_evidence_service import CapabilityEvidenceService
from backend.circuit_breaker_service import CircuitBreakerService
from backend.exploration_governance_service import ExplorationGovernanceService
from channel_adapter import MockChannelAdapter
from decision_logger import DecisionLogger
from scheduler import ROOT, Scheduler


BASE = {"request_id": "SAFE-1", "requested_model": "deepseek-v4-flash",
        "stream": False, "input_tokens": 50, "output_tokens": 20,
        "currency": "CNY", "strategy": "confidence_aware_v2",
        "mode": "simulation", "metadata": {"environment_id": "china_uat",
        "request_profile_id": "P01", "run_id": "RUN-SAFE"}}


def make(tmp_path, **kwargs):
    return Scheduler(logger=DecisionLogger(tmp_path / "decisions.jsonl"),
                     id_generator=lambda _req, index: f"SAFE-DEC-{index}", **kwargs)


def circuit_service(tmp_path, threshold=1):
    policy = json.loads((ROOT / "config/circuit_breaker_policy_v1.json").read_text())
    policy["failure_threshold"] = threshold
    return CircuitBreakerService(tmp_path / "safety.sqlite3", policy)


def test_open_circuit_is_excluded_before_selection(tmp_path):
    probe = make(tmp_path).route(BASE)
    blocked_candidate = probe["recommended_candidate"]
    circuit = circuit_service(tmp_path)
    circuit_id = f"china_uat:{blocked_candidate}:deepseek-v4-flash"
    assert circuit.record_failure(circuit_id, "trip-1")["state"] == "OPEN"
    result = make(tmp_path, circuit_breaker_service=circuit).route(
        {**BASE, "request_id": "SAFE-OPEN"})
    excluded = {row["candidate_id"]: row for row in result["excluded_candidates"]}
    assert excluded[blocked_candidate]["routing_block_reason"] == "circuit_open"
    assert result["recommended_candidate"] != blocked_candidate


def test_mock_outcomes_update_circuit_but_never_claim_actual_channel(tmp_path):
    class FailureThenSuccess(MockChannelAdapter):
        def __init__(self):
            super().__init__(); self.calls = 0
        def send(self, request, candidate):
            self.calls += 1
            raw = super().send(request, candidate)
            return ({**raw, "status": "timeout", "error_category": "upstream_timeout"}
                    if self.calls == 1 else raw)
    circuit = circuit_service(tmp_path)
    result = make(tmp_path, adapter=FailureThenSuccess(),
                  circuit_breaker_service=circuit).route(
        {**BASE, "mode": "mock_execute", "request_id": "SAFE-EXEC"})
    primary = result["selected_target_id"]
    assert result["safety_governance"]["circuit_breakers"][primary]["state"] == "OPEN"
    assert result["fallback_executed"] is True
    assert result["authoritative_actual_channel"] is None


def test_unconfirmed_required_capability_blocks_and_supported_local_evidence_allows(tmp_path):
    service = CapabilityEvidenceService(
        tmp_path / "cap.sqlite3", ROOT / "config/capability_evidence_policy_v1.json")
    request = {**BASE, "request_id": "CAP-UNKNOWN",
               "capability_scope": {"required_modalities": ["text"]},
               "metadata": {**BASE["metadata"], "capability_subject_version": "v1"}}
    blocked = make(tmp_path, capability_evidence_service=service).route(request)
    assert blocked["recommended_candidate"] is None
    assert blocked["safety_governance"]["capability"]["state"] == "unknown"
    service.record_evidence({
        "evidence_id": "CAP-E1", "evidence_type": "contract_test",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "environment_id": "china_uat", "subject_id": "deepseek-v4-flash",
        "subject_version": "v1", "source": "local", "requirement": "text",
        "state": "supported"})
    allowed = make(tmp_path, capability_evidence_service=service).route(
        {**request, "request_id": "CAP-SUPPORTED"})
    assert allowed["recommended_candidate"] is not None
    assert allowed["safety_governance"]["capability"]["allowed"] is True


def test_exploration_remains_shadow_only_and_cannot_change_selection(tmp_path):
    exploration = ExplorationGovernanceService(
        tmp_path / "explore.sqlite3", ROOT / "config/exploration_policy_v1.json",
        environ={})
    baseline = make(tmp_path).route(BASE)
    governed = make(tmp_path, exploration_service=exploration).route(
        {**BASE, "request_id": "EXP-1"})
    assert governed["recommended_candidate"] == baseline["recommended_candidate"]
    state = governed["safety_governance"]["exploration"]
    assert state["mode"] == "shadow_only"
    assert state["real_execution_allowed"] is False
    assert state["selected"] is None
    assert state["execution_authorized"] is False


def test_exploration_uses_reservation_contract_but_never_executes_shadow_choice(tmp_path):
    class RecordingExploration:
        def __init__(self): self.calls = []
        def reserve_recommendation(self, **kwargs):
            self.calls.append(kwargs)
            return {"allowed": True, "selected": kwargs["candidates"][-1]["id"],
                    "mode": "shadow_only", "execution_authorized": False,
                    "network_called": False}
    exploration = RecordingExploration()
    baseline = make(tmp_path / "base").route(BASE)
    result = make(tmp_path / "governed", exploration_service=exploration).route(
        {**BASE, "request_id": "EXP-RESERVED"})
    assert len(exploration.calls) == 1
    call = exploration.calls[0]
    assert call["idempotency_key"].startswith("explore:SAFE-DEC-0:")
    assert all({"channel_id", "model_id", "health", "circuit_state"} <= set(row)
               for row in call["candidates"])
    assert result["recommended_candidate"] == baseline["recommended_candidate"]
    assert result["safety_governance"]["exploration"]["execution_authorized"] is False


def test_ambiguous_image_capability_is_fail_closed_without_name_inference(tmp_path):
    service = CapabilityEvidenceService(
        tmp_path / "cap.sqlite3", ROOT / "config/capability_evidence_policy_v1.json")
    result = make(tmp_path, capability_evidence_service=service).route({
        **BASE, "request_id": "CAP-IMAGE-AMBIGUOUS",
        "capability_scope": {"required_modalities": ["text", "image"]},
        "metadata": {**BASE["metadata"], "capability_subject_version": "v1"}})
    assert result["recommended_candidate"] is None
    capability = result["safety_governance"]["capability"]
    assert capability["state"] == "unknown" and capability["fail_closed"] is True
    assert capability["reason"] == "capability_scope_or_version_unconfirmed"


def test_capability_evidence_is_applied_per_candidate_before_strategy_scoring(tmp_path):
    service = CapabilityEvidenceService(
        tmp_path / "candidate-cap.sqlite3",
        ROOT / "config/capability_evidence_policy_v1.json")
    scheduler = make(tmp_path / "baseline")
    baseline = scheduler.route({**BASE, "request_id": "CAP-CANDIDATE-BASE"})
    supported_id = baseline["recommended_candidate"]
    assert supported_id
    subject_map = {}
    for row in scheduler.candidates:
        candidate_id = str(row["candidate_id"])
        subject_id = f"opaque-capability-subject-{candidate_id}"
        subject_map[candidate_id] = {
            "subject_id": subject_id, "subject_version": "contract-v1"}
        service.record_evidence({
            "evidence_id": f"CAP-CAND-{candidate_id}",
            "evidence_type": "contract_test",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "environment_id": "china_uat", "subject_id": subject_id,
            "subject_version": "contract-v1", "source": "local",
            "requirement": "text",
            "state": "supported" if candidate_id == supported_id else "unsupported",
        })
    result = make(tmp_path / "candidate", capability_evidence_service=service).route({
        **BASE,
        "request_id": "CAP-CANDIDATE-SCOPED",
        "capability_scope": {"required_modalities": ["text"]},
        "metadata": {**BASE["metadata"], "capability_subjects": subject_map},
    })
    capability = result["safety_governance"]["capability"]
    assert capability["assessment_scope"] == "candidate_scoped"
    assert capability["allowed_candidate_count"] == 1
    assert result["recommended_candidate"] == supported_id
    assert all(
        row["routing_block_reason"] == "required_capability_unconfirmed"
        for row in result["excluded_candidates"]
        if row["candidate_id"] != supported_id
    )


def test_video_capability_evidence_is_candidate_scoped_before_scoring(tmp_path):
    service = CapabilityEvidenceService(
        tmp_path / "video-cap.sqlite3",
        ROOT / "config/capability_evidence_policy_v1.json")
    scheduler = make(tmp_path / "video-baseline")
    baseline = scheduler.route({**BASE, "request_id": "CAP-VIDEO-BASE"})
    supported_id = baseline["recommended_candidate"]
    assert supported_id
    subject_map = {}
    for row in scheduler.candidates:
        candidate_id = str(row["candidate_id"])
        subject_id = f"opaque-video-subject-{candidate_id}"
        subject_map[candidate_id] = {
            "subject_id": subject_id, "subject_version": "contract-v1"}
        for requirement in ("text", "video_in"):
            service.record_evidence({
                "evidence_id": f"CAP-VIDEO-{candidate_id}-{requirement}",
                "evidence_type": "contract_test",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "environment_id": "china_uat", "subject_id": subject_id,
                "subject_version": "contract-v1", "source": "local",
                "requirement": requirement,
                "state": "supported" if candidate_id == supported_id else "unsupported",
                "scenario_id": "video_understanding",
                **({"mime_types": ["video/mp4"], "maximum_input_bytes": 4096}
                   if requirement == "video_in" else {}),
            })
    result = make(tmp_path / "video", capability_evidence_service=service).route({
        **BASE,
        "request_id": "CAP-VIDEO-CANDIDATE-SCOPED",
        "capability_scope": {"required_modalities": ["text", "video"]},
        "metadata": {**BASE["metadata"],
            "capability_subjects": subject_map,
            "capability_scenario_id": "video_understanding",
            "capability_request_constraints": {
                "video_in": {"mime_type": "video/mp4", "input_bytes": 1024}},
        },
    })
    capability = result["safety_governance"]["capability"]
    assert capability["assessment_scope"] == "candidate_scoped"
    assert capability["allowed_candidate_count"] == 1
    assert result["recommended_candidate"] == supported_id


def test_normal_cli_wires_persistent_circuit_service(tmp_path):
    database = tmp_path / "cli-circuit.sqlite3"
    result = subprocess.run([
        sys.executable, str(ROOT / "src" / "run_scheduler.py"),
        "--request-id", "CLI-CIRCUIT", "--model", "deepseek-v4-flash",
        "--stream", "false", "--input-tokens", "50", "--output-tokens", "20",
        "--currency", "CNY", "--strategy", "confidence_aware_v2",
        "--mode", "simulation", "--environment-id", "offline_benchmark",
        "--request-profile-id", "BENCH-V1", "--circuit-database", str(database),
        "--log-path", str(tmp_path / "decisions.jsonl")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["safety_governance"]["circuit_breakers"]
    assert all(item["state"] == "CLOSED" for item in
               payload["safety_governance"]["circuit_breakers"].values())
    exploration = payload["safety_governance"]["exploration"]
    assert exploration["mode"] == "shadow_only"
    assert exploration["execution_authorized"] is False
    assert database.exists() and payload["network_called"] is False
