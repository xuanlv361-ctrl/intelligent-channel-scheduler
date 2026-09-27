from datetime import datetime, timedelta, timezone

import pytest

from backend.tenant_security import TenantScope
from backend.uat_output_capability_service import (
    MODEL_IDS, UatOutputCapabilityError, UatOutputCapabilityService,
)


def evidence(now, model=MODEL_IDS[0], **overrides):
    value = {"model_id":model,"provider":"reviewed-provider",
      "channel_id":"unified-routing","max_context_tokens":32768,
      "max_input_tokens":24576,"max_output_tokens":8192,
      "streaming_supported":False,"multimodal_capabilities":[],
      "evidence_source":"approved-channel-metadata",
      "evidence_type":"versioned_channel_metadata","evidence_version":"2026-08-04",
      "observed_at":now.isoformat(),"expires_at":(now+timedelta(days=1)).isoformat(),
      "confidence_status":"confirmed","supersede_existing":False}
    value.update(overrides); return value


def test_six_models_are_isolated_and_pending_by_default(tmp_path):
    now=datetime(2026,8,4,tzinfo=timezone.utc)
    service=UatOutputCapabilityService(tmp_path/"cap.sqlite3",clock=lambda:now)
    service.review(evidence(now),reviewed_by="reviewer")
    assert service.resolve(MODEL_IDS[0],"unified-routing")["status"]=="confirmed"
    assert all(service.resolve(model,"unified-routing")["status"]=="capability_pending_confirmation"
               for model in MODEL_IDS[1:])


def test_conflict_and_expiry_fail_closed(tmp_path):
    now=[datetime(2026,8,4,tzinfo=timezone.utc)]
    service=UatOutputCapabilityService(tmp_path/"cap.sqlite3",clock=lambda:now[0])
    service.review(evidence(now[0]),reviewed_by="r1")
    service.review(evidence(now[0],max_output_tokens=4096,evidence_version="conflict"),reviewed_by="r2")
    assert service.resolve(MODEL_IDS[0],"unified-routing")["status"]=="capability_evidence_conflict"
    now[0]+=timedelta(days=2)
    assert service.resolve(MODEL_IDS[0],"unified-routing")["status"]=="capability_pending_confirmation"


def test_historical_output_is_only_a_lower_bound(tmp_path):
    now=datetime(2026,8,4,tzinfo=timezone.utc)
    service=UatOutputCapabilityService(tmp_path/"cap.sqlite3",clock=lambda:now)
    with pytest.raises(UatOutputCapabilityError,match="only_proves_lower_bound"):
        service.review(evidence(now,evidence_type="historical_observed_lower_bound",
          observed_output_lower_bound=11125),reviewed_by="reviewer")
    item=service.review(evidence(now,evidence_type="historical_observed_lower_bound",
      max_context_tokens=None,max_input_tokens=None,max_output_tokens=None,
      observed_output_lower_bound=11125,confidence_status="pending_confirmation"),reviewed_by="reviewer")
    assert item["observed_output_lower_bound"]==11125
    assert service.resolve(MODEL_IDS[0],"unified-routing")["status"]=="capability_pending_confirmation"


def test_tenant_and_channel_isolation(tmp_path):
    now=datetime(2026,8,4,tzinfo=timezone.utc); path=tmp_path/"cap.sqlite3"
    first=UatOutputCapabilityService(path,TenantScope("t1","w1"),clock=lambda:now)
    second=UatOutputCapabilityService(path,TenantScope("t2","w2"),clock=lambda:now)
    first.review(evidence(now),reviewed_by="reviewer")
    assert second.resolve(MODEL_IDS[0],"unified-routing")["status"]=="capability_pending_confirmation"
    assert first.resolve(MODEL_IDS[0],"other-channel")["status"]=="capability_pending_confirmation"
