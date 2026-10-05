"""
MCP endpoint over the research tools: stateless Streamable HTTP (official `mcp` SDK, low-level
Server), JSON responses, no sessions. Served at config.MCP_PATH on the web app and the standalone
API, and at the root of config.MCP_SUBDOMAIN_HOSTS (McpSubdomainMiddleware).

Tools are RESEARCH_TOOL_REGISTRY with its own names and the public view of its JSON schemas
(routes.public_tool_schema: no top_k); a call runs the same validation, limits and `dispatch` as the
/v1 routes and returns the same JSON body. The server instructions are mcp_instructions.md, with the
protocol date range read from the database per request (cached until knesset.db changes on disk). Tool calls are
rate-limited in the bucket of the matching /v1 route, so REST and MCP share one budget per IP.

Requests that came through Cloudflare must carry a valid Cloudflare Access token
(Cf-Access-Jwt-Assertion, checked against the team's signing keys and config.CF_ACCESS_AUDIENCES);
local requests without Cloudflare headers skip the check.
"""

import json
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import jwt
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from api import validation as valid
from api.docs import API_VERSION
from api.rate_limit import bucket_limit, client_ip, route_bucket
from api.request_log import current_request_id, log_server_error
from api.routes import (TOOL_ENDPOINTS, input_error_body, log_tool_call, public_tool_schema, tool_call_outcome,
                        validated_tool_args, with_coverage_dates)
from api.tool_arguments import integral_floats_as_ints
from retrieval import knesset_db_store as store

MCP_SERVER_NAME = "knessetlm"
MCP_INSTRUCTIONS_PATH = Path(__file__).parent / "mcp_instructions.md"
MCP_TOOL_TITLES = {
    "find_mk":                "Find MK",
    "find_committee":         "Find committee",
    "find_party":             "Find party",
    "query_protocols":        "Search committee protocols",
    "get_meeting_attendance": "Committee meeting attendance",
    "query_bills":            "Search bills",
    "get_bill":               "Get bill",
    "query_votes":            "Search plenum votes",
}
CLOUDFLARE_REQUEST_HEADERS = ("cf-connecting-ip", "cf-ray")
CLOUDFLARE_ACCESS_TOKEN_HEADER = "cf-access-jwt-assertion"
CLOUDFLARE_ACCESS_ALGORITHMS = ["RS256"]


def tool_is_upstream(tool_name: str) -> bool:
    return route_bucket(TOOL_ENDPOINTS[tool_name]) == "upstream"


def mcp_instructions() -> str:
    """mcp_instructions.md with the protocol date range of the current knesset.db, the roster Knesset range
    and the protocol page size."""
    return (with_coverage_dates(MCP_INSTRUCTIONS_PATH.read_text(encoding="utf-8"))
            .replace("{roster_knesset_from}", str(config.API_KNESSET_NUM_RANGE[0]))
            .replace("{roster_knesset_to}", str(config.API_KNESSET_NUM_RANGE[1]))
            .replace("{protocols_page_chars}", str(config.API_PROTOCOLS_PAGE_CHARS)))


def mcp_tools() -> list[Tool]:
    tools = []
    for spec in RESEARCH_TOOL_REGISTRY:
        schema = public_tool_schema(spec)
        tools.append(Tool(
            name=spec.name,
            title=MCP_TOOL_TITLES.get(spec.name),
            description=schema["description"],
            input_schema={key: value for key, value in schema.items() if key != "description"},
            annotations=ToolAnnotations(title=MCP_TOOL_TITLES.get(spec.name), read_only_hint=True,
                                        destructive_hint=False, idempotent_hint=True,
                                        open_world_hint=tool_is_upstream(spec.name))))
    return tools


def tool_result(body: dict, is_error: bool) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
                          is_error=is_error)


def rate_limit_refusal(limiter, request: Request | None, tool_name: str) -> dict | None:
    if not config.API_RATE_LIMIT_ENABLED or request is None or tool_name not in TOOL_ENDPOINTS:
        return None
    bucket = route_bucket(TOOL_ENDPOINTS[tool_name])
    limit = bucket_limit(bucket)
    retry_after = limiter.retry_after_seconds((client_ip(request), bucket), limit, time.monotonic())
    if not retry_after:
        return None
    return {"error_code": "rate_limited", "tool": tool_name, "retry_after_seconds": retry_after,
            "message": f"rate limit: {limit} calls per minute for this tool group; retry in {retry_after}s"}


def build_mcp_server(limiter) -> Server:
    async def list_tools(ctx, params) -> ListToolsResult:
        return ListToolsResult(tools=mcp_tools())

    async def outcome_of_call(request: Request | None, tool_name: str, arguments: dict) -> tuple[bool, dict]:
        refusal = rate_limit_refusal(limiter, request, tool_name)
        if refusal:
            return True, refusal
        try:
            args = validated_tool_args(tool_name, arguments)
        except valid.ApiInputError as exc:
            return True, input_error_body(tool_name, exc)
        status, body = await anyio.to_thread.run_sync(tool_call_outcome, tool_name, args)
        return status != 200, body

    async def call_tool(ctx, params) -> CallToolResult:
        tool_name = params.name
        arguments = params.arguments or {}
        request = ctx.request if isinstance(ctx.request, Request) else None
        try:
            if isinstance(arguments, dict):
                arguments = integral_floats_as_ints(arguments)
            is_error, body = await outcome_of_call(request, tool_name, arguments)
        except Exception as exc:  # noqa: BLE001
            print(f"[mcp] {tool_name} raised {type(exc).__name__}: {exc}", flush=True)
            log_server_error(current_request_id.get(), f"mcp {tool_name} raised {type(exc).__name__}: {exc}",
                             traceback.format_exc())
            is_error, body = True, {"error_code": "internal_error", "message": "internal error", "tool": tool_name}
        log_tool_call(request, f"mcp:{tool_name}", tool_name, arguments if isinstance(arguments, dict) else {}, body)
        return tool_result(body, is_error=is_error)

    return Server(MCP_SERVER_NAME, version=API_VERSION, instructions=mcp_instructions(),
                  on_list_tools=list_tools, on_call_tool=call_tool)


# ── Cloudflare Access ────────────────────────────────────────────────────────

_jwks_client_by_team_domain: dict[str, jwt.PyJWKClient] = {}


def cloudflare_access_signing_key(token: str):
    team_domain = config.CF_ACCESS_TEAM_DOMAIN
    if team_domain not in _jwks_client_by_team_domain:
        _jwks_client_by_team_domain[team_domain] = jwt.PyJWKClient(
            f"https://{team_domain}/cdn-cgi/access/certs", cache_keys=True,
            lifespan=config.CF_ACCESS_JWKS_CACHE_SECONDS, timeout=config.PUBLIC_API_HTTP_TIMEOUT_SECONDS)
    return _jwks_client_by_team_domain[team_domain].get_signing_key_from_jwt(token).key


def request_came_through_cloudflare(request: Request) -> bool:
    peer_host = request.client.host if request.client else ""
    return peer_host not in config.MCP_LOCAL_PEER_HOSTS or any(
        header in request.headers for header in CLOUDFLARE_REQUEST_HEADERS)


def cloudflare_access_refusal_reason(token: str) -> str | None:
    if not config.CF_ACCESS_TEAM_DOMAIN or not config.CF_ACCESS_AUDIENCES:
        return "Cloudflare Access is not configured (CF_ACCESS_TEAM_DOMAIN / CF_ACCESS_AUDIENCES)"
    if not token:
        return "no Cf-Access-Jwt-Assertion header"
    try:
        jwt.decode(token, cloudflare_access_signing_key(token), algorithms=CLOUDFLARE_ACCESS_ALGORITHMS,
                   audience=list(config.CF_ACCESS_AUDIENCES), issuer=f"https://{config.CF_ACCESS_TEAM_DOMAIN}",
                   options={"require": ["exp", "iat", "aud", "iss"]})
    except jwt.PyJWTError as exc:
        return f"invalid Cloudflare Access token: {type(exc).__name__}: {exc}"
    return None


# ── HTTP endpoint, lifespan, subdomain routing ──────────────────────────────

class McpHttpEndpoint:
    """ASGI app for MCP_PATH; the session manager is created per app lifespan by running_mcp_sessions."""

    def __init__(self, server: Server):
        self.server = server
        self.session_manager: StreamableHTTPSessionManager | None = None

    async def __call__(self, scope, receive, send):
        request = Request(scope)
        if request.method != "POST":
            response = JSONResponse({"error_code": "method_not_allowed",
                                     "message": "stateless MCP endpoint: POST JSON-RPC requests only"},
                                    status_code=405, headers={"Allow": "POST"})
            return await response(scope, receive, send)
        if config.MCP_REQUIRE_CLOUDFLARE_ACCESS and request_came_through_cloudflare(request):
            token = request.headers.get(CLOUDFLARE_ACCESS_TOKEN_HEADER, "")
            refusal_reason = await anyio.to_thread.run_sync(cloudflare_access_refusal_reason, token)
            if refusal_reason:
                print(f"[mcp] {current_request_id.get()} from {client_ip(request)} refused: {refusal_reason}",
                      flush=True)
                response = JSONResponse({"error_code": "access_denied",
                                         "message": "a valid Cloudflare Access token is required"}, status_code=403)
                return await response(scope, receive, send)
        if self.session_manager is None:
            response = JSONResponse({"error_code": "mcp_not_ready", "message": "the MCP endpoint is starting"},
                                    status_code=503)
            return await response(scope, receive, send)
        self.server.instructions = mcp_instructions()
        await self.session_manager.handle_request(scope, receive, send)


def new_session_manager(server: Server) -> StreamableHTTPSessionManager:
    return StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True,
        max_request_body_size=config.WEB_MAX_REQUEST_BODY_BYTES,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(config.MCP_ALLOWED_HOSTS),
            allowed_origins=list(config.MCP_ALLOWED_ORIGINS)))


@asynccontextmanager
async def running_mcp_sessions(app):
    """Enter from the app lifespan: the SDK needs its task group running even in stateless mode."""
    endpoint = getattr(app.state, "mcp_endpoint", None)
    if endpoint is None:
        yield
        return
    endpoint.server.instructions = mcp_instructions()
    endpoint.session_manager = new_session_manager(endpoint.server)
    try:
        async with endpoint.session_manager.run():
            yield
    finally:
        endpoint.session_manager = None


def install_mcp_endpoint(app, limiter) -> None:
    if not config.MCP_ENABLED:
        return
    endpoint = McpHttpEndpoint(build_mcp_server(limiter))
    app.state.mcp_endpoint = endpoint
    app.add_route(config.MCP_PATH, endpoint, include_in_schema=False)


class McpSubdomainMiddleware:
    """On MCP_SUBDOMAIN_HOSTS, "/" and MCP_PATH go to the MCP endpoint and every other path is 404.
    Register it inside RequestLogMiddleware and outside RateLimitMiddleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not config.MCP_ENABLED:
            return await self.app(scope, receive, send)
        host = Headers(scope=scope).get("host", "").split(":")[0].lower()
        if host not in config.MCP_SUBDOMAIN_HOSTS:
            return await self.app(scope, receive, send)
        if scope.get("path") not in ("/", config.MCP_PATH):
            response = JSONResponse({"error_code": "not_found",
                                     "message": f"this host serves only the MCP endpoint (POST {config.MCP_PATH})"},
                                    status_code=404)
            return await response(scope, receive, send)
        scope = {**scope, "path": config.MCP_PATH, "raw_path": config.MCP_PATH.encode()}
        await self.app(scope, receive, send)
