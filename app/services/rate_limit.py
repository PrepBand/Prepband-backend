"""A small in-process sliding-window rate limiter (spec §9: limit
"certificate generation rate" and protect public lookups from scraping).

State lives in this worker's memory, which matches the single free-tier
Render instance. With several workers each would count separately - an
acceptable approximation for abuse protection, not a billing quota.
"""

import threading
import time
from collections import deque

from fastapi import Depends, HTTPException, Request

from ..core.security import get_current_user_id

_ALL: list["RateLimiter"] = []


class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: float, message: str) -> None:
        _ALL.append(self)
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.message = message
        self._calls: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            calls = self._calls.setdefault(key, deque())
            while calls and now - calls[0] > self.window_seconds:
                calls.popleft()
            if len(calls) >= self.max_calls:
                raise HTTPException(status_code=429, detail=self.message)
            calls.append(now)
            # Keep memory bounded: drop keys that have gone quiet.
            if len(self._calls) > 10_000:
                for stale in [k for k, v in self._calls.items() if not v or now - v[-1] > self.window_seconds]:
                    del self._calls[stale]

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


def client_ip(request: Request) -> str:
    # Render terminates TLS at a proxy and sets X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def reset_all_limiters() -> None:
    for limiter in _ALL:
        limiter.reset()


def per_user(limiter: RateLimiter):
    """A route dependency that counts calls per signed-in account (the
    verified JWT subject, never a client-supplied id)."""

    def _check(user_id: str = Depends(get_current_user_id)) -> None:
        limiter.check(user_id)

    return _check


certificate_pdf_limiter = RateLimiter(10, 60, "Too many certificate downloads. Please wait a minute and try again.")
verification_limiter = RateLimiter(30, 60, "Too many verification requests. Please wait a minute and try again.")

# Phase 6 (spec section 9: "limit ... requests per user"). Generous enough
# that a real student never meets them; they stop scripts, not people.
attempt_start_limiter = RateLimiter(30, 60, "Too many tests started. Please wait a minute and try again.")
autosave_limiter = RateLimiter(300, 60, "Too many saves in a short time. Please wait a moment and try again.")
submit_limiter = RateLimiter(20, 60, "Too many submissions. Please wait a minute and try again.")
mock_limiter = RateLimiter(20, 60, "Too many mock exam requests. Please wait a minute and try again.")
admin_bulk_limiter = RateLimiter(30, 60, "Too many bulk admin operations. Please wait a minute and try again.")
