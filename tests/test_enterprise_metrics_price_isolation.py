from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.historical_evidence_import_service import HistoricalEvidenceImportService
from backend.incremental_metrics_service import IncrementalMetricsService, MetricsError
from backend.price_catalog_service import PriceCatalogService
from backend.price_sync_supervisor import PriceSyncSupervisor
from backend.security.authorization import AuthorizationError, AuthorizationService
from backend.security.config import EnterpriseIdentityConfig
from backend.security.identity import PrincipalResolver, VerifiedIdentity
from backend.security.principal import PrincipalType
from src.incremental_metrics_provider import IncrementalMetricProvider


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)


def security(role: str, tenant: str):
    authorization = AuthorizationService(ROOT / "config/rbac_permissions_v1.json")
    config = EnterpriseIdentityConfig.load(ROOT / "config/enterprise_identity_v1.json")
    principal = PrincipalResolver(config, authorization).resolve_verified(
        VerifiedIdentity(
            principal_id=f"service:{role}:{tenant}",
            principal_type=PrincipalType.SERVICE,
            tenant_id=tenant,
            workspace_id="workspace-main",
            roles=(role,),
            authn_method="enterprise_test_service_adapter",
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
            session_id=None,
            token_id=f"jti:{role}:{tenant}",
            is_development_identity=False,
            security_version=1,
        )
    )
    return principal, authorization


def metric_event(evidence_id: str, *, tenant_hint: str | None = None):
    return {
        "evidence_id": evidence_id,
        "environment_id": "china_uat",
        "requested_model": "model-a",
        "actual_channel": "channel-a",
        "request_profile_id": "P01",
        "observed_at": (NOW - timedelta(seconds=1)).isoformat(),
        "success": True,
        "http_status": 200,
        "latency_ms": 10,
        "stream": False,
        "currency": "CNY",
        "source_type": "measured_unified_uat",
        # Untrusted payload hints must never select the authoritative scope.
        "tenant_id": tenant_hint,
        "workspace_id": "forged-workspace",
    }


def metrics(path: Path, tenant: str) -> IncrementalMetricsService:
    principal, authorization = security("metrics_service", tenant)
    return IncrementalMetricsService(
        path, principal=principal, authorization=authorization
    )


def price_payload(value: str = "1"):
    return {
        "schema_version": "price_catalog_v1",
        "records": [{
            "environment_id": "china_uat",
            "model_id": "model-a",
            "channel_id": "channel-a",
            "currency": "CNY",
            "input_unit_price": value,
            "output_unit_price": "2",
            "billing_unit": "per_1m_tokens",
            "effective_from": "2026-07-01T00:00:00Z",
            "effective_until": None,
        }],
    }


def price(path: Path, tenant: str) -> PriceCatalogService:
    principal, authorization = security("price_sync_service", tenant)
    return PriceCatalogService(
        path,
        ROOT / "config/price_sync_policy_v1.json",
        principal=principal,
        authorization=authorization,
        transport=lambda _source, _timeout: {
            "payload": price_payload(), "network_called": False
        },
        clock=lambda: NOW,
    )


def test_missing_identity_and_wrong_service_role_fail_closed(tmp_path):
    with pytest.raises(MetricsError, match="enterprise_service_identity_required"):
        IncrementalMetricsService(tmp_path / "missing.sqlite3")

    principal, authorization = security("price_sync_service", "tenant-a")
    forged = IncrementalMetricsService(
        tmp_path / "forged.sqlite3",
        principal=principal,
        authorization=authorization,
    )
    with pytest.raises(AuthorizationError, match="permission_denied"):
        forged.ingest([metric_event("e-1")], as_of=NOW)


def test_same_evidence_id_and_snapshots_are_tenant_scoped(tmp_path):
    path = tmp_path / "metrics.sqlite3"
    tenant_a = metrics(path, "tenant-a")
    tenant_b = metrics(path, "tenant-b")

    tenant_a.ingest([metric_event("shared", tenant_hint="tenant-b")], as_of=NOW)
    assert tenant_b.list_snapshots()["event_count"] == 0
    tenant_b.ingest([metric_event("shared", tenant_hint="tenant-a")], as_of=NOW)

    a = tenant_a.list_snapshots(window="1h")
    b = tenant_b.list_snapshots(window="1h")
    assert a["event_count"] == b["event_count"] == 1
    assert a["items"][0]["snapshot_id"] == b["items"][0]["snapshot_id"]
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM metric_evidence_events WHERE evidence_id='shared'"
        ).fetchone()[0] == 2
        assert db.execute(
            """SELECT COUNT(DISTINCT tenant_id || ':' || workspace_id)
               FROM statistical_confidence_snapshots"""
        ).fetchone()[0] == 2


def test_scheduler_provider_cannot_consume_other_tenant_snapshot(tmp_path):
    path = tmp_path / "metrics.sqlite3"
    metrics(path, "tenant-a").ingest([metric_event("only-a")], as_of=NOW)
    scheduler_b, authorization = security("scheduler_service", "tenant-b")
    provider = IncrementalMetricProvider(
        path, principal=scheduler_b, authorization=authorization, window="1h"
    )
    candidates, context = provider.apply(
        [{"channel_id": "channel-a", "availability_status": "available"}],
        environment_id="china_uat",
        requested_model="model-a",
        stream=False,
        request_profile_id="P01",
    )
    assert candidates[0]["availability_status"] == "unavailable"
    assert context["snapshot_ids"] == []


def test_price_catalog_idempotency_and_schedule_are_tenant_scoped(tmp_path):
    path = tmp_path / "prices.sqlite3"
    tenant_a = price(path, "tenant-a")
    tenant_b = price(path, "tenant-b")
    result_a = tenant_a.synchronize("contract_mock")
    result_b = tenant_b.synchronize("contract_mock")
    assert result_a["catalog_version"] == result_b["catalog_version"]
    assert result_a["idempotent"] is result_b["idempotent"] is False

    schedule_a = PriceSyncSupervisor(tenant_a, clock=lambda: NOW)
    schedule_b = PriceSyncSupervisor(tenant_b, clock=lambda: NOW)
    schedule_a.configure("contract_mock", enabled=True)
    assert schedule_b.status("contract_mock")["state"] == "never_configured"
    schedule_b.configure("contract_mock", enabled=True)
    with sqlite3.connect(path) as db:
        assert db.execute(
            """SELECT COUNT(*) FROM price_catalog_versions
               WHERE catalog_version=?""", (result_a["catalog_version"],)
        ).fetchone()[0] == 2
        assert db.execute(
            "SELECT COUNT(*) FROM price_sync_schedule WHERE source_id='contract_mock'"
        ).fetchone()[0] == 2


def test_historical_import_same_batch_identity_is_allowed_across_tenants(tmp_path):
    path = tmp_path / "history.sqlite3"
    events = [
        {
            "evidence_id": f"e-{index}",
            "observed_at": NOW.isoformat(),
            "requested_model": "model-a",
            "actual_channel": "channel-a",
        }
        for index in range(60)
    ]
    results = []
    for tenant in ("tenant-a", "tenant-b"):
        principal, authorization = security("collector_service", tenant)
        service = HistoricalEvidenceImportService(
            path, principal=principal, authorization=authorization
        )
        results.append(service.import_reconciliation(
            events,
            evidence_manifest_sha256="A" * 64,
            reconciliation_result_sha256="B" * 64,
            imported_at=NOW,
        ))
    assert results[0]["batch_id"] == results[1]["batch_id"]
    assert results[0]["idempotent"] is results[1]["idempotent"] is False
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 2
