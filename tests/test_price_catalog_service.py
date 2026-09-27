import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.price_catalog_service import PriceCatalogService


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "price_sync_policy_v1.json"
NOW = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def record(**patch):
    return {
        "environment_id": "china_uat",
        "model_id": "model-a",
        "channel_id": "channel-1",
        "currency": "CNY",
        "input_unit_price": "1.2500",
        "output_unit_price": "2.500",
        "billing_unit": "per_1m_tokens",
        "effective_from": "2026-07-01T00:00:00Z",
        "effective_until": None,
        **patch,
    }


def payload(*records, schema="price_catalog_v1"):
    return {"schema_version": schema, "records": list(records)}


def fake(value, *, called=False):
    def transport(_source, timeout):
        assert timeout == 3.0
        return {"payload": value, "network_called": called}
    return transport


def service(tmp_path, value=None, clock=None):
    return PriceCatalogService(tmp_path / "prices.sqlite3", POLICY,
        transport=None if value is None else fake(value), clock=clock or Clock(),
        development_mode=True)


def test_default_is_network_disabled_and_unknown_source_is_blocked(tmp_path):
    svc = service(tmp_path)
    assert svc.plan("approved_price_api") == {
        "status": "blocked", "reason": "price_network_disabled",
        "source_id": "approved_price_api", "network_called": False,
    }
    result = svc.synchronize("not-approved")
    assert result["status"] == "blocked"
    assert result["reason"] == "price_source_not_allowed"
    assert result["network_called"] is False


def test_decimal_catalog_is_atomic_versioned_and_resolvable(tmp_path):
    svc = service(tmp_path, payload(record()))
    result = svc.synchronize("contract_mock")
    assert result["status"] == "ready" and result["network_called"] is False
    price = svc.resolve(environment_id="china_uat", model_id="model-a",
                        channel_id="channel-1", currency="CNY")
    assert price["status"] == "fresh" and price["eligible"] is True
    assert price["input_unit_price"] == "1.25"
    assert price["output_unit_price"] == "2.5"
    assert price["cached_input_unit_price"] is None
    assert len(price["record_sha256"]) == 64
    with svc.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM price_catalog_versions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM price_catalog_records").fetchone()[0] == 1


def test_cached_input_token_price_is_optional_decimal_evidence(tmp_path):
    svc = service(tmp_path, payload(record(cached_input_unit_price="0.6250")))
    result = svc.synchronize("contract_mock")
    assert result["status"] == "ready"
    price = svc.resolve(environment_id="china_uat", model_id="model-a",
                        channel_id="channel-1", currency="CNY")
    assert price["cached_input_unit_price"] == "0.625"


def test_same_semantic_payload_is_idempotent_across_fetch_times(tmp_path):
    clock = Clock()
    svc = service(tmp_path, payload(record()), clock)
    first = svc.synchronize("contract_mock")
    clock.value += timedelta(hours=1)
    second = svc.synchronize("contract_mock")
    assert second["catalog_version"] == first["catalog_version"]
    assert second["idempotent"] is True
    with svc.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM price_catalog_versions").fetchone()[0] == 1


def test_empty_and_unknown_schema_fail_closed_without_partial_write(tmp_path):
    for index, bad in enumerate((payload(), payload(record(), schema="price_catalog_v2"))):
        svc = PriceCatalogService(tmp_path / f"bad-{index}.sqlite3", POLICY,
                                  transport=fake(bad), clock=Clock(),
                                  development_mode=True)
        result = svc.synchronize("contract_mock")
        assert result["status"] == "failed"
        assert result["reason"] in {"empty_price_catalog", "unknown_price_catalog_schema"}
        with svc.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM price_catalog_records").fetchone()[0] == 0


def test_duplicate_key_is_rejected_atomically(tmp_path):
    svc = service(tmp_path, payload(record(), record(input_unit_price="9")))
    result = svc.synchronize("contract_mock")
    assert result["reason"] == "duplicate_price_record"
    with svc.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM price_catalog_versions").fetchone()[0] == 0


def test_transport_failure_retains_last_known_catalog_and_marks_age(tmp_path):
    clock = Clock()
    svc = service(tmp_path, payload(record()), clock)
    version = svc.synchronize("contract_mock")["catalog_version"]
    clock.value += timedelta(days=2)

    def failed(_source, _timeout):
        raise TimeoutError("Authorization: Bearer do-not-log")

    svc.transport = failed
    result = svc.synchronize("contract_mock")
    assert result["status"] == "failed" and result["reason"] == "price_transport_failed"
    assert result["catalog"]["catalog_version"] == version
    assert result["catalog"]["status"] == "stale"
    resolved = svc.resolve(environment_id="china_uat", model_id="model-a",
                           channel_id="channel-1", currency="CNY")
    assert resolved["status"] == "stale" and resolved["eligible"] is False
    assert "do-not-log" not in json.dumps(svc.audit())


def test_explicit_effective_expiration_and_age_expiration_fail_closed(tmp_path):
    clock = Clock()
    svc = service(tmp_path, payload(record(effective_until="2026-08-02T00:00:00Z")), clock)
    svc.synchronize("contract_mock")
    clock.value += timedelta(days=1, seconds=1)
    assert svc.resolve(environment_id="china_uat", model_id="model-a",
        channel_id="channel-1", currency="CNY")["status"] == "expired"
    clock2 = Clock()
    aged = PriceCatalogService(tmp_path / "aged.sqlite3", POLICY,
        transport=fake(payload(record())), clock=clock2, development_mode=True)
    aged.synchronize("contract_mock")
    clock2.value += timedelta(days=8)
    assert aged.catalog_status("contract_mock")["status"] == "expired"


def test_future_effective_price_is_not_scheduler_eligible(tmp_path):
    svc = service(tmp_path, payload(record(
        effective_from="2026-08-02T00:00:00Z",
        effective_until="2026-08-03T00:00:00Z")))
    svc.synchronize("contract_mock")
    result = svc.resolve(environment_id="china_uat", model_id="model-a",
                         channel_id="channel-1", currency="CNY")
    assert result["status"] == "not_yet_effective"
    assert result["eligible"] is False
    assert result["reason"] == "price_not_yet_effective"


def test_currency_isolation_and_unknown_price_fail_closed(tmp_path):
    svc = service(tmp_path, payload(record(currency="CNY"),
        record(currency="USD", input_unit_price="0.1", output_unit_price="0.2")))
    svc.synchronize("contract_mock")
    usd = svc.resolve(environment_id="china_uat", model_id="model-a",
                      channel_id="channel-1", currency="USD")
    assert usd["input_unit_price"] == "0.1"
    missing = svc.resolve(environment_id="china_uat", model_id="model-a",
                          channel_id="channel-1", currency="EUR")
    assert missing["status"] == "unavailable" and missing["eligible"] is False


def test_sensitive_or_additional_fields_are_rejected_and_redacted(tmp_path):
    bad = record(api_key="plaintext-must-never-persist")  # secret-scan: allow
    svc = service(tmp_path, payload(bad))
    result = svc.synchronize("contract_mock")
    assert result["reason"] == "invalid_price_record_schema"
    serialized = (tmp_path / "prices.sqlite3").read_bytes().decode(errors="ignore")
    assert "plaintext-must-never-persist" not in serialized
    assert "api_key" not in json.dumps(svc.audit()).casefold()


def test_injected_transport_reports_logical_network_use_without_real_io(tmp_path):
    calls = []

    def transport(source, timeout):
        calls.append((dict(source), timeout))
        return {"payload": payload(record()), "network_called": True}

    svc = PriceCatalogService(tmp_path / "prices.sqlite3", POLICY,
                              transport=transport, clock=Clock(),
                              development_mode=True)
    result = svc.synchronize("approved_price_api", allow_network=True)
    assert result["status"] == "ready" and result["network_called"] is True
    assert len(calls) == 1


def test_model_level_price_version_is_immutable_and_does_not_invent_channel(tmp_path):
    svc = service(tmp_path)
    records = [{
        "model_id": "model-a", "requested_model": "model-a",
        "billed_model": "model-a-billed", "provider": "provider-a",
        "currency": "CNY", "input_price_per_million_tokens": "1.25",
        "cached_input_price_per_million_tokens": "0.25",
        "output_price_per_million_tokens": "5", "per_request_price": None,
        "pricing_unit": "per_1m_tokens", "confidence": "observed_repeated",
        "verification_status": "confirmed",
    }, {
        "model_id": "model-pending", "requested_model": "model-pending",
        "billed_model": None, "provider": None, "currency": "CNY",
        "input_price_per_million_tokens": None,
        "cached_input_price_per_million_tokens": None,
        "output_price_per_million_tokens": None, "per_request_price": None,
        "pricing_unit": "per_1m_tokens", "confidence": "insufficient_evidence",
        "verification_status": "pending_confirmation",
    }]
    first = svc.import_model_price_version(price_version="price-v1",
        environment_id="china_uat", effective_from="2026-08-01T00:00:00Z",
        effective_to=None, source_type="historical_log_detail",
        source_reference="standardized_call_logs:abc", records=records)
    assert first["confirmed_count"] == 1 and first["pending_count"] == 1
    assert first["idempotent"] is False
    second = svc.import_model_price_version(price_version="price-v1",
        environment_id="china_uat", effective_from="2026-08-01T00:00:00Z",
        effective_to=None, source_type="historical_log_detail",
        source_reference="standardized_call_logs:abc", records=records)
    assert second["idempotent"] is True
    prices = svc.list_model_prices("price-v1")
    assert prices[0]["model_id"] == "model-a"
    assert "channel_id" not in prices[0]
    changed = [dict(records[0], output_price_per_million_tokens="6"), records[1]]
    import pytest
    from backend.price_catalog_service import PriceCatalogError
    with pytest.raises(PriceCatalogError, match="immutable_price_version_conflict"):
        svc.import_model_price_version(price_version="price-v1",
            environment_id="china_uat", effective_from="2026-08-01T00:00:00Z",
            effective_to=None, source_type="historical_log_detail",
            source_reference="standardized_call_logs:abc", records=changed)
