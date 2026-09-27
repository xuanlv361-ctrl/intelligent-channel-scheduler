"""Local execution protocol primitives shared by retry and SSE handling.

The module deliberately performs no network I/O.  Executors receive a bounded
``AttemptContext`` and remain responsible for honouring it while doing work.
The engine verifies the bounds again after each executor returns.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol


class ExecutionCancelled(RuntimeError):
    """Raised when cooperative execution observes a cancellation request."""


class CancellationToken:
    """Thread-safe, cooperative cancellation with idempotent callbacks."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._callbacks: list[Callable[[str], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "client_cancelled") -> bool:
        reason = str(reason or "client_cancelled")
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            callbacks = tuple(self._callbacks)
            self._callbacks.clear()
            self._event.set()
        for callback in callbacks:
            callback(reason)
        return True

    def add_callback(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            if not self._event.is_set():
                self._callbacks.append(callback)
                return
            reason = self._reason or "client_cancelled"
        callback(reason)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ExecutionCancelled(self.reason or "client_cancelled")


@dataclass(frozen=True, slots=True)
class AttemptContext:
    attempt_number: int
    candidate_id: str
    idempotency_key: str
    started_monotonic: float
    attempt_timeout_seconds: float
    deadline_monotonic: float
    cancellation_token: CancellationToken
    clock: Callable[[], float] = time.monotonic

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - self.clock())

    def raise_if_cancelled(self) -> None:
        self.cancellation_token.raise_if_cancelled()


class ContextExecutor(Protocol):
    def execute_attempt(
        self, request: Any, candidate: Mapping[str, Any], context: AttemptContext,
    ) -> Mapping[str, Any]: ...


@dataclass(slots=True)
class ReleaseOnce:
    """Call a reservation/lease release function at most once."""

    callback: Callable[[Mapping[str, Any]], None] | None
    _released: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def release(self, summary: Mapping[str, Any]) -> bool:
        with self._lock:
            if self._released:
                return False
            self._released = True
        if self.callback is not None:
            self.callback(dict(summary))
        return True
