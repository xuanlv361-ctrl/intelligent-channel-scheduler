"""Concurrency-safe in-memory ingress limits.

Client IPs can participate in an anonymous rate-limit key, but this module has
no persistence or logging hooks.  Authenticated keys are scoped by tenant,
workspace, principal/service identity and action.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import threading
import time
from typing import Callable, Hashable


@dataclass(frozen=True)
class RateLimitScope:
    tenant_id: str
    workspace_id: str
    principal_id: str
    principal_type: str
    action: str
    service_id: str = ""
    anonymous_ip: str = ""

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("rate_limit_action_required")
        if self.principal_type not in {"human", "service", "anonymous"}:
            raise ValueError("rate_limit_principal_type_invalid")
        if self.principal_type == "anonymous":
            if not self.anonymous_ip:
                raise ValueError("anonymous_rate_limit_ip_required")
        elif not all((self.tenant_id, self.workspace_id, self.principal_id)):
            raise ValueError("authenticated_rate_limit_scope_incomplete")
        if self.principal_type == "service" and not self.service_id:
            raise ValueError("service_rate_limit_identity_required")

    @property
    def key(self) -> tuple[str, ...]:
        if self.principal_type == "anonymous":
            return ("anonymous", self.anonymous_ip, self.action)
        return (
            self.tenant_id, self.workspace_id, self.principal_type,
            self.principal_id, self.service_id, self.action,
        )


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: float


class SlidingWindowRateLimiter:
    """Exact fixed-capacity sliding window with injected monotonic clock."""

    def __init__(self, *, capacity: int, window_seconds: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if capacity < 1 or window_seconds <= 0:
            raise ValueError("invalid_rate_limit_configuration")
        self.capacity = int(capacity)
        self.window_seconds = float(window_seconds)
        self.clock = clock
        self._events: dict[Hashable, deque[float]] = defaultdict(deque)
        self._lock = threading.RLock()

    def check(self, key: Hashable, *, consume: bool = True) -> RateLimitDecision:
        now = float(self.clock())
        threshold = now - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= threshold:
                events.popleft()
            if len(events) >= self.capacity:
                retry = max(0.0, self.window_seconds - (now - events[0]))
                return RateLimitDecision(False, self.capacity, 0, retry)
            if consume:
                events.append(now)
            remaining = self.capacity - len(events)
            return RateLimitDecision(True, self.capacity, remaining, 0.0)

    def consume(self, scope: RateLimitScope) -> RateLimitDecision:
        return self.check(scope.key)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


class ConcurrencyLease:
    def __init__(self, limiter: "ConcurrencyLimiter", key: Hashable):
        self._limiter = limiter
        self.key = key
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._limiter._release(self.key)

    def __enter__(self) -> "ConcurrencyLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class ConcurrencyLimiter:
    """Atomically enforces a global and per-scope concurrency ceiling."""

    def __init__(self, *, global_limit: int, per_scope_limit: int) -> None:
        if global_limit < 1 or per_scope_limit < 1 or per_scope_limit > global_limit:
            raise ValueError("invalid_concurrency_limit_configuration")
        self.global_limit = int(global_limit)
        self.per_scope_limit = int(per_scope_limit)
        self._global_active = 0
        self._scope_active: dict[Hashable, int] = defaultdict(int)
        self._lock = threading.Lock()

    def acquire(self, key: Hashable) -> ConcurrencyLease | None:
        with self._lock:
            if (self._global_active >= self.global_limit
                    or self._scope_active[key] >= self.per_scope_limit):
                return None
            self._global_active += 1
            self._scope_active[key] += 1
        return ConcurrencyLease(self, key)

    def _release(self, key: Hashable) -> None:
        with self._lock:
            if self._scope_active.get(key, 0) <= 0 or self._global_active <= 0:
                raise RuntimeError("concurrency_lease_underflow")
            self._scope_active[key] -= 1
            self._global_active -= 1
            if self._scope_active[key] == 0:
                self._scope_active.pop(key, None)

    def snapshot(self) -> tuple[int, dict[Hashable, int]]:
        with self._lock:
            return self._global_active, dict(self._scope_active)
