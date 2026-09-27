import json

import pytest

from backend.log_schema_adapters import (
    SchemaAdapterRegistry, SchemaObservationError, observe_schema,
)
from backend.realtime_log_sync_service import RealtimeLogSyncService


SOURCE = "https://uat.weimeta.cn/api/log/self"


class Runtime:
    def active(self, environment, key):
        return None


def empty_envelope():
    return {
        "success": True,
        "message": "synthetic-safe-message",
        "data": {"items": [], "page": 1, "page_size": 10, "total": 0},
    }


def reviewed_record_envelope():
    return {
        "success": True,
        "message": "ok",
        "data": {
            "items": [{
                "id": 991,
                "created_at": 1785286800,
                "model_name": "deepseek-v4-flash",
                "quota": 107,
                "prompt_tokens": 114,
                "completion_tokens": 162,
                "request_id": "REQ-PROVIDER-COST",
                "use_time": 3,
                "is_stream": False,
                "other": json.dumps({"cache_tokens": 4}),
            }],
            "page": 1, "page_size": 10, "total": 1,
        },
    }


def service(tmp_path):
    execution = tmp_path / "executions.jsonl"
    execution.write_text("", encoding="utf-8")
    return RealtimeLogSyncService(
        tmp_path / "database.sqlite3", execution, Runtime())


def persistent_syncing(svc):
    job = svc.create_job(
        "china_uat", "2026-07-29T00:00:00Z",
        "2026-07-29T02:00:00Z", "Asia/Shanghai", 5, 20)
    svc.update_job(
        job["sync_job_id"], state="waiting_for_manual_login",
        current_safe_url=job["safe_log_page_url"])
    job = svc.confirm_login(job["sync_job_id"], True)
    return svc.update_job(
        job["sync_job_id"], state="synchronizing",
        sync_mode="persistent_encrypted_session",
        persistent_session_id="PSESSION-SYNTHETIC")


def test_actual_redacted_empty_shape_selects_reviewed_envelope_adapter(tmp_path):
    svc = service(tmp_path)
    observation = observe_schema(empty_envelope(), "china_uat", SOURCE)
    adapter = svc.schema_registry.select(observation)
    assert observation.fingerprint == (
        "6dfe81cfce092b5342527ad94ee2e3017ebd1aef64baa54178720d4b84500a2b")
    assert adapter["schema_adapter_id"] == "china_uat_billing_empty_envelope"
    records, pagination = SchemaAdapterRegistry.extract(
        adapter, empty_envelope())
    assert records == []
    assert pagination == {"page": 1, "page_size": 10, "total": 0}


def test_known_empty_envelope_is_not_miscounted_as_a_record(tmp_path):
    svc = service(tmp_path)
    job = persistent_syncing(svc)
    result = svc.ingest(job["sync_job_id"], empty_envelope(), SOURCE)
    assert result["schema_mapping_required"] is False
    assert result["schema_status"] == "adapter_ready_empty"
    assert result["collected_count"] == 0
    assert result["inserted_count"] == 0
    assert result["rejected_count"] == 0
    with svc.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM realtime_log_records").fetchone()[0] == 0
        audit = db.execute(
            "SELECT * FROM realtime_log_schema_audit").fetchone()
        stored = db.execute(
            "SELECT sanitized_payload FROM realtime_log_evidence").fetchone()[0]
    assert audit["selection_status"] == "selected"
    assert audit["schema_adapter_id"] == "china_uat_billing_empty_envelope"
    assert json.loads(audit["pagination_json"]) == {
        "page": 1, "page_size": 10, "total": 0}
    adapter_metadata = json.loads(audit["adapter_metadata_json"])
    assert adapter_metadata["reviewed_at"] == "2026-07-30T07:08:36Z"
    assert adapter_metadata["source_reliability_classification"] == (
        "measured_uat_empty_envelope_only")
    stored_object = json.loads(stored)
    assert stored_object["schema_fingerprint"] == (
        "6dfe81cfce092b5342527ad94ee2e3017ebd1aef64baa54178720d4b84500a2b")
    assert stored_object["sync_job_id"] == job["sync_job_id"]


def test_reviewed_nonempty_billing_log_maps_displayed_cost(tmp_path):
    svc = service(tmp_path)
    job = persistent_syncing(svc)
    envelope = reviewed_record_envelope()
    observation = observe_schema(envelope, "china_uat", SOURCE)
    adapter = svc.schema_registry.select(observation)
    assert adapter["schema_adapter_id"] == "china_uat_billing_records"
    result = svc.ingest(
        job["sync_job_id"], envelope, SOURCE,
        billing_context={
            "quota_per_unit": 500000,
            "quota_display_type": "CNY",
            "usd_exchange_rate": 7.3,
        })
    assert result["inserted_count"] == 1
    with svc.connect() as db:
        normalized = json.loads(db.execute(
            "SELECT normalized_json FROM realtime_log_records").fetchone()[0])
    assert normalized["actual_cost"] == "0.0015622"
    assert normalized["converted_amount"] == "0.0015622"
    assert normalized["display_amount"] == "0.001562"
    assert normalized["quota_conversion_rate"] == "0.0000146"
    assert normalized["cost_precision"] == 6
    assert normalized["rounding_mode"] == "ROUND_HALF_UP"
    assert normalized["currency"] == "CNY"
    assert normalized["input_tokens"] == 114
    assert normalized["output_tokens"] == 162
    assert normalized["cached_tokens"] == 4
    assert normalized["total_tokens"] == 276
    assert normalized["latency_ms"] == 3000
    assert normalized["platform_created_at"].endswith("+00:00")


def test_provider_numeric_token_identifier_is_non_secret_and_not_projected(tmp_path):
    svc = service(tmp_path)
    job = persistent_syncing(svc)
    envelope = reviewed_record_envelope()
    envelope["data"]["items"][0].update({
        "token_id": 123, "user_uid": 456, "channel": 7,
    })
    result = svc.ingest(job["sync_job_id"], envelope, SOURCE,
                        billing_context={"quota_per_unit": 500000,
                                         "quota_display_type": "CNY",
                                         "usd_exchange_rate": 7.3})
    assert result["inserted_count"] == 1
    with svc.connect() as db:
        normalized = db.execute(
            "SELECT normalized_json FROM realtime_log_records").fetchone()[0]
    assert "token_id" not in normalized
    assert "user_uid" not in normalized


def test_quota_without_reviewed_billing_context_is_not_zero(tmp_path):
    svc = service(tmp_path)
    job = persistent_syncing(svc)
    result = svc.ingest(
        job["sync_job_id"], reviewed_record_envelope(), SOURCE)
    assert result["inserted_count"] == 1
    with svc.connect() as db:
        normalized = json.loads(db.execute(
            "SELECT normalized_json FROM realtime_log_records").fetchone()[0])
    assert normalized["actual_cost"] is None
    assert normalized["currency"] is None


def test_nonempty_unknown_record_shape_fails_closed_with_per_record_reason(tmp_path):
    svc = service(tmp_path)
    job = persistent_syncing(svc)
    envelope = empty_envelope()
    envelope["data"]["items"] = [
        {"synthetic_unknown_a": "x"},
        {"synthetic_unknown_a": "y"},
    ]
    result = svc.ingest(job["sync_job_id"], envelope, SOURCE)
    assert result["schema_mapping_required"] is True
    assert result["last_rejection_reason"] == "schema_mapping_required"
    assert result["collected_count"] == 2
    assert result["rejected_count"] == 2
    assert result["inserted_count"] == 0
    with svc.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM realtime_log_rejections").fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM realtime_log_records").fetchone()[0] == 0
        audit = db.execute(
            "SELECT * FROM realtime_log_schema_audit").fetchone()
    assert audit["selection_status"] == "rejected"
    assert audit["schema_fingerprint"] == result["schema_fingerprint"]


def test_credential_like_fields_are_rejected_and_values_never_persist(tmp_path):
    svc = service(tmp_path)
    job = persistent_syncing(svc)
    envelope = empty_envelope()
    envelope["Authorization"] = "Bearer NEVER-PERSIST-THIS"
    result = svc.ingest(job["sync_job_id"], envelope, SOURCE)
    assert result["last_rejection_reason"] == "credential_like_field_present"
    with svc.connect() as db:
        rendered = json.dumps([
            tuple(row) for row in db.execute(
                "SELECT sanitized_payload,schema_summary "
                "FROM realtime_log_evidence")
        ])
    assert "NEVER-PERSIST-THIS" not in rendered
    assert "Authorization" not in rendered


def test_shape_drift_additional_fields_wrong_types_and_missing_list_fail_closed(
        tmp_path):
    svc = service(tmp_path)
    for envelope in (
        {**empty_envelope(), "unexpected": {"nested": True}},
        {**empty_envelope(), "success": "true"},
        {**empty_envelope(), "data": {
            "page": 1, "page_size": 10, "total": 0}},
        {**empty_envelope(), "data": {
            "items": None, "page": 1, "page_size": 10, "total": 0}},
    ):
        observation = observe_schema(envelope, "china_uat", SOURCE)
        assert svc.schema_registry.select(observation) is None


def test_shape_bounds_and_source_path_are_part_of_fingerprint(tmp_path):
    svc = service(tmp_path)
    with __import__("pytest").raises(
            SchemaObservationError, match="field_limit"):
        observe_schema(
            {f"field_{index}": index for index in range(129)},
            "china_uat", SOURCE)
    domestic = observe_schema(empty_envelope(), "china_uat", SOURCE)
    other_path = observe_schema(
        empty_envelope(), "china_uat",
        "https://uat.weimeta.cn/api/log/other")
    assert domestic.fingerprint != other_path.fingerprint
    assert svc.schema_registry.select(other_path) is None


def test_formula_like_values_do_not_enter_persisted_shape_metadata(tmp_path):
    svc = service(tmp_path)
    job = persistent_syncing(svc)
    envelope = empty_envelope()
    envelope["message"] = "=HYPERLINK(\"https://evil.test\")"
    result = svc.ingest(job["sync_job_id"], envelope, SOURCE)
    assert result["schema_mapping_required"] is False
    with svc.connect() as db:
        evidence = db.execute(
            "SELECT sanitized_payload,schema_summary "
            "FROM realtime_log_evidence").fetchone()
    assert "HYPERLINK" not in evidence["sanitized_payload"]
    assert "HYPERLINK" not in evidence["schema_summary"]


def test_temporary_domestic_mode_cannot_bypass_registry(tmp_path):
    svc = service(tmp_path)
    job = svc.create_job(
        "china_uat", "2026-07-29T00:00:00Z",
        "2026-07-29T02:00:00Z", "Asia/Shanghai", 5, 20)
    svc.update_job(
        job["sync_job_id"], state="waiting_for_manual_login",
        current_safe_url=job["safe_log_page_url"])
    job = svc.confirm_login(job["sync_job_id"], True)
    payload = empty_envelope()
    payload["data"]["items"] = [{
        "id": "synthetic", "cost": 1.2, "channel": "must-not-infer"}]
    result = svc.ingest(job["sync_job_id"], payload, SOURCE)
    assert result["schema_mapping_required"] is True
    assert result["inserted_count"] == 0
    with svc.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM realtime_log_records").fetchone()[0] == 0


def test_safe_evidence_hash_matches_stored_content_and_is_job_scoped(tmp_path):
    svc = service(tmp_path)
    evidence = []
    for _ in range(2):
        job = persistent_syncing(svc)
        svc.ingest(job["sync_job_id"], empty_envelope(), SOURCE)
        svc.update_job(job["sync_job_id"], state="completed")
    with svc.connect() as db:
        evidence = db.execute("""SELECT evidence_sha256,sync_job_id,
          sanitized_payload FROM realtime_log_evidence
          ORDER BY sync_job_id""").fetchall()
    assert len(evidence) == 2
    for row in evidence:
        assert __import__("hashlib").sha256(
            row["sanitized_payload"].encode()).hexdigest() == \
            row["evidence_sha256"]
        assert json.loads(
            row["sanitized_payload"])["sync_job_id"] == row["sync_job_id"]


@pytest.mark.parametrize("field", [
    "client_secret", "access-token", "authorization_value",
    "session_cookie_value", "customCredential",
])
def test_credential_like_field_variants_fail_closed(tmp_path, field):
    svc = service(tmp_path)
    job = persistent_syncing(svc)
    payload = empty_envelope()
    payload[field] = "NEVER-PERSIST"
    result = svc.ingest(job["sync_job_id"], payload, SOURCE)
    assert result["last_rejection_reason"] == "credential_like_field_present"
    with svc.connect() as db:
        stored = json.dumps([
            tuple(row) for row in db.execute(
                "SELECT sanitized_payload,schema_summary "
                "FROM realtime_log_evidence")])
    assert "NEVER-PERSIST" not in stored
    assert field not in stored
