"""In-memory sliding-window rate limiter.

Valid for a single API process (see docs/limitations-and-risks.md, A2).
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = Lock()

    def hit(self, bucket: str, key: str, limit: int, window_seconds: float = 60.0) -> float | None:
        """Record a hit. Returns ``None`` if allowed, else seconds until retry."""
        if limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault((bucket, key), deque())
            while q and q[0] <= now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                return max(0.0, q[0] + window_seconds - now)
            q.append(now)
            return None

    def reset(self, bucket: str, key: str) -> None:
        with self._lock:
            self._hits.pop((bucket, key), None)

    def prune(self, window_seconds: float = 300.0) -> None:
        now = time.monotonic()
        with self._lock:
            for k in [k for k, q in self._hits.items() if not q or q[-1] <= now - window_seconds]:
                del self._hits[k]


limiter = RateLimiter()
