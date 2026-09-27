"""
In-process per-client-IP sliding-window rate limits for the standalone public API.

Routes that call the Knesset APIs share a low budget, knesset.db-only routes a higher one,
web-UI routes that run an LLM the lowest, every other route a generous web budget; only the page,
static files, docs, the llms texts and stream replays are not limited.
Limits are read from config on every request.
"""

import re
import time
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import config

WINDOW_SECONDS = 60
UPSTREAM_ROUTE_PREFIXES = ("/v1/mks", "/v1/committees", "/v1/parties", "/v1/bills", "/v1/votes")
DB_ROUTE_PREFIXES = ("/v1/protocols", "/v1/meetings/", "/api/browse/search")
DB_ROUTE_PATHS = ("/v1/meta", "/api/health")
AGENT_ROUTE_PATTERN = re.compile(r"^/api/research/(start|[^/]+/(respond|workspace/ask))$")
STREAM_REPLAY_PATTERN = re.compile(r"^/api/research/[^/]+/stream$")
UNLIMITED_PATHS = ("/", "/favicon.ico", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json",
                   "/llms.txt", "/llms-full.txt", "/agent-instructions")
UNLIMITED_PREFIXES = ("/static/",)


def route_bucket(path: str) -> str | None:
    if AGENT_ROUTE_PATTERN.match(path):
        return "agent"
    if path.startswith(UPSTREAM_ROUTE_PREFIXES):
        return "upstream"
    if path.startswith(DB_ROUTE_PREFIXES) or path in DB_ROUTE_PATHS:
        return "db"
    if path in UNLIMITED_PATHS or path.startswith(UNLIMITED_PREFIXES) or STREAM_REPLAY_PATTERN.match(path):
        return None
    return "web"


def bucket_limit(bucket: str) -> int:
    if bucket == "upstream":
        return config.API_RATE_LIMIT_UPSTREAM_PER_MINUTE
    if bucket == "agent":
        return config.API_RATE_LIMIT_AGENT_PER_MINUTE
    if bucket == "web":
        return config.API_RATE_LIMIT_WEB_PER_MINUTE
    return config.API_RATE_LIMIT_DB_PER_MINUTE


def client_ip(request: Request) -> str:
    peer_host = request.client.host if request.client else "unknown"
    if config.API_TRUST_CLOUDFLARE_IP_HEADER and peer_host in config.API_TRUSTED_PROXY_HOSTS:
        forwarded = request.headers.get("CF-Connecting-IP", "").strip()
        if forwarded:
            return forwarded
    return peer_host


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
