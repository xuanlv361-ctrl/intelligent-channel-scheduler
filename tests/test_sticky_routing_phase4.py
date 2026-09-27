from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.capability_evidence_service import CapabilityEvidenceService
from backend.sticky_routing_routes import build_sticky_routing_router
from backend.sticky_routing_service import StickyRoutingService
from sticky_routing import (
    GATE_ORDER, GATE_PASS_STATES, StickyRoutingError, derive_route_scope,
)
from sticky_eligibility import ControlledStickyEligibilityProvider
from channel_adapter import MockChannelAdapter
from decision_logger import DecisionLogger
from retry_policy import RetryPolicy
from scheduler import Scheduler


HMAC_TEST_MATERIAL = "phase-four-test-key-material-32-bytes"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


def policy_file(tmp_path: Path, **changes) -> Path:
    policy = json.loads(
        (ROOT / "config" / "sticky_routing_policy_v1.json").read_text(encoding="utf-8"))
    policy.update({"enabled": True, "ttl_seconds": 10,
                   "maximum_total_duration_seconds": 25, **changes})
    path = tmp_path / f"policy-{len(list(tmp_path.glob('policy-*')))}.json"
    path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
    return path


def service(tmp_path: Path, clock: Clock | None = None, **policy_changes):
    clock = clock or Clock()
    return StickyRoutingService(
        tmp_path / "sticky.sqlite3", policy_file(tmp_path, **policy_changes),
        secret=HMAC_TEST_MATERIAL, key_id="test-key-v1", clock=clock), clock


def gates(**changes):
    value = {name: True for name in GATE_ORDER}
    value.update(changes)
    return value


def resolve(svc: StickyRoutingService, decision="D1", affinity="opaque-A", **changes):
    values = {
        "decision_id": decision, "affinity": affinity,
        "environment_id": "china_uat", "requested_model": "model-a",
        "capability_scope": None, "stream": False, "gate_results": gates(),
        "mode": "simulation",
        "eligible_channel_ids": ["19", "48"],
        "evidence": {"metric_snapshot_id": "MET-1",
                     "confidence_snapshot_id": "CONF-1",
                     "evidence_ids": ["EV-1"]},
        "is_mock": True,
    }
    values.update(changes)
    return svc.resolve(**values)


def create(svc: StickyRoutingService, decision="D1", affinity="opaque-A", **changes):
    creation_decision = f"CREATE-{decision}"
    values = {
        "decision_id": creation_decision, "affinity": affinity,
        "environment_id": "china_uat", "requested_model": "model-a",
        "capability_scope": None, "stream": False,
        "selected_channel": "19", "evidence": {"evidence_ids": ["EV-1"]},
        "is_mock": True,
    }
    values.update(changes)
    resolve_changes = {
        key: values[key] for key in (
            "environment_id", "requested_model", "capability_scope", "stream")
    }
    resolve(
        svc, decision=creation_decision, affinity=affinity,
        eligible_channel_ids=[values["selected_channel"], "48"],
        **resolve_changes)
    return svc.create_binding(**values)


def test_deterministic_hmac_and_scope_isolation():
    base = dict(
        secret=HMAC_TEST_MATERIAL, key_id="key-1", affinity="opaque",
        environment_id="china_uat", requested_model="model-a",
        capability_scope=None, stream=False,
        policy_version="p1", configuration_version="c1",
    )
    first = derive_route_scope(**base)
    assert first == derive_route_scope(**base)
    assert first.route_key_fingerprint != derive_route_scope(
        **{**base, "environment_id": "overseas"}).route_key_fingerprint
    assert first.route_key_fingerprint != derive_route_scope(
        **{**base, "requested_model": "model-b"}).route_key_fingerprint
    assert first.route_key_fingerprint != derive_route_scope(
        **{**base, "stream": True,
           "capability_scope": {"required_modalities": ["text", "image"]}}
    ).route_key_fingerprint
    assert "opaque" not in first.route_key_fingerprint


def test_invalid_affinity_and_weak_key_are_rejected():
    base = dict(
        secret=HMAC_TEST_MATERIAL, key_id="key-1", affinity="opaque",
        environment_id="china_uat", requested_model="model-a",
        capability_scope=None, stream=False,
        policy_version="p1", configuration_version="c1",
    )
    with pytest.raises(StickyRoutingError):
        derive_route_scope(**{**base, "affinity": "bad\nvalue"})
    with pytest.raises(StickyRoutingError):
        derive_route_scope(**{**base, "secret": "too-short"})


def test_ttl_hit_slides_but_never_exceeds_maximum(tmp_path):
    svc, clock = service(tmp_path)
    assert resolve(svc)["outcome"] == "MISS"
    created = create(svc)["binding"]
    maximum = created["maximum_expires_at"]
    clock.value += timedelta(seconds=8)
    hit = resolve(svc, decision="D2")
    assert hit["outcome"] == "HIT"
    assert hit["binding"]["expires_at"] > created["expires_at"]
    clock.value += timedelta(seconds=9)
    hit = resolve(svc, decision="D3")
    assert hit["binding"]["expires_at"] == maximum
    clock.value = datetime.fromisoformat(maximum.replace("Z", "+00:00"))
    assert resolve(svc, decision="D4")["outcome"] == "MISS"
    assert svc.list(state="EXPIRED")["total"] == 1


def test_clock_rollback_never_reduces_last_use_or_expiry(tmp_path):
    svc, clock = service(tmp_path)
    created = create(svc)["binding"]
    clock.value += timedelta(seconds=5)
    first = resolve(svc, decision="CLOCK-1")["binding"]
    clock.value -= timedelta(seconds=20)
    second = resolve(svc, decision="CLOCK-2")["binding"]
    assert second["last_used_at"] == first["last_used_at"]
    assert second["expires_at"] == first["expires_at"]
    assert second["expires_at"] >= created["expires_at"]


@pytest.mark.parametrize("gate,reason", [
    ("security_authorization", "security_denied"),
    ("capability", "capability_changed"),
    ("environment", "environment_changed"),
    ("budget", "budget_exhausted"),
    ("health", "health_below_threshold"),
    ("statistical_confidence", "confidence_insufficient"),
    ("freshness", "metric_evidence_stale"),
    ("circuit_breaker", "circuit_open"),
])
def test_each_ordered_gate_interrupts_active_binding(tmp_path, gate, reason):
    svc, _ = service(tmp_path)
    create(svc)
    result = resolve(
        svc, decision=f"D-{gate}",
        gate_results=gates(**{gate: {"state": "blocked", "reason": reason}}))
    assert result["outcome"] == "INTERRUPTED"
    assert result["reason"] == reason
    trace = result["gate_trace"]
    assert [item["gate"] for item in trace] == list(GATE_ORDER)
    assert trace[list(GATE_ORDER).index(gate)]["passed"] is False


@pytest.mark.parametrize("reason", [
    "http_429", "http_5xx", "timeout", "retry_requires_reassignment",
    "fallback_requires_reassignment", "model_mapping_invalid",
    "manual_channel_disabled", "authorization_denied",
])
def test_execution_and_policy_reassignment_reasons_are_persisted(tmp_path, reason):
    svc, _ = service(tmp_path)
    binding = create(svc)["binding"]
    updated = svc.interrupt(
        binding["sticky_binding_id"], decision_id=f"D-{reason}", reason=reason)
    assert updated["state"] == "INTERRUPTED"
    assert updated["interruption_reason"] == reason


def test_policy_and_key_rotation_invalidate_on_restart(tmp_path):
    clock = Clock()
    first_policy = policy_file(tmp_path)
    database = tmp_path / "sticky.sqlite3"
    first = StickyRoutingService(
        database, first_policy, secret=HMAC_TEST_MATERIAL, key_id="key-v1", clock=clock)
    create(first)
    changed_policy = json.loads(first_policy.read_text(encoding="utf-8"))
    changed_policy["policy_version"] = "sticky-routing-policy-v2"
    second_policy = tmp_path / "policy-v2.json"
    second_policy.write_text(json.dumps(changed_policy), encoding="utf-8")
    restarted = StickyRoutingService(
        database, second_policy, secret=HMAC_TEST_MATERIAL, key_id="key-v2", clock=clock)
    item = restarted.list()["items"][0]
    assert item["state"] == "INVALIDATED"
    assert item["invalidation_reason"] == "policy_configuration_or_key_rotation"


@pytest.mark.parametrize("changes,reason", [
    ({"environment_id": "overseas"}, "environment_changed"),
    ({"requested_model": "model-b"}, "requested_model_changed_incompatibly"),
    ({"capability_scope": {"required_modalities": ["text", "image"]}},
     "capability_requirements_changed"),
])
def test_scope_change_interrupts_prior_affinity_binding(tmp_path, changes, reason):
    svc, _ = service(tmp_path)
    create(svc)
    result = resolve(svc, decision=f"D-{reason}", **changes)
    assert result["outcome"] == "MISS"
    interrupted = svc.list(state="INTERRUPTED")["items"]
    assert len(interrupted) == 1
    assert interrupted[0]["interruption_reason"] == reason


def test_concurrent_creation_yields_one_active_binding(tmp_path):
    svc, _ = service(tmp_path)
    barrier = threading.Barrier(12)
    def work(index):
        decision = f"CONCURRENT-{index}"
        assert resolve(svc, decision=decision)["outcome"] == "MISS"
        barrier.wait()
        return svc.create_binding(
            decision_id=decision, affinity="opaque-A",
            environment_id="china_uat", requested_model="model-a",
            capability_scope=None, stream=False, selected_channel="19",
            evidence={"evidence_ids": ["EV-1"]}, is_mock=True)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(work, range(12)))
    assert sum(item["created"] for item in results) == 1
    assert svc.list(state="ACTIVE")["total"] == 1


def test_concurrent_hits_are_not_lost(tmp_path):
    svc, _ = service(tmp_path)
    create(svc)
    def work(index):
        return resolve(svc, decision=f"H{index}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(20)))
    assert all(item["outcome"] == "HIT" for item in results)
    assert svc.list(state="ACTIVE")["items"][0]["hit_count"] == 20


def test_migration_preserves_existing_database_history(tmp_path):
    database = tmp_path / "sticky.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE existing_history(id TEXT PRIMARY KEY,payload TEXT)")
        db.execute("INSERT INTO existing_history VALUES('old','preserve')")
    svc, _ = service(tmp_path)
    with svc.connect() as db:
        assert db.execute(
            "SELECT payload FROM existing_history WHERE id='old'").fetchone()[0] == "preserve"
        assert db.execute(
            "SELECT MAX(version) FROM sticky_schema_migrations").fetchone()[0] == 5


def test_incremental_upgrade_from_previous_sticky_schema(tmp_path):
    svc, clock = service(tmp_path)
    create(svc)
    with sqlite3.connect(tmp_path / "sticky.sqlite3") as db:
        db.execute("DROP INDEX IF EXISTS uq_sticky_active_route")
        db.execute("ALTER TABLE sticky_bindings DROP COLUMN capability_scope_version")
        db.execute("DELETE FROM sticky_schema_migrations WHERE version IN (3,4)")
    upgraded = StickyRoutingService(
        tmp_path / "sticky.sqlite3", svc.policy_path,
        secret=HMAC_TEST_MATERIAL, key_id="test-key-v1", clock=clock)
    with upgraded.connect() as db:
        columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(sticky_bindings)")}
        versions = {row["version"] for row in db.execute(
            "SELECT version FROM sticky_schema_migrations")}
    assert "capability_scope_version" in columns
    assert versions == {1, 2, 3, 4, 5}
    assert upgraded.list()["items"][0]["state"] == "INVALIDATED"


def test_explicit_invalidation_is_idempotent_and_conflict_safe(tmp_path):
    svc, _ = service(tmp_path)
    binding = create(svc)["binding"]
    request_hash = hashlib.sha256(b"same-request").hexdigest()
    first = svc.invalidate(
        binding["sticky_binding_id"], environment_id="china_uat",
        reason="operator_confirmed_invalidation", idempotency_key="IDEM-1",
        request_sha256=request_hash)
    replay = svc.invalidate(
        binding["sticky_binding_id"], environment_id="china_uat",
        reason="operator_confirmed_invalidation", idempotency_key="IDEM-1",
        request_sha256=request_hash)
    assert first["binding"]["state"] == "INVALIDATED"
    assert replay["idempotent_replay"] is True
    with pytest.raises(StickyRoutingError, match="idempotency_key_conflict"):
        svc.invalidate(
            binding["sticky_binding_id"], environment_id="china_uat",
            reason="different", idempotency_key="IDEM-1",
            request_sha256=hashlib.sha256(b"different").hexdigest())
    with svc.connect() as db:
        stored = db.execute(
            "SELECT idempotency_key FROM sticky_mutation_idempotency").fetchone()[0]
    assert stored == hashlib.sha256(b"IDEM-1").hexdigest()


def test_api_pagination_filters_confirmation_origin_and_authorization(tmp_path):
    svc, _ = service(tmp_path)
    first = create(svc)["binding"]
    create(svc, decision="D2", affinity="opaque-B", selected_channel="48")
    app = FastAPI()
    allowed = {"http://127.0.0.1:5174"}
    def secure(request: Request):
        if request.headers.get("origin") not in allowed:
            raise HTTPException(403, detail={"code": "ORIGIN_REJECTED"})
    app.include_router(build_sticky_routing_router(
        lambda: svc, secure,
        lambda _request, env: env in {"china_uat", "overseas"}))
    client = TestClient(app)
    page = client.get("/api/v1/sticky-routing/bindings?limit=1&offset=0&channel=19")
    assert page.status_code == 200
    assert page.json()["total"] == 1
    assert page.json()["items"][0]["selected_channel"] == "19"
    assert "route_key_fingerprint" not in page.json()["items"][0]
    body = {
        "environment_id": "china_uat",
        "reason": "operator_confirmed_invalidation",
        "confirmation_text": "我确认使此粘性路由绑定失效",
    }
    assert client.post(
        f"/api/v1/sticky-routing/bindings/{first['sticky_binding_id']}/invalidate",
        json=body, headers={"Idempotency-Key": "x"}).status_code == 403
    assert client.post(
        f"/api/v1/sticky-routing/bindings/{first['sticky_binding_id']}/invalidate",
        json={**body, "confirmation_text": "确认"},
        headers={"Origin": "http://127.0.0.1:5174", "Idempotency-Key": "x"}
    ).status_code == 400
    response = client.post(
        f"/api/v1/sticky-routing/bindings/{first['sticky_binding_id']}/invalidate",
        json=body,
        headers={"Origin": "http://127.0.0.1:5174", "Idempotency-Key": "x"})
    assert response.status_code == 200
    assert response.json()["binding"]["state"] == "INVALIDATED"


def test_no_affinity_or_secret_appears_in_database_or_api(tmp_path):
    affinity = "PRIVATE-AFFINITY-SHOULD-NEVER-PERSIST"
    svc, _ = service(tmp_path)
    create(svc, affinity=affinity)
    database_bytes = (tmp_path / "sticky.sqlite3").read_bytes()
    assert affinity.encode() not in database_bytes
    assert HMAC_TEST_MATERIAL.encode() not in database_bytes
    payload = json.dumps(svc.list(), ensure_ascii=False)
    assert affinity not in payload
    assert HMAC_TEST_MATERIAL not in payload
    assert "Authorization" not in payload


class AllPassEligibility:
    def evaluate(self, *, request, candidates, metric_context):
        return {
            "gate_results": gates(),
            "eligible_channel_ids": [row["candidate_id"] for row in candidates],
            "evidence": {
                "metric_snapshot_id": "MET-SCHEDULER",
                "confidence_snapshot_id": "CONF-SCHEDULER",
                "evidence_ids": ["EV-SCHEDULER"],
            },
        }


class BudgetDeniedEligibility(AllPassEligibility):
    def evaluate(self, **kwargs):
        result = super().evaluate(**kwargs)
        result["gate_results"]["budget"] = {
            "state": "blocked", "reason": "budget_exhausted"}
        return result


def scheduler_with_sticky(tmp_path, svc, **changes):
    return Scheduler(
        logger=DecisionLogger(tmp_path / "scheduler.jsonl"),
        id_generator=lambda _request, index: f"STICKY-DECISION-{index}",
        sticky_service=svc, sticky_eligibility_provider=AllPassEligibility(),
        **changes,
    )


def runtime_payload(**changes):
    value = {
        "request_id": "R-STICKY", "requested_model": "deepseek-v4-flash",
        "stream": False, "input_tokens": 1000, "output_tokens": 500,
        "currency": "CNY", "strategy": "confidence_aware_v2",
        "mode": "simulation", "metadata": {"environment_id": "china_uat"},
        "sticky_affinity_key": "EPHEMERAL-OPAQUE-AFFINITY",
        "capability_scope": {"required_modalities": ["text"]},
    }
    value.update(changes)
    return value


def test_scheduler_creates_then_reuses_binding_without_logging_affinity(tmp_path):
    svc, _ = service(tmp_path)
    scheduler = scheduler_with_sticky(tmp_path, svc)
    first = scheduler.route(runtime_payload())
    second = scheduler.route(runtime_payload(request_id="R-STICKY-2"))
    assert first["sticky_routing"]["outcome"] == "CREATED"
    assert second["sticky_routing"]["outcome"] == "HIT"
    assert second["recommended_candidate"] == first["recommended_candidate"]
    logged = (tmp_path / "scheduler.jsonl").read_text(encoding="utf-8")
    assert "EPHEMERAL-OPAQUE-AFFINITY" not in logged
    assert second["sticky_routing"]["binding"]["hit_count"] == 1


def test_scheduler_fails_sticky_closed_when_gate_provider_is_missing(tmp_path):
    svc, _ = service(tmp_path)
    scheduler = Scheduler(
        logger=DecisionLogger(tmp_path / "scheduler.jsonl"),
        id_generator=lambda _request, index: f"D-{index}",
        sticky_service=svc,
    )
    result = scheduler.route(runtime_payload())
    assert result["sticky_routing"]["outcome"] == "BLOCKED"
    assert result["sticky_routing"]["reason"] == "security_authorization_gate_result_missing"
    assert result["execution_attempted"] is False
    assert svc.list(state="ACTIVE")["total"] == 0


def test_scheduler_does_not_route_when_an_earlier_gate_denies(tmp_path):
    svc, _ = service(tmp_path)
    scheduler = Scheduler(
        logger=DecisionLogger(tmp_path / "scheduler.jsonl"),
        id_generator=lambda _request, index: f"D-{index}",
        sticky_service=svc,
        sticky_eligibility_provider=BudgetDeniedEligibility(),
    )
    result = scheduler.route(runtime_payload())
    assert result["status"] == "blocked"
    assert result["routing_allowed"] is False
    assert result["recommended_candidate"] is None
    assert result["sticky_routing"]["reason"] == "budget_exhausted"


def test_replayed_decision_does_not_increment_hit_or_extend_ttl(tmp_path):
    svc, clock = service(tmp_path)
    create(svc)
    first = resolve(svc, decision="H-REPLAY")
    clock.value += timedelta(seconds=3)
    replay = resolve(svc, decision="H-REPLAY")
    binding = svc.list(state="ACTIVE")["items"][0]
    assert first["outcome"] == replay["outcome"] == "HIT"
    assert replay["idempotent_replay"] is True
    assert binding["hit_count"] == 1
    assert binding["expires_at"] == first["binding"]["expires_at"]
    with pytest.raises(StickyRoutingError, match="decision_id_scope_conflict"):
        resolve(svc, decision="H-REPLAY", eligible_channel_ids=["48"])


def test_missing_key_on_restart_does_not_invalidate_active_history(tmp_path):
    svc, clock = service(tmp_path)
    create(svc)
    blocked = StickyRoutingService(
        tmp_path / "sticky.sqlite3", svc.policy_path,
        secret="", key_id="", clock=clock)
    assert blocked.status()["blocked_reason"] == "sticky_key_material_unavailable"
    assert blocked.list()["items"][0]["state"] == "ACTIVE"


def test_mock_fallback_interrupts_created_binding_before_reassignment(tmp_path):
    class FailOnceAdapter(MockChannelAdapter):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def send(self, request, candidate):
            self.calls += 1
            result = super().send(request, candidate)
            if self.calls == 1:
                return {**result, "status": "timeout",
                        "error_category": "upstream_timeout"}
            return result

    svc, _ = service(tmp_path)
    scheduler = scheduler_with_sticky(tmp_path, svc, adapter=FailOnceAdapter())
    result = scheduler.route(runtime_payload(mode="mock_execute"))
    assert result["fallback_executed"] is True
    assert result["sticky_routing"]["outcome"] == "INTERRUPTED"
    assert result["sticky_routing"]["reason"].startswith(
        "fallback_requires_reassignment:")
    assert svc.list(state="ACTIVE")["total"] == 0


def test_gate_denial_does_not_interrupt_related_scope(tmp_path):
    svc, _ = service(tmp_path)
    original = create(svc)
    denied = resolve(
        svc, decision="DENIED-SCOPE", affinity="opaque-A",
        environment_id="overseas",
        gate_results=gates(budget=False),
    )
    assert denied["outcome"] == "BLOCKED"
    persisted = svc.get(original["binding"]["sticky_binding_id"])
    assert persisted["state"] == "ACTIVE"


def test_gate_states_are_not_interchangeable(tmp_path):
    svc, _ = service(tmp_path)
    result = resolve(
        svc, decision="WRONG-GATE-STATE",
        gate_results=gates(security_authorization="healthy"),
    )
    assert result["outcome"] == "BLOCKED"
    assert result["reason"] == "security_authorization_healthy"


def test_controlled_eligibility_is_per_channel_and_exceptions_fail_closed():
    candidates = [
        {"candidate_id": "19", "routing_eligible": "TRUE"},
        {"candidate_id": "48", "routing_eligible": "TRUE"},
    ]

    controlled = ControlledStickyEligibilityProvider({
        gate: (
            lambda *, _gate=gate, **_kwargs: {
                "state": sorted(GATE_PASS_STATES[_gate])[0],
                "eligible_channel_ids": ["19"],
            }
        )
        for gate in GATE_ORDER
    })
    result = controlled.evaluate(
        request=object(), candidates=candidates, metric_context={})
    assert result["eligible_channel_ids"] == ["19"]

    def exploding(**_kwargs):
        raise RuntimeError("unsafe detail")

    blocked = ControlledStickyEligibilityProvider({
        **{
            gate: (
                lambda *, _gate=gate, **_kwargs: {
                    "state": sorted(GATE_PASS_STATES[_gate])[0],
                    "eligible_channel_ids": ["19"],
                }
            )
            for gate in GATE_ORDER
        },
        "health": exploding,
    }).evaluate(request=object(), candidates=candidates, metric_context={})
    assert blocked["eligible_channel_ids"] == []
    assert blocked["gate_results"]["health"]["reason"] == "health_provider_error"
    assert "unsafe detail" not in json.dumps(blocked)


def test_read_apis_project_elapsed_active_binding_without_mutation(tmp_path):
    svc, clock = service(tmp_path)
    created = create(svc)
    clock.value += timedelta(seconds=11)
    assert svc.list(state="ACTIVE")["total"] == 0
    expired = svc.list(state="EXPIRED")
    assert expired["total"] == 1
    assert expired["items"][0]["state"] == "EXPIRED"
    assert expired["items"][0]["persisted_state"] == "ACTIVE"
    assert expired["items"][0]["expiration_pending_reconciliation"] is True
    assert svc.status()["state_counts"]["EXPIRED"] == 1
    with svc.connect() as db:
        persisted = db.execute(
            "SELECT state FROM sticky_bindings WHERE sticky_binding_id=?",
            (created["binding"]["sticky_binding_id"],),
        ).fetchone()[0]
    assert persisted == "ACTIVE"


def test_event_ledger_snapshots_ttl_hits_and_state_transitions(tmp_path):
    svc, _ = service(tmp_path)
    create(svc)
    resolve(svc, decision="AUDIT-HIT")
    with svc.connect() as db:
        rows = db.execute("""SELECT outcome,state_before,state_after,expires_at,
          maximum_expires_at,last_used_at,hit_count,safe_route_key_fingerprint
          FROM sticky_routing_events WHERE sticky_binding_id IS NOT NULL
          ORDER BY audit_timestamp,event_id""").fetchall()
    assert {row["outcome"] for row in rows} >= {"CREATED", "HIT"}
    hit = next(row for row in rows if row["outcome"] == "HIT")
    assert hit["state_before"] == hit["state_after"] == "ACTIVE"
    assert hit["expires_at"] and hit["maximum_expires_at"] and hit["last_used_at"]
    assert hit["hit_count"] == 1
    assert hit["safe_route_key_fingerprint"]


def test_real_execute_cannot_create_binding(tmp_path):
    svc, _ = service(tmp_path)
    result = scheduler_with_sticky(tmp_path, svc).route(
        runtime_payload(mode="real_execute"))
    assert result["sticky_routing"]["outcome"] == "BLOCKED"
    assert result["sticky_routing"]["reason"] == "sticky_mode_not_allowed"
    assert svc.list(state="ACTIVE")["total"] == 0


def test_status_distinguishes_crypto_operation_from_gate_readiness(tmp_path):
    svc, _ = service(tmp_path)
    before = svc.status()
    assert before["operational"] is True
    assert before["execution_ready"] is False
    assert before["blocked_reason"] == "sticky_eligibility_provider_unavailable"
    scheduler_with_sticky(tmp_path, svc)
    after = svc.status()
    assert after["status"] == "ready"
    assert after["execution_ready"] is True


def test_retryable_terminal_failure_without_fallback_interrupts_binding(tmp_path):
    class AlwaysTimeout(MockChannelAdapter):
        def send(self, request, candidate):
            result = super().send(request, candidate)
            return {
                **result, "status": "timeout",
                "error_category": "upstream_timeout",
            }

    svc, _ = service(tmp_path)
    scheduler = scheduler_with_sticky(tmp_path, svc, adapter=AlwaysTimeout())
    scheduler.route(runtime_payload())
    scheduler.fallback_policy = RetryPolicy({
        "policy_version": "test-no-fallback",
        "max_attempts": 1,
        "retryable_errors": ["upstream_timeout"],
        "non_retryable_errors": [],
        "automatic_real_execution_authorized": False,
    })
    result = scheduler.route(
        runtime_payload(request_id="TERMINAL", mode="mock_execute"))
    assert result["fallback_executed"] is False
    assert result["sticky_routing"]["outcome"] == "INTERRUPTED"
    assert result["sticky_routing"]["reason"].startswith(
        "retry_requires_reassignment:")
    assert svc.list(state="ACTIVE")["total"] == 0


def test_cli_wires_enabled_sticky_policy_with_local_gate_snapshot(tmp_path):
    policy = json.loads(
        (ROOT / "config" / "sticky_routing_policy_v1.json")
        .read_text(encoding="utf-8"))
    policy["enabled"] = True
    policy_path = tmp_path / "sticky-policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    with (ROOT / "data" / "candidate_channels_v1.csv").open(
        encoding="utf-8-sig") as handle:
        import csv
        candidates = [row["candidate_id"] for row in csv.DictReader(handle)]
    gate_snapshot = {
        "gate_results": {
            gate: {"state": sorted(GATE_PASS_STATES[gate])[0],
                   "eligible_channel_ids": candidates}
            for gate in GATE_ORDER
        }
    }
    gate_path = tmp_path / "gate-snapshot.json"
    gate_path.write_text(json.dumps(gate_snapshot), encoding="utf-8")
    governance_database = tmp_path / "governance.sqlite3"
    capability_version = "cli-sticky-fixture-v1"
    CapabilityEvidenceService(
        governance_database,
        ROOT / "config" / "capability_evidence_policy_v1.json",
    ).record_evidence({
        "evidence_id": "EV-CLI-STICKY-TEXT",
        "evidence_type": "contract_test",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "environment_id": "china_uat",
        "subject_id": "deepseek-v4-flash",
        "subject_version": capability_version,
        "source": "local",
        "requirement": "text",
        "state": "supported",
    })
    request_path = tmp_path / "request.json"
    payload = runtime_payload()
    payload["metadata"]["capability_subject_version"] = capability_version
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    command = [
        sys.executable, str(ROOT / "src" / "run_scheduler.py"),
        "--request-file", str(request_path),
        "--sticky-policy", str(policy_path),
        "--sticky-database", str(tmp_path / "sticky.sqlite3"),
        "--sticky-eligibility-file", str(gate_path),
        "--governance-database", str(governance_database),
        "--dry-run",
    ]
    process = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        env={
            **os.environ,
            "STICKY_ROUTE_KEY_SECRET": HMAC_TEST_MATERIAL,
            "STICKY_ROUTE_KEY_ID": "test-key-v1",
        },
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["sticky_routing"]["outcome"] == "CREATED"
    assert result["network_called"] is False
    assert HMAC_TEST_MATERIAL not in process.stdout + process.stderr


def test_protected_v3_evidence_hash_is_unchanged():
    protected = ROOT / "output" / "unified_uat_execution_v3.jsonl"
    assert protected.stat().st_size == 68620
    assert hashlib.sha256(protected.read_bytes()).hexdigest().upper() == (
        "E61D6CDD596BB8963F4BE9A1C42724D31A2A54E33765CEB5EF60AC372EACB627")
