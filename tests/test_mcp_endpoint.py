"""
tests/test_mcp_endpoint.py

MCP endpoint (src/api/mcp_server.py): stateless Streamable HTTP at POST /mcp on both servers and at
the root of MCP_SUBDOMAIN_HOSTS, exposing RESEARCH_TOOL_REGISTRY as MCP tools. Same real data and
replayed network as tests/test_api.py; a tool call's JSON must equal the matching /v1 body.
Cloudflare Access: requests that came through Cloudflare need a valid Cf-Access-Jwt-Assertion.
"""

import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import config
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from tests.conftest import ROLES, X_MK
from tests.test_api import client, network  # noqa: F401  (fixtures)

M1, M2 = ROLES["M1"], ROLES["M2"]
UPSTREAM_TOOLS = {"find_mk", "find_committee", "find_party", "query_bills", "get_bill", "query_votes"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
TEAM_DOMAIN = "knessetlm-test.cloudflareaccess.com"
ACCESS_AUDIENCE = "test-access-aud"


class McpTestClient:
    def __init__(self, http_client: TestClient, path: str = "/mcp"):
        self.http = http_client
        self.path = path

    def post(self, method: str, params: dict | None = None, headers: dict | None = None, path: str | None = None):
        return self.http.post(path or self.path, headers={**MCP_HEADERS, **(headers or {})},
                              json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def result(self, method: str, params: dict | None = None, **kwargs) -> dict:
        response = self.post(method, params, **kwargs)
        assert response.status_code == 200, response.text
        body = response.json()
        assert "result" in body, body
        return body["result"]

    def call_tool(self, name: str, arguments: dict, **kwargs) -> tuple[bool, dict]:
        result = self.result("tools/call", {"name": name, "arguments": arguments}, **kwargs)
        return result["isError"], json.loads(result["content"][0]["text"])


def allow_test_client(monkeypatch, local: bool = True):
    monkeypatch.setattr(config, "MCP_ALLOWED_HOSTS", (*config.MCP_ALLOWED_HOSTS, "testserver"))
    monkeypatch.setattr(config, "MCP_LOCAL_PEER_HOSTS", ("testclient",) if local else ("127.0.0.1",))


@pytest.fixture()
def mcp(sample_db, network, monkeypatch):
    from api.app import app, rate_limiter
    allow_test_client(monkeypatch)
    rate_limiter.reset()
    with TestClient(app) as http_client:
        yield McpTestClient(http_client)
    rate_limiter.reset()


def registry_input_schema(spec) -> dict:
    return {key: value for key, value in spec.schema.items() if key != "description"}


# ── protocol surface ─────────────────────────────────────────────────────────

class TestHandshakeAndListing:
    def test_initialize_gives_server_info_and_the_agent_instructions(self, mcp):
        result = mcp.result("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "pytest", "version": "1"}})
        assert result["serverInfo"]["name"]
        assert "tools" in result["capabilities"]
        llms_text = mcp.http.get("/llms.txt").text
        assert llms_text.strip() in result["instructions"]

    def test_tools_mirror_the_research_registry(self, mcp):
        tools = mcp.result("tools/list")["tools"]
        assert [tool["name"] for tool in tools] == [spec.name for spec in RESEARCH_TOOL_REGISTRY]
        for tool, spec in zip(tools, RESEARCH_TOOL_REGISTRY):
            assert tool["description"] == spec.schema["description"]
            assert tool["inputSchema"] == registry_input_schema(spec)
            assert tool["annotations"]["readOnlyHint"] is True
            assert tool["annotations"]["openWorldHint"] is (spec.name in UPSTREAM_TOOLS)

    def test_get_is_405_in_stateless_mode(self, mcp):
        assert mcp.http.get("/mcp", headers=MCP_HEADERS).status_code == 405


# ── tool calls ───────────────────────────────────────────────────────────────

class TestToolCalls:
    def test_find_mk_equals_rest_body(self, mcp):
        is_error, body = mcp.call_tool("find_mk", {"query": X_MK["full_name"]})
        assert not is_error
        assert body["results"][0]["mk_id"] == X_MK["mk_id"]
        assert body == mcp.http.get("/v1/mks", params={"q": X_MK["full_name"]}).json()

    def test_query_protocols_equals_rest_body(self, mcp):
        is_error, body = mcp.call_tool("query_protocols", {
            "meeting_ids": [M2], "search_in": ["speeches"], "top_k": 2})
        assert not is_error
        assert body == mcp.http.get("/v1/protocols", params={
            "meeting_id": M2, "search_in": "speeches", "top_k": 2}).json()
        assert len(body["results"]["speeches"]) == 2 and "offset=2" in body["hint"]

    def test_numeric_id_sent_as_number_is_accepted(self, mcp):
        is_error, body = mcp.call_tool("query_protocols", {"mk_id": int(X_MK["mk_id"]), "search_in": ["opinions"]})
        assert not is_error
        assert body["results"]["opinions"]
        assert all(row["mk_id"] == X_MK["mk_id"] for row in body["results"]["opinions"])

    def test_attendance(self, mcp):
        is_error, body = mcp.call_tool("get_meeting_attendance", {"meeting_id": M1})
        assert not is_error and body["results"]["attendance"]

    def test_top_k_is_clamped_like_rest(self, mcp):
        is_error, body = mcp.call_tool("find_mk", {"query": "עודד", "top_k": 50})
        assert not is_error and body["args"]["top_k"] == config.API_FIND_MAX_TOP_K

    @pytest.mark.parametrize("tool, arguments, error_code", [
        ("query_protocols", {"mk_id": "abc"}, "invalid_mk_id"),
        ("query_protocols", {"date_from": "yesterday"}, "invalid_date_from"),
        ("query_protocols", {"search_in": ["bullets"]}, "invalid_search_in"),
        ("find_mk", {"query": ""}, "missing_query"),
        ("find_mk", {"query": "עודד", "top_k": "many"}, "invalid_top_k"),
        ("get_meeting_attendance", {"meeting_id": "9999999"}, "meeting_not_found"),
        ("no_such_tool", {}, "unknown_tool"),
    ])
    def test_bad_calls_are_tool_errors(self, mcp, tool, arguments, error_code):
        is_error, body = mcp.call_tool(tool, arguments)
        assert is_error
        assert body["error_code"] == error_code

    def test_server_error_hides_the_exception(self, mcp, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "nope.db")
        is_error, body = mcp.call_tool("query_protocols", {"query": "תקציב"})
        assert is_error and body["error_code"] == "knesset_db_missing"
        assert "nope.db" not in json.dumps(body)


# ── hosts, origins, subdomain ────────────────────────────────────────────────

class TestHostsAndSubdomain:
    def test_unknown_host_is_rejected(self, mcp):
        assert mcp.post("tools/list", headers={"Host": "evil.example"}).status_code == 421

    def test_foreign_origin_is_rejected(self, mcp):
        assert mcp.post("tools/list", headers={"Origin": "https://evil.example"}).status_code == 403

    @pytest.mark.parametrize("path", ["/", "/mcp"])
    def test_mcp_subdomain_serves_mcp(self, mcp, path):
        tools = mcp.result("tools/list", headers={"Host": "mcp.meorav.com"}, path=path)["tools"]
        assert len(tools) == len(RESEARCH_TOOL_REGISTRY)

    @pytest.mark.parametrize("path", ["/v1/mks?q=x", "/llms.txt", "/docs"])
    def test_mcp_subdomain_serves_nothing_else(self, mcp, path):
        assert mcp.http.get(path, headers={"Host": "mcp.meorav.com"}).status_code == 404

    def test_main_host_still_serves_the_site(self, mcp):
        assert mcp.http.get("/llms.txt", headers={"Host": "meorav.com"}).status_code == 200


# ── rate limits ──────────────────────────────────────────────────────────────

class TestRateLimits:
    def test_tool_calls_use_the_rest_route_buckets(self, mcp, monkeypatch):
        monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config, "API_RATE_LIMIT_UPSTREAM_PER_MINUTE", 2)
        assert not mcp.call_tool("find_mk", {"query": "עודד"})[0]
        assert not mcp.call_tool("find_party", {"query": X_MK["party"]})[0]
        is_error, body = mcp.call_tool("find_mk", {"query": "עודד"})
        assert is_error and body["error_code"] == "rate_limited" and body["retry_after_seconds"] >= 1
        assert not mcp.call_tool("get_meeting_attendance", {"meeting_id": M1})[0], "db tools have their own budget"

    def test_rest_and_mcp_share_the_budget(self, mcp, monkeypatch):
        monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config, "API_RATE_LIMIT_UPSTREAM_PER_MINUTE", 1)
        assert mcp.http.get("/v1/mks", params={"q": "עודד"}).status_code == 200
        assert mcp.call_tool("find_mk", {"query": "עודד"})[1]["error_code"] == "rate_limited"

    def test_mcp_requests_count_in_the_web_bucket(self, mcp, monkeypatch):
        monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config, "API_RATE_LIMIT_WEB_PER_MINUTE", 3)
        assert [mcp.post("tools/list").status_code for _ in range(4)] == [200, 200, 200, 429]


# ── Cloudflare Access ────────────────────────────────────────────────────────

def new_signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def access_token(private_key, audience=ACCESS_AUDIENCE, issuer=f"https://{TEAM_DOMAIN}", expires_in=300) -> str:
    now = int(time.time())
    return jwt.encode({"aud": [audience], "iss": issuer, "iat": now, "exp": now + expires_in,
                       "email": "someone@example.com"}, private_key, algorithm="RS256")


@pytest.fixture()
def behind_access(sample_db, network, monkeypatch):
    """Test client treated as a request that came through Cloudflare (peer is not local)."""
    import api.mcp_server as mcp_server
    from api.app import app, rate_limiter
    allow_test_client(monkeypatch, local=False)
    trusted_key = new_signing_key()
    monkeypatch.setattr(config, "CF_ACCESS_TEAM_DOMAIN", TEAM_DOMAIN)
    monkeypatch.setattr(config, "CF_ACCESS_AUDIENCES", (ACCESS_AUDIENCE,))
    monkeypatch.setattr(mcp_server, "cloudflare_access_signing_key", lambda token: trusted_key.public_key())
    rate_limiter.reset()
    with TestClient(app) as http_client:
        yield McpTestClient(http_client), trusted_key
    rate_limiter.reset()


def access_header(token: str) -> dict:
    return {"Cf-Access-Jwt-Assertion": token}


class TestCloudflareAccess:
    def test_valid_token_passes(self, behind_access):
        mcp, trusted_key = behind_access
        assert mcp.result("tools/list", headers=access_header(access_token(trusted_key)))["tools"]

    def test_missing_token_is_403(self, behind_access):
        mcp, _ = behind_access
        assert mcp.post("tools/list").status_code == 403

    @pytest.mark.parametrize("token_kwargs", [
        {"audience": "some-other-app"},
        {"issuer": "https://other-team.cloudflareaccess.com"},
        {"expires_in": -60},
    ])
    def test_wrong_audience_issuer_or_expired_is_403(self, behind_access, token_kwargs):
        mcp, trusted_key = behind_access
        assert mcp.post("tools/list", headers=access_header(access_token(trusted_key, **token_kwargs))).status_code == 403

    def test_token_signed_by_another_key_is_403(self, behind_access):
        mcp, _ = behind_access
        assert mcp.post("tools/list", headers=access_header(access_token(new_signing_key()))).status_code == 403

    def test_garbage_token_is_403(self, behind_access):
        mcp, _ = behind_access
        assert mcp.post("tools/list", headers=access_header("not.a.jwt")).status_code == 403

    def test_unconfigured_access_refuses_everything(self, behind_access, monkeypatch):
        mcp, trusted_key = behind_access
        monkeypatch.setattr(config, "CF_ACCESS_AUDIENCES", ())
        assert mcp.post("tools/list", headers=access_header(access_token(trusted_key))).status_code == 403

    def test_local_peer_with_cloudflare_headers_still_needs_a_token(self, mcp):
        response = mcp.post("tools/list", headers={"CF-Connecting-IP": "1.2.3.4", "Cf-Ray": "abc"})
        assert response.status_code == 403

    def test_rest_routes_are_not_behind_the_mcp_check(self, behind_access):
        mcp, _ = behind_access
        assert mcp.http.get("/v1/mks", params={"q": "עודד"}).status_code == 200

    def test_subdomain_is_behind_the_check_too(self, behind_access):
        mcp, trusted_key = behind_access
        assert mcp.post("tools/list", headers={"Host": "mcp.meorav.com"}, path="/").status_code == 403
        assert mcp.result("tools/list", path="/", headers={
            "Host": "mcp.meorav.com", **access_header(access_token(trusted_key))})["tools"]


# ── web UI server ────────────────────────────────────────────────────────────

class TestWebServer:
    def test_web_app_serves_mcp(self, sample_db, network, monkeypatch):
        import web.app as webapp
        from api.mcp_server import running_mcp_sessions
        allow_test_client(monkeypatch)

        @asynccontextmanager
        async def mcp_only_lifespan(app):
            async with running_mcp_sessions(app):
                yield

        monkeypatch.setattr(webapp.app.router, "lifespan_context", mcp_only_lifespan)
        webapp.rate_limiter.reset()
        with TestClient(webapp.app) as http_client:
            mcp = McpTestClient(http_client)
            assert [t["name"] for t in mcp.result("tools/list")["tools"]] == [s.name for s in RESEARCH_TOOL_REGISTRY]
            is_error, body = mcp.call_tool("get_meeting_attendance", {"meeting_id": M1})
            assert not is_error and body["results"]["attendance"]
        webapp.rate_limiter.reset()
