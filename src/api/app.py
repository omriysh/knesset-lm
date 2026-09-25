"""
Standalone public API server (no sessions, no LLM): uvicorn api.app:app
Run from src/ or with src/ on sys.path.
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
from api.rate_limit import RateLimitMiddleware, SlidingWindowRateLimiter
from api.routes import router
from api.validation import install_error_handlers


def use_public_api_http_settings() -> None:
    """Fail fast on Knesset API outages instead of holding worker threads through long retries."""
    config.API_RETRY_ATTEMPTS = config.PUBLIC_API_RETRY_ATTEMPTS
    config.API_RETRY_SLEEP = config.PUBLIC_API_RETRY_SLEEP
    config.HTTP_TIMEOUT_SECONDS = config.PUBLIC_API_HTTP_TIMEOUT_SECONDS


@asynccontextmanager
async def lifespan(app: FastAPI):
    use_public_api_http_settings()
    yield


rate_limiter = SlidingWindowRateLimiter()

app = FastAPI(title="KnessetLM public API",
              description="Read-only access to Knesset committee protocols, MKs, bills and votes. "
                          "Start with /agent-instructions.",
              lifespan=lifespan)
app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.include_router(router)
install_error_handlers(app)
