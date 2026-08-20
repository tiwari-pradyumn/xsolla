"""Token bucket for POST /v1/reviews. GETs are never rate limited.

Capacity equals the declared rateLimitPerMinute, so a sustained 30/min always
passes and only a burst beyond 30 is rejected -- with 429 and Retry-After, never
a 5xx.
"""

import math
import time

from app.config import RATE_LIMIT_PER_MINUTE


class TokenBucket:
    def __init__(self, capacity: int = RATE_LIMIT_PER_MINUTE, per_minute: int = RATE_LIMIT_PER_MINUTE):
        self.capacity = float(capacity)
        self.rate = per_minute / 60.0
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def take(self) -> int | None:
        """Consume a token. Returns None on success, else Retry-After seconds."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return None
        return max(1, math.ceil((1.0 - self.tokens) / self.rate))
