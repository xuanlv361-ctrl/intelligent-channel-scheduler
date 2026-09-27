from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from backend.rate_limiting import (
    ConcurrencyLimiter, RateLimitScope, SlidingWindowRateLimiter,
)


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


def human(tenant="TENANT-A", principal="USER-1", action="evidence.read"):
    return RateLimitScope(
        tenant_id=tenant, workspace_id="WS-1", principal_id=principal,
        principal_type="human", action=action)


def test_sliding_window_is_deterministic_and_expires_old_events():
    clock = Clock()
    limiter = SlidingWindowRateLimiter(capacity=2, window_seconds=10, clock=clock)
    assert limiter.consume(human()).allowed is True
    assert limiter.consume(human()).remaining == 0
    denied = limiter.consume(human())
    assert denied.allowed is False
    assert denied.retry_after_seconds == 10
    clock.value += 10
    assert limiter.consume(human()).allowed is True


def test_rate_keys_isolate_tenant_principal_service_and_action():
    limiter = SlidingWindowRateLimiter(capacity=1, window_seconds=60)
    assert limiter.consume(human()).allowed
    assert not limiter.consume(human()).allowed
    assert limiter.consume(human(tenant="TENANT-B")).allowed
    assert limiter.consume(human(principal="USER-2")).allowed
    assert limiter.consume(human(action="evidence.import")).allowed
    service = RateLimitScope(
        "TENANT-A", "WS-1", "metrics_service", "service",
        "snapshot.generate", service_id="metrics_service")
    assert limiter.consume(service).allowed


def test_anonymous_ip_is_in_memory_only_and_authenticated_scope_is_complete():
    scope = RateLimitScope("", "", "", "anonymous", "health.read",
                           anonymous_ip="192.0.2.3")
    assert scope.key == ("anonymous", "192.0.2.3", "health.read")
    with pytest.raises(ValueError, match="authenticated_rate_limit_scope_incomplete"):
        RateLimitScope("", "WS-1", "USER", "human", "evidence.read")
    with pytest.raises(ValueError, match="service_rate_limit_identity_required"):
        RateLimitScope("TENANT", "WS", "svc", "service", "price.sync")


def test_concurrency_limit_is_atomic_and_release_is_idempotent():
    limiter = ConcurrencyLimiter(global_limit=4, per_scope_limit=2)
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        return limiter.acquire(("TENANT-A", "WS-1"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(lambda _value: attempt(), range(8)))
    acquired = [lease for lease in leases if lease is not None]
    assert len(acquired) == 2
    for lease in acquired:
        lease.release()
        lease.release()
    assert limiter.snapshot() == (0, {})


def test_global_concurrency_is_independent_of_scope():
    limiter = ConcurrencyLimiter(global_limit=2, per_scope_limit=2)
    first = limiter.acquire("A")
    second = limiter.acquire("B")
    assert first and second
    assert limiter.acquire("C") is None
    first.release()
    third = limiter.acquire("C")
    assert third is not None
    second.release()
    third.release()
