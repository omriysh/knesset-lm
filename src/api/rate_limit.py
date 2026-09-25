"""
In-process per-client-IP sliding-window rate limits for the standalone public API.

Routes that call the Knesset APIs share a low budget, knesset.db-only /v1 routes a higher one;
docs, health and meta are not limited. Limits are read from config on every request.
"""

import time
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import config

WINDOW_SECONDS = 60
UPSTREAM_ROUTE_PREFIXES = ("/v1/mks", "/v1/committees", "/v1/parties", "/v1/bills", "/v1/votes")
DB_ROUTE_PREFIXES = ("/v1/protocols", "/v1/meetings/")


def route_bucket(path: str) -> str | None:
    if path.startswith(UPSTREAM_ROUTE_PREFIXES):
        return "upstream"
    if path.startswith(DB_ROUTE_PREFIXES):
        return "db"
    return None


def bucket_limit(bucket: str) -> int:
    if bucket == "upstream":
        return config.API_RATE_LIMIT_UPSTREAM_PER_MINUTE
    return config.API_RATE_LIMIT_DB_PER_MINUTE


def client_ip(request: Request) -> str:
    if config.API_TRUST_CLOUDFLARE_IP_HEADER:
        forwarded = request.headers.get("CF-Connecting-IP", "").strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


class SlidingWindowRateLimiter:
    def __init__(self):
        self._hits: dict[tuple[str, str], deque] = {}

    def reset(self) -> None:
        self._hits.clear()

    def retry_after_seconds(self, key: tuple[str, str], limit: int, now: float) -> int:
        """0 and the hit recorded when under the limit, else seconds until the oldest hit leaves the window."""
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - WINDOW_SECONDS:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, int(hits[0] + WINDOW_SECONDS - now) + 1)
        hits.append(now)
        return 0

    def forget_idle_clients(self, now: float) -> None:
        idle = [key for key, hits in self._hits.items() if not hits or hits[-1] <= now - WINDOW_SECONDS]
        for key in idle:
            del self._hits[key]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: SlidingWindowRateLimiter):
        super().__init__(app)
        self.limiter = limiter
        self._requests_since_cleanup = 0

    async def dispatch(self, request: Request, call_next):
        bucket = route_bucket(request.url.path)
        if not config.API_RATE_LIMIT_ENABLED or bucket is None:
            return await call_next(request)
        now = time.monotonic()
        self._requests_since_cleanup += 1
        if self._requests_since_cleanup >= 1000:
            self.limiter.forget_idle_clients(now)
            self._requests_since_cleanup = 0
        limit = bucket_limit(bucket)
        retry_after = self.limiter.retry_after_seconds((client_ip(request), bucket), limit, now)
        if retry_after:
            return JSONResponse(
                {"error_code": "rate_limited",
                 "message": f"rate limit: {limit} requests per minute for these endpoints; retry in {retry_after}s"},
                status_code=429, headers={"Retry-After": str(retry_after)})
        return await call_next(request)
