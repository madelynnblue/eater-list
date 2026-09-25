"""Politeness primitives: a global token bucket plus a concurrency cap.

One limiter instance is shared by every worker so the process as a whole never
exceeds the configured request rate, regardless of thread count. A ``penalize``
call imposes a global cooldown, which is how throttling responses (429/503) are
handled without every thread independently hammering the server.
"""
from __future__ import annotations

import random
import threading
import time
from contextlib import contextmanager


class RateLimiter:
    def __init__(self, rate_per_second: float, burst: int, max_concurrency: int):
        self.rate = max(rate_per_second, 0.01)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(max(max_concurrency, 1))
        self._cooldown_until = 0.0

    # -- token bucket -----------------------------------------------------
    def _take_token(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                wait = 0.0
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    ready = now >= self._cooldown_until
                    if ready:
                        return
                    wait = self._cooldown_until - now
                else:
                    wait = max((1.0 - self._tokens) / self.rate, 0.0)
                    wait = max(wait, self._cooldown_until - now, 0.0)
            # small jitter avoids lock-step retries from parallel workers
            time.sleep(wait + random.uniform(0, 0.05))

    @contextmanager
    def slot(self):
        """Block until a request may be issued, holding a concurrency slot."""
        self._sem.acquire()
        try:
            self._take_token()
            yield
        finally:
            self._sem.release()

    def penalize(self, seconds: float) -> None:
        """Apply a global cooldown (used after a throttling response)."""
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + seconds)


def backoff_delay(attempt: int, base: float = 2.0, cap: float = 120.0) -> float:
    """Exponential backoff with full jitter."""
    return min(cap, base ** attempt) * (0.5 + random.random() / 2)
