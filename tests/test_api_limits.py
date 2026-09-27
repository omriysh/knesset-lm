"""
tests/test_api_limits.py

Operational limits of the public API: per-IP rate limits, upstream retry/timeout settings of
the standalone server, the requests-cache session behind the Knesset API calls, and the SQLite
query timeout. Uses the real sampled rows (sample_db) and the recorded OData replies
(tests/test_api.py fixtures).
"""

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import requests
import requests_cache
import urllib3
from fastapi.testclient import TestClient

import config
import utils.knesset_db as kdb
import utils.tools as tools
from retrieval import knesset_db_store as store
from tests.conftest import SAMPLE
from tests.test_api import client, network  # noqa: F401  (fixtures)
from tests.test_odata_tools import META, RESULTS

REAL_WORD = max((w for w in SAMPLE["topics"][0]["text"].split() if w.isalpha()), key=len)


# ── rate limits ──────────────────────────────────────────────────────────────

@pytest.fixture()
def limited(client, monkeypatch):
    from api.app import rate_limiter
    monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(config, "API_RATE_LIMIT_UPSTREAM_PER_MINUTE", 2)
    monkeypatch.setattr(config, "API_RATE_LIMIT_DB_PER_MINUTE", 3)
    rate_limiter.reset()
    yield client
    rate_limiter.reset()


class TestRateLimit:
    def test_upstream_routes_share_the_low_limit(self, limited):
        assert limited.get("/v1/bills", params={"q": META["bill_query"]}).status_code == 200
        assert limited.get("/v1/votes", params={"q": META["vote_topic"], "top_k": 5}).status_code == 200
        r = limited.get("/v1/mks", params={"q": "עודד"})
        assert r.status_code == 429
        assert r.json()["error_code"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1

    def test_db_routes_have_their_own_budget(self, limited):
        for _ in range(2):
            limited.get("/v1/bills", params={"q": META["bill_query"]})
        for _ in range(3):
            assert limited.get("/v1/protocols", params={"q": REAL_WORD}).status_code == 200
        assert limited.get("/v1/protocols", params={"q": REAL_WORD}).status_code == 429

    def test_docs_are_not_limited(self, limited, monkeypatch):
        monkeypatch.setattr(config, "API_RATE_LIMIT_WEB_PER_MINUTE", 3)
        for _ in range(10):
            assert limited.get("/llms.txt").status_code == 200
            assert limited.get("/docs").status_code == 200
        assert [limited.get("/health").status_code for _ in range(4)] == [200, 200, 200, 429]

    def test_cloudflare_client_ip_only_when_trusted(self, limited, monkeypatch):
        from api.app import rate_limiter
        monkeypatch.setattr(config, "API_TRUST_CLOUDFLARE_IP_HEADER", False)

        def statuses(ip):
            return [limited.get("/v1/bills", params={"q": META["bill_query"]},
                                headers={"CF-Connecting-IP": ip}).status_code for _ in range(3)]
        assert statuses("1.1.1.1") == [200, 200, 429]
        assert statuses("2.2.2.2")[0] == 429, "header ignored unless trusted: same socket peer"
        monkeypatch.setattr(config, "API_TRUST_CLOUDFLARE_IP_HEADER", True)
        rate_limiter.reset()
        assert statuses("1.1.1.1") == [200, 200, 429]
        assert statuses("2.2.2.2")[0] == 429, "trusted only from the local cloudflared peer"
        monkeypatch.setattr(config, "API_TRUSTED_PROXY_HOSTS", ("testclient",))
        rate_limiter.reset()
        assert statuses("1.1.1.1") == [200, 200, 429]
        assert statuses("2.2.2.2") == [200, 200, 429]

    def test_web_ui_server_is_rate_limited_too(self, sample_db, network, monkeypatch, tmp_path):
        import web.app as webapp
        import web.settings as settings
        monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config, "API_RATE_LIMIT_DB_PER_MINUTE", 1)
        webapp.app.state.settings = settings
        webapp.app.state.sessions_dir = tmp_path
        webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
        webapp.rate_limiter.reset()
        web_client = TestClient(webapp.app)
        statuses = [web_client.get("/v1/protocols", params={"q": REAL_WORD}).status_code for _ in range(2)]
        webapp.rate_limiter.reset()
        assert statuses == [200, 429]


# ── upstream retries / timeout ───────────────────────────────────────────────

class TestUpstreamRetry:
    def test_public_server_settings(self, monkeypatch):
        for name in ("API_RETRY_ATTEMPTS", "API_RETRY_SLEEP", "HTTP_TIMEOUT_SECONDS"):
            monkeypatch.setattr(config, name, getattr(config, name))
        from api.app import use_public_api_http_settings
        use_public_api_http_settings()
        assert config.API_RETRY_ATTEMPTS == config.PUBLIC_API_RETRY_ATTEMPTS == 2
        assert config.HTTP_TIMEOUT_SECONDS == config.PUBLIC_API_HTTP_TIMEOUT_SECONDS == 10

    def test_timeouts_retry_twice_with_the_configured_timeout(self, monkeypatch):
        monkeypatch.setattr(config, "API_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr(config, "API_RETRY_SLEEP", 0)
        monkeypatch.setattr(config, "HTTP_TIMEOUT_SECONDS", 10)
        timeouts = []

        def timing_out(url, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            raise requests.exceptions.Timeout("read timed out")
        monkeypatch.setattr(requests, "get", timing_out)
        env = tools.handle_query_bills({"query": META["bill_query"]})
        assert env.error == "odata_request_failed"
        assert timeouts == [10, 10]


# ── requests-cache session ───────────────────────────────────────────────────

@pytest.fixture()
def cached_session(monkeypatch):
    """A real CachedSession (in-memory backend) whose transport serves the recorded bill search."""
    session = requests_cache.CachedSession(backend="memory", expire_after=config.CACHE_TTL)
    sent = []
    payload = json.dumps({"value": RESULTS["search_bills"]}).encode()

    def send(adapter, request, **kwargs):
        sent.append(request.url)
        raw = urllib3.HTTPResponse(body=io.BytesIO(payload), status=200,
                                   headers={"Content-Type": "application/json"}, preload_content=False)
        return adapter.build_response(request, raw)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    monkeypatch.setattr(kdb, "HTTP_SESSION", session)
    return sent


class TestCache:
    def test_repeated_tool_call_is_served_from_cache(self, cached_session):
        first = tools.handle_query_bills({"query": META["bill_query"]})
        second = tools.handle_query_bills({"query": META["bill_query"]})
        assert first.error is None and first.full == second.full
        assert json.loads(first.full)
        assert len(cached_session) == 1

    def test_bill_documents_bypass_the_cache(self, monkeypatch):
        class NoCacheSession:
            def get(self, *args, **kwargs):
                raise AssertionError("document download must not go through the cache session")

        class DocumentResponse:
            status_code = 200
            headers = {"Content-Length": "3"}
            url = "https://fs.knesset.gov.il/doc.pdf"

            def raise_for_status(self):
                pass

            def iter_content(self, chunk_size):
                yield b"pdf"

            def close(self):
                pass
        downloaded = []
        monkeypatch.setattr(kdb, "HTTP_SESSION", NoCacheSession())
        monkeypatch.setattr(requests, "get", lambda url, **kwargs: downloaded.append(url) or DocumentResponse())
        assert kdb._download_bill_document("https://fs.knesset.gov.il/doc.pdf") == b"pdf"
        assert downloaded == ["https://fs.knesset.gov.il/doc.pdf"]

    def test_votes_expire_sooner_than_the_default_ttl(self):
        from utils.cache import SESSION
        vote_ttls = [ttl for pattern, ttl in SESSION.settings.urls_expire_after.items() if "PlenumVote" in pattern]
        assert vote_ttls and all(ttl == config.CACHE_TTL_VOTES < config.CACHE_TTL for ttl in vote_ttls)

    def test_knesset_db_uses_the_cache_session_by_default(self):
        import subprocess
        src = Path(__file__).parent.parent / "src"
        check = ("import sys; sys.path.insert(0, sys.argv[1]); import utils.knesset_db as k, utils.cache as c; "
                 "sys.exit(0 if k.HTTP_SESSION is c.SESSION else 1)")
        assert subprocess.run([sys.executable, "-c", check, str(src)], capture_output=True).returncode == 0


# ── SQLite query timeout ─────────────────────────────────────────────────────

class TestQueryTimeout:
    def test_default_is_twenty_seconds(self):
        assert config.DB_QUERY_TIMEOUT_SECONDS == 20

    def test_handler_reports_query_timeout(self, sample_db, monkeypatch):
        monkeypatch.setattr(config, "DB_QUERY_TIMEOUT_SECONDS", -1)
        monkeypatch.setattr(store, "_PROGRESS_HANDLER_OPCODES", 1)  # the sample db is tiny
        env = tools.handle_query_protocols({"query": REAL_WORD, "search_in": ["speeches"]})
        assert env.error == "query_timeout"

    def test_api_returns_503_with_advice(self, client, monkeypatch):
        monkeypatch.setattr(config, "DB_QUERY_TIMEOUT_SECONDS", -1)
        monkeypatch.setattr(store, "_PROGRESS_HANDLER_OPCODES", 1)  # the sample db is tiny
        r = client.get("/v1/protocols", params={"q": REAL_WORD})
        assert r.status_code == 503
        body = r.json()
        assert body["error_code"] == "query_timeout"
        assert "filter" in body["message"] and "interrupted" not in body["message"]

    def test_normal_queries_unaffected(self, client):
        assert client.get("/v1/protocols", params={"q": REAL_WORD}).status_code == 200
