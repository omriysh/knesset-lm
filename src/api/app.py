"""
Standalone public API server (no sessions, no LLM):
    uvicorn api.app:app --no-proxy-headers --no-access-log
Run from src/ or with src/ on sys.path. --no-proxy-headers keeps the peer address as the connecting
host so rate_limit.client_ip trusts CF-Connecting-IP only from a local cloudflared; requests are
logged to config.LOG_DIR/requests.jsonl by RequestLogMiddleware instead of uvicorn's access log.
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import config
from api.docs import install_public_docs
from api.rate_limit import RateLimitMiddleware, SlidingWindowRateLimiter
from api.request_log import RequestLogMiddleware, setup_file_logging
from api.routes import router
from api.validation import install_error_handlers


def use_public_api_http_settings() -> None:
    """Fail fast on Knesset API outages instead of holding worker threads through long retries."""
    config.API_RETRY_ATTEMPTS = config.PUBLIC_API_RETRY_ATTEMPTS
    config.API_RETRY_SLEEP = config.PUBLIC_API_RETRY_SLEEP
    config.HTTP_TIMEOUT_SECONDS = config.PUBLIC_API_HTTP_TIMEOUT_SECONDS


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_file_logging()
    use_public_api_http_settings()
    yield


rate_limiter = SlidingWindowRateLimiter()
PUBLIC_API_TITLE = "KnessetLM public API"

app = FastAPI(title=PUBLIC_API_TITLE, lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.add_middleware(RequestLogMiddleware)
app.include_router(router)
install_public_docs(app, PUBLIC_API_TITLE)
install_error_handlers(app)
