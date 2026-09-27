from __future__ import annotations

import sys
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from capability_evidence_service import CapabilityEvidenceService  # noqa: E402

NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)


def record(svc, identifier, state, *, source="live", evidence_type="measured", age=0,
           version="v1", requirement="text"):
    return svc.record_evidence({"evidence_id": identifier, "evidence_type": evidence_type,
        "observed_at": (NOW - timedelta(seconds=age)).isoformat(), "environment_id": "prod",
        "subject_id": "opaque-model", "subject_version": version, "source": source,
        "requirement": requirement, "state": state})


def test_supported_unsupported_unknown_and_conflict(tmp_path):
    svc = CapabilityEvidenceService(tmp_path / "caps.db",
        ROOT / "config/capability_evidence_policy_v1.json", clock=lambda: NOW)
    assert svc.resolve(environment_id="prod", subject_id="name-says-image", subject_version="v1",
                       requirement="image_in")["state"] == "unknown"
    record(svc, "yes", "supported")
    assert svc.resolve(environment_id="prod", subject_id="opaque-model", subject_version="v1",
                       requirement="text")["state"] == "supported"
    record(svc, "no", "unsupported")
    result = svc.resolve(environment_id="prod", subject_id="opaque-model", subject_version="v1",
                         requirement="text")
    assert result["state"] == "conflicting" and result["fail_closed"]


def test_stale_mock_legacy_and_version_mismatch_fail_closed(tmp_path):
    svc = CapabilityEvidenceService(tmp_path / "caps.db",
        ROOT / "config/capability_evidence_policy_v1.json", clock=lambda: NOW)
    record(svc, "stale", "supported", age=2592001)
    record(svc, "mock", "supported", source="mock")
    legacy = record(svc, "legacy", "supported", evidence_type="legacy_assumption")
    assert legacy["state"] == "unknown" and not legacy["verified"]
    for version in ("v1", "v2"):
        result = svc.resolve(environment_id="prod", subject_id="opaque-model",
                             subject_version=version, requirement="text")
        assert result["state"] == "unknown" and result["fail_closed"]


def test_fixed_requirements_must_all_have_evidence(tmp_path):
    svc = CapabilityEvidenceService(tmp_path / "caps.db",
        ROOT / "config/capability_evidence_policy_v1.json", clock=lambda: NOW)
    record(svc, "text", "supported")
    result = svc.assess_requirements(environment_id="prod", subject_id="opaque-model",
        subject_version="v1", requirements={"text": True, "stream": True})
    assert not result["allowed"]
    assert result["requirements"]["stream"]["state"] == "unknown"

def test_fixed_scenario_mime_size_context_and_image_output_bounds(tmp_path):
    svc=CapabilityEvidenceService(tmp_path/"caps.db",ROOT/"config/capability_evidence_policy_v1.json",clock=lambda:NOW)
    record(svc,"text","supported")
    svc.record_evidence({"evidence_id":"img","evidence_type":"contract_test","observed_at":NOW.isoformat(),
      "environment_id":"prod","subject_id":"opaque-model","subject_version":"v1","source":"live",
      "requirement":"image_out","state":"supported","scenario_id":"image_generation",
      "mime_types":["image/png"],"maximum_output_bytes":1024})
    good=svc.resolve(environment_id="prod",subject_id="opaque-model",subject_version="v1",requirement="image_out",
      scenario_id="image_generation",request_constraints={"mime_type":"image/png","output_bytes":1000})
    bad=svc.resolve(environment_id="prod",subject_id="opaque-model",subject_version="v1",requirement="image_out",
      scenario_id="image_generation",request_constraints={"mime_type":"image/jpeg","output_bytes":2048})
    assert good["state"]=="supported" and bad["state"]=="unknown"
    assert svc.resolve(environment_id="prod",subject_id="opaque-model",subject_version="v1",requirement="context",
      scenario_id="long_context",request_constraints={"context_tokens":1})["state"]=="unknown"

def test_video_capability_is_candidate_scoped_and_mime_bounded(tmp_path):
    svc=CapabilityEvidenceService(tmp_path/"caps.db",ROOT/"config/capability_evidence_policy_v1.json",clock=lambda:NOW)
    svc.record_evidence({"evidence_id":"video","evidence_type":"contract_test","observed_at":NOW.isoformat(),
      "environment_id":"prod","subject_id":"opaque-model","subject_version":"v1","source":"live",
      "requirement":"video_in","state":"supported","scenario_id":"video_understanding",
      "mime_types":["video/mp4"],"maximum_input_bytes":2048})
    good=svc.resolve(environment_id="prod",subject_id="opaque-model",subject_version="v1",requirement="video_in",
      scenario_id="video_understanding",request_constraints={"mime_type":"video/mp4","input_bytes":1024})
    wrong_mime=svc.resolve(environment_id="prod",subject_id="opaque-model",subject_version="v1",requirement="video_in",
      scenario_id="video_understanding",request_constraints={"mime_type":"video/x-msvideo","input_bytes":1024})
    wrong_candidate=svc.resolve(environment_id="prod",subject_id="another-model",subject_version="v1",requirement="video_in",
      scenario_id="video_understanding",request_constraints={"mime_type":"video/mp4","input_bytes":1024})
    assert good["state"]=="supported" and not good["fail_closed"]
    assert wrong_mime["state"]=="unknown" and wrong_mime["fail_closed"]
    assert wrong_candidate["state"]=="unknown" and wrong_candidate["fail_closed"]

def test_expiry_revocation_supersession_and_protected_detail_rejection(tmp_path):
    svc=CapabilityEvidenceService(tmp_path/"caps.db",ROOT/"config/capability_evidence_policy_v1.json",clock=lambda:NOW)
    record(svc,"old","supported")
    new={"evidence_id":"new","evidence_type":"measured","observed_at":NOW.isoformat(),"environment_id":"prod",
      "subject_id":"opaque-model","subject_version":"v1","source":"live","requirement":"text","state":"unsupported",
      "supersedes_evidence_id":"old","valid_until":(NOW+timedelta(seconds=10)).isoformat()}
    svc.record_evidence(new)
    assert svc.resolve(environment_id="prod",subject_id="opaque-model",subject_version="v1",requirement="text")["state"]=="unsupported"
    svc.revoke_evidence("new",reason="invalid measurement")
    assert svc.resolve(environment_id="prod",subject_id="opaque-model",subject_version="v1",requirement="text")["state"]=="unknown"
    dangerous={**new,"evidence_id":"secret","supersedes_evidence_id":None,"details":{"nested":{"api_key":"x"}}}
    with pytest.raises(Exception,match="protected_evidence"):svc.record_evidence(dangerous)
