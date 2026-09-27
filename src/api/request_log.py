"""
Server logs shared by the web app and the standalone public API, under config.LOG_DIR, rotated at UTC
midnight and kept LOG_RETENTION_DAYS days:
  requests.jsonl   one JSON line per HTTP request (RequestLogMiddleware; allowlisted headers only)
  questions.jsonl  questions visitors asked the agent
  errors.log       server-side tracebacks, tagged with the request id the client got
Nothing is written until setup_file_logging() (called from both lifespans).
"""

import json
import logging
import logging.handlers
import os
import time
import traceback
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request

import config
from api.rate_limit import client_ip, route_bucket

REQUESTS_LOGGER = logging.getLogger("knessetlm.requests")
QUESTIONS_LOGGER = logging.getLogger("knessetlm.questions")
ERRORS_LOGGER = logging.getLogger("knessetlm.errors")
LOG_FILE_BY_LOGGER = {
    REQUESTS_LOGGER: "requests.jsonl",
    QUESTIONS_LOGGER: "questions.jsonl",
    ERRORS_LOGGER: "errors.log",
}
GEMINI_KEY_HEADER = b"x-gemini-api-key"
REQUEST_ID_HEADER = b"x-request-id"

current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)


def file_handlers() -> list[logging.handlers.TimedRotatingFileHandler]:
    return [handler for logger in LOG_FILE_BY_LOGGER for handler in logger.handlers
            if isinstance(handler, logging.handlers.TimedRotatingFileHandler)]


def close_file_logging() -> None:
    for logger in LOG_FILE_BY_LOGGER:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def setup_file_logging() -> None:
    """Attach the rotating file handlers under config.LOG_DIR; a no-op when they already write there."""
    log_dir = Path(os.path.abspath(config.LOG_DIR))
    handlers = file_handlers()
    if len(handlers) == len(LOG_FILE_BY_LOGGER) and all(Path(h.baseFilename).parent == log_dir for h in handlers):
        return
    close_file_logging()
    log_dir.mkdir(parents=True, exist_ok=True)
    for logger, file_name in LOG_FILE_BY_LOGGER.items():
        handler = logging.handlers.TimedRotatingFileHandler(
            log_dir / file_name, when="midnight", utc=True, backupCount=config.LOG_RETENTION_DAYS,
            encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(message)s" if logger is ERRORS_LOGGER else "%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def request_id_of(request: Request) -> str | None:
    return getattr(request.state, "request_id", None) or current_request_id.get()


def generic_error_message(request_id: str | None) -> str:
    return f"{config.WEB_GENERIC_ERROR_MESSAGE} (מזהה בקשה: {request_id})" if request_id else \
        config.WEB_GENERIC_ERROR_MESSAGE


def log_server_error(request_id: str | None, context: str, traceback_text: str) -> None:
    ERRORS_LOGGER.error(f"[{request_id}] {context}\n{traceback_text.rstrip()}")


def log_question(request: Request, route: str, session_id: str | None, question) -> None:
    QUESTIONS_LOGGER.info(json.dumps({
        "ts": utc_timestamp(), "id": request_id_of(request), "ip": client_ip(request),
        "route": route, "session_id": session_id, "q": question,
    }, ensure_ascii=False, default=str))


def _milliseconds_since(start: float | None, now: float) -> float | None:
    return None if start is None else round((now - start) * 1000, 1)


class RequestLogMiddleware:
    """Pure ASGI (SSE streams pass untouched); register it last so it wraps every other middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = new_request_id()
        request_state = scope.setdefault("state", {})
        request_state["request_id"] = request_id
        context_token = current_request_id.set(request_id)
        started = time.monotonic()
        response = {"status": None, "first_byte_at": None, "bytes": 0}

        async def send_and_measure(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
                response["first_byte_at"] = time.monotonic()
                message = {**message, "headers": [*message.get("headers", []),
                                                  (REQUEST_ID_HEADER, request_id.encode())]}
            elif message["type"] == "http.response.body":
                response["bytes"] += len(message.get("body", b""))
            await send(message)

        escaped_error = None
        try:
            await self.app(scope, receive, send_and_measure)
        except Exception as exc:
            escaped_error = type(exc).__name__
            print(f"[request_log] {request_id} {scope.get('method')} {scope.get('path')} raised {escaped_error}: {exc}",
                  flush=True)
            log_server_error(request_id, f"unhandled exception in {scope.get('method')} {scope.get('path')}",
                             traceback.format_exc())
            raise
        finally:
            try:
                self._write_request_line(scope, request_state, request_id, started, response, escaped_error)
            except Exception as exc:
                print(f"[request_log] writing the request line of {request_id} failed: {exc}", flush=True)
            current_request_id.reset(context_token)

    @staticmethod
    def _write_request_line(scope, request_state: dict, request_id: str, started: float, response: dict,
                            escaped_error: str | None) -> None:
        if not REQUESTS_LOGGER.handlers:
            return
        now = time.monotonic()
        request = Request(scope)
        headers = request.headers
        path = scope.get("path", "")
        REQUESTS_LOGGER.info(json.dumps({
            "ts": utc_timestamp(),
            "id": request_id,
            "ip": client_ip(request),
            "cf_ray": headers.get("cf-ray"),
            "country": headers.get("cf-ipcountry"),
            "method": scope.get("method"),
            "path": path,
            "query": scope.get("query_string", b"").decode("latin-1")[:config.REQUEST_LOG_MAX_QUERY_CHARS],
            "status": response["status"] or 500,
            "ms": _milliseconds_since(started, now),
            "ttfb_ms": _milliseconds_since(started, response["first_byte_at"]) if response["first_byte_at"] else None,
            "bytes": response["bytes"],
            "bucket": route_bucket(path),
            "gemini_key_sent": GEMINI_KEY_HEADER.decode() in headers,
            "gemini_key_outcome": request_state.get("gemini_key_outcome"),
            "session_id": request_state.get("session_id") or (scope.get("path_params") or {}).get("session_id"),
            "ua": (headers.get("user-agent") or "")[:config.REQUEST_LOG_MAX_USER_AGENT_CHARS],
            "error": escaped_error,
        }, ensure_ascii=False))
