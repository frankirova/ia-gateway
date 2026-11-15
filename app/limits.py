"""Process-local admission, rate limiting and readiness caching."""

from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    """Per-application token bucket with a continuous refill rate."""

    def __init__(
        self,
        capacity: int,
        per_minute: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = float(capacity)
        self._refill_per_second = float(per_minute) / 60.0
        self._tokens = float(capacity)
        self._clock = clock
        self._updated = clock()
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Take one token if available; refill first using elapsed time."""
        with self._lock:
            now = self._clock()
            elapsed = now - self._updated
            if elapsed > 0:
                self._tokens = min(
                    self._capacity, self._tokens + elapsed * self._refill_per_second
                )
                self._updated = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


class InferenceSlot:
    """Single non-queueing inference slot held for one upstream call.

    Admission control is process-local by design: the deployment runs exactly
    one gateway process, so a lock is a sufficient claim on the backend slot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def try_acquire(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True

    def release(self) -> None:
        with self._lock:
            self._busy = False


class ReadinessCache:
    """Cache the last upstream readiness result for at most ``ttl`` seconds."""

    def __init__(
        self,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._value: bool | None = None
        self._checked_at = 0.0

    def result(self, probe: Callable[[], bool]) -> bool:
        now = self._clock()
        if self._value is not None and (now - self._checked_at) < self._ttl:
            return self._value
        value = probe()
        self._value = value
        self._checked_at = now
        return value
