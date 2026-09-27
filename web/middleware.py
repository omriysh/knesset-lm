"""
Pure-ASGI middlewares for the public web server: response security headers (CSP from config) and
a request body size limit. Pure ASGI rather than BaseHTTPMiddleware so SSE streams pass untouched.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config

DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc")
METHODS_WITH_BODY = ("POST", "PUT", "PATCH")


def content_security_policy(path: str) -> str:
    return config.WEB_DOCS_CONTENT_SECURITY_POLICY if path in DOCS_PATHS else config.WEB_CONTENT_SECURITY_POLICY


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra_headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"content-security-policy", content_security_policy(scope["path"]).encode()),
        ]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {name for name, _ in extra_headers}
                kept = [(name, value) for name, value in message.get("headers", []) if name.lower() not in names]
                message = {**message, "headers": kept + extra_headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


async def _send_json_error(send, status_code: int, message: str) -> None:
    body = json.dumps({"error": message}, ensure_ascii=False).encode()
    await send({"type": "http.response.start", "status": status_code,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class RequestBodyLimitMiddleware:
    """413 when Content-Length exceeds WEB_MAX_REQUEST_BODY_BYTES, 411 for bodies without a
    Content-Length (chunked); the declared length is enforced while the body is read too."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in METHODS_WITH_BODY:
            await self.app(scope, receive, send)
            return
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        max_bytes = config.WEB_MAX_REQUEST_BODY_BYTES
        declared_length = headers.get(b"content-length")
        if declared_length is None or b"chunked" in headers.get(b"transfer-encoding", b"").lower():
            await _send_json_error(send, 411, "Content-Length required")
            return
        try:
            declared_bytes = int(declared_length)
        except ValueError as exc:
            print(f"[web] bad Content-Length {declared_length!r}: {exc}", flush=True)
            await _send_json_error(send, 400, "invalid Content-Length")
            return
        if declared_bytes > max_bytes:
            await _send_json_error(send, 413, f"request body larger than {max_bytes} bytes")
            return

        received_bytes = 0

        async def receive_within_limit():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > max_bytes:
                    raise ValueError(f"request body exceeded {max_bytes} bytes despite Content-Length")
            return message

        await self.app(scope, receive_within_limit, send)
