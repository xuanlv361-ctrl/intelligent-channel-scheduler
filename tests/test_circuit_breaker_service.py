from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.circuit_breaker_service import (
    CircuitBreakerEventConflict, CircuitBreakerLeaseError, CircuitBreakerService,
)


POLICY = {
    "policy_version": "test-v1", "enabled": True,
    "failure_window_seconds": 10, "failure_threshold": 2,
    "cooldown_seconds": 5, "half_open_probe_quota": 2,
    "half_open_success_threshold": 2, "probe_lease_seconds": 3,
}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def service(tmp_path, clock):
    return CircuitBreakerService(tmp_path / "circuit.db", POLICY, clock=clock)


def open_circuit(cb):
    cb.record_failure("channel:a", "failure-1")
    return cb.record_failure("channel:a", "failure-2")


def test_closed_open_half_open_closed_and_audits(tmp_path):
    clock = Clock()
    cb = service(tmp_path, clock)
    assert cb.before_request("channel:a", "request-normal")["allowed"] is True
    assert open_circuit(cb)["state"] == "OPEN"
    assert cb.before_request("channel:a", "blocked")["allowed"] is False
    clock.advance(5)
    first = cb.before_request("channel:a", "probe-1")
    second = cb.before_request("channel:a", "probe-2")
    assert first["state"] == second["state"] == "HALF_OPEN"
    assert cb.before_request("channel:a", "probe-3")["reason"] == "probe_quota_exhausted"
    assert cb.record_success("channel:a", "success-1", first["probe_lease_id"])["state"] == "HALF_OPEN"
    assert cb.record_success("channel:a", "success-2", second["probe_lease_id"])["state"] == "CLOSED"
    audits = cb.list_transition_audits("channel:a")
    assert [(a["from_state"], a["to_state"]) for a in audits] == [
        ("CLOSED", "OPEN"), ("OPEN", "HALF_OPEN"), ("HALF_OPEN", "CLOSED")]


def test_failure_window_and_duplicate_event_idempotency(tmp_path):
    clock = Clock()
    cb = service(tmp_path, clock)
    first = cb.record_failure("channel:a", "same-event")
    assert cb.record_failure("channel:a", "same-event") == first
    assert cb.get_state("channel:a")["state"] == "CLOSED"
    with pytest.raises(CircuitBreakerEventConflict):
        cb.record_success("channel:a", "same-event")
    clock.advance(11)
    cb.record_failure("channel:a", "failure-new")
    assert cb.get_state("channel:a")["state"] == "CLOSED"


def test_strict_probe_lease_and_probe_failure_reopens(tmp_path):
    clock = Clock()
    cb = service(tmp_path, clock)
    open_circuit(cb)
    clock.advance(5)
    probe = cb.before_request("channel:a", "probe")
    with pytest.raises(CircuitBreakerLeaseError):
        cb.record_success("channel:a", "missing-lease")
    assert cb.record_failure("channel:a", "probe-failed", probe["probe_lease_id"])["state"] == "OPEN"
    with pytest.raises(CircuitBreakerLeaseError):
        cb.record_failure("channel:a", "reuse-lease", probe["probe_lease_id"])


def test_restart_recovery_expires_leases_and_preserves_state(tmp_path):
    clock = Clock()
    cb = service(tmp_path, clock)
    open_circuit(cb)
    clock.advance(5)
    lease = cb.before_request("channel:a", "old-probe")
    clock.advance(4)
    restarted = service(tmp_path, clock)
    assert restarted.get_state("channel:a")["state"] == "HALF_OPEN"
    with pytest.raises(CircuitBreakerLeaseError):
        restarted.record_success("channel:a", "late", lease["probe_lease_id"])
    assert restarted.before_request("channel:a", "new-probe")["allowed"] is True


def test_inconsistent_persisted_state_is_quarantined_open(tmp_path):
    clock = Clock()
    cb = service(tmp_path, clock)
    cb.get_state("channel:a")
    with cb.connect() as db:
        db.execute("UPDATE circuit_breakers SET opened_at='2026-01-01T00:00:00Z' WHERE circuit_id='channel:a'")
    state = cb.get_state("channel:a")
    assert state["state"] == "OPEN"
    assert cb.before_request("channel:a", "denied")["allowed"] is False
    assert cb.list_transition_audits("channel:a")[-1]["reason"] == "corrupt_state_fail_closed"


def test_admission_request_id_is_idempotent(tmp_path):
    clock = Clock()
    cb = service(tmp_path, clock)
    open_circuit(cb)
    clock.advance(5)
    first = cb.before_request("channel:a", "same-request")
    assert cb.before_request("channel:a", "same-request") == first
    assert cb.before_request("channel:a", "other-request")["allowed"] is True
    assert cb.before_request("channel:a", "third-request")["allowed"] is False


def test_atomic_admission_never_oversubscribes_probe_quota(tmp_path):
    clock = Clock()
    policy = dict(POLICY, half_open_probe_quota=1, half_open_success_threshold=1)
    cb = CircuitBreakerService(tmp_path / "circuit.db", policy, clock=clock)
    open_circuit(cb)
    clock.advance(5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda number: cb.before_request("channel:a", f"parallel-{number}"), range(8)))
    assert sum(result["allowed"] for result in results) == 1
    assert sum(result["reason"] == "probe_quota_exhausted" for result in results) == 7
