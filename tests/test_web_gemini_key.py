"""
tests/test_web_gemini_key.py

The auto-research tab runs Gemini on the visitor's own key: the browser keeps it and sends
it as the X-Gemini-Api-Key header; the server checks it with Google before any LLM work,
passes it down to GoogleBackend without storing it, never uses its own env key, and never
falls back to the local model.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import config

VISITOR_KEY = "AIzaSyVisitorKeyForTests_0123456789abc"
KEY_HEADER = {"X-Gemini-Api-Key": VISITOR_KEY}
AGENT_POSTS = [
    ("/api/research/start", {"question": "מה אמרו על דיור ציבורי?"}),
]


@pytest.fixture(autouse=True)
def google_answers(monkeypatch):
    """Google's verdict on keys (True valid, False rejected, None unreachable); records each check."""
    import web.gemini_keys as gemini_keys
    answers = SimpleNamespace(verdict=True, checked=[], real_check=gemini_keys.ask_google_whether_key_is_valid)

    def fake_google_check(key):
        answers.checked.append(key)
        return answers.verdict

    monkeypatch.setattr(gemini_keys, "ask_google_whether_key_is_valid", fake_google_check)
    gemini_keys.forget_gemini_key_verdicts()
    yield answers
    gemini_keys.forget_gemini_key_verdicts()


class FakeRunner:
    created: list["FakeRunner"] = []
    events: list[tuple] = [("token", "תשובה"), ("done", {})]

    def __init__(self, machine, backend, tool_registry, gemini_api_key=None):
        self.gemini_api_key = gemini_api_key
        FakeRunner.created.append(self)

    def run_stream(self, question, resume=None, user_response=None):
        yield from FakeRunner.events


@pytest.fixture()
def web(sample_db, tmp_path, monkeypatch):
    import web.app as webapp
    import web.settings as settings
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = sessions
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    webapp.app.state.backend = None
    webapp.app.state.tool_registry = {}
    monkeypatch.setattr(webapp, "MachineRunner", FakeRunner)
    FakeRunner.created = []
    FakeRunner.events = [("token", "תשובה"), ("done", {})]
    return SimpleNamespace(client=TestClient(webapp.app), sessions=sessions, tmp=tmp_path, app=webapp)


def awaiting_session(web) -> str:
    from web.session import ResearchSession, save_session
    sid = str(uuid.uuid4())
    save_session(ResearchSession(session_id=sid, status="awaiting_user", original_question="שאלה",
                                 created_at="2026-09-26T00:00:00.000Z", updated_at="2026-09-26T00:00:00.000Z",
                                 machine_checkpoint={"question": "שאלה"}), web.sessions)
    return sid


def sse_events(body: str) -> list[str]:
    return [line[len("event: "):] for line in body.splitlines() if line.startswith("event: ")]


def wait_for_session_file(sessions: Path) -> None:
    for _ in range(50):
        if any(sessions.iterdir()):
            return
        time.sleep(0.05)


class TestKeyRequired:
    @pytest.mark.parametrize("path,body", AGENT_POSTS)
    def test_missing_key_is_401_before_anything_runs(self, web, path, body):
        r = web.client.post(path, json=body)
        assert r.status_code == 401
        assert r.json()["error"] == "gemini_key_required"
        assert FakeRunner.created == []

    def test_respond_requires_key(self, web):
        sid = awaiting_session(web)
        r = web.client.post(f"/api/research/{sid}/respond", json={"output_var": "x", "value": "y"})
        assert r.status_code == 401
        assert FakeRunner.created == []

    @pytest.mark.parametrize("bad_key", ["short", "has spaces in it 0123456789", "x" * 300, "key;with=punctuation-0123456789abcdef"])
    def test_malformed_key_is_rejected(self, web, bad_key):
        r = web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers={"X-Gemini-Api-Key": bad_key})
        assert r.status_code == 401

    def test_non_agent_routes_need_no_key(self, web):
        assert web.client.post("/api/browse/search", json={"query": ""}).status_code == 200
        assert web.client.get("/api/meta").status_code == 200


class TestKeyPassedNotStored:
    @pytest.mark.parametrize("path,body", AGENT_POSTS)
    def test_key_reaches_runner(self, web, path, body):
        r = web.client.post(path, json=body, headers=KEY_HEADER)
        assert r.status_code == 200
        assert [runner.gemini_api_key for runner in FakeRunner.created] == [VISITOR_KEY]

    def test_respond_passes_key(self, web):
        sid = awaiting_session(web)
        r = web.client.post(f"/api/research/{sid}/respond", json={"output_var": "x", "value": "y"}, headers=KEY_HEADER)
        assert r.status_code == 200
        assert FakeRunner.created[-1].gemini_api_key == VISITOR_KEY

    def test_key_not_in_session_files_log_or_stream(self, web):
        body = web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers=KEY_HEADER).text
        wait_for_session_file(web.sessions)
        assert any(web.sessions.iterdir())
        assert VISITOR_KEY not in body
        for path in web.tmp.rglob("*"):
            if path.is_file():
                assert VISITOR_KEY not in path.read_text(encoding="utf-8", errors="replace"), path

    def test_key_rejected_mid_run_flags_it_and_stops_the_run(self, web):
        FakeRunner.events = [
            ("status", "שגיאה בתת-גרף (research): ClientError: 400 INVALID_ARGUMENT. API key not valid. "
                       "Please pass a valid API key. reason: API_KEY_INVALID"),
            ("node_start", {"label": "עיצוב תשובה", "stage": "reviewer"}),
            ("token", "local model answer that should never run"),
            ("done", {}),
        ]
        body = web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers=KEY_HEADER).text
        events = sse_events(body)
        assert events.count("gemini_key_invalid") == 1
        assert "error" in events
        assert "node_start" not in events and "done" not in events
        assert "should never run" not in body

    def test_unrelated_errors_do_not_flag_the_key(self, web):
        FakeRunner.events = [("status", "שגיאה בתת-גרף (research): TimeoutError: read timed out"), ("done", {})]
        body = web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers=KEY_HEADER).text
        assert "gemini_key_invalid" not in sse_events(body)


class TestKeyCheckedWithGoogleFirst:
    def test_rejected_key_is_401_and_nothing_runs(self, web, google_answers):
        google_answers.verdict = False
        r = web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers=KEY_HEADER)
        assert r.status_code == 401
        assert r.json()["error"] == "gemini_key_invalid"
        assert FakeRunner.created == []

    def test_unverifiable_key_is_503_and_nothing_runs(self, web, google_answers):
        google_answers.verdict = None
        r = web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers=KEY_HEADER)
        assert r.status_code == 503
        assert r.json()["error"] == "gemini_key_unverified"
        assert FakeRunner.created == []

    def test_verdict_is_cached(self, web, google_answers):
        for _ in range(2):
            assert web.client.post(AGENT_POSTS[0][0], json=AGENT_POSTS[0][1], headers=KEY_HEADER).status_code == 200
        assert google_answers.checked == [VISITOR_KEY]

    def test_unverifiable_verdict_is_not_cached(self, web, google_answers):
        google_answers.verdict = None
        web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers=KEY_HEADER)
        google_answers.verdict = True
        assert web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers=KEY_HEADER).status_code == 200
        assert len(google_answers.checked) == 2

    def test_verdict_cache_is_keyed_by_hash_not_the_key(self, web):
        import web.gemini_keys as gemini_keys
        web.client.post("/api/research/start", json=AGENT_POSTS[0][1], headers=KEY_HEADER)
        assert gemini_keys._verdict_by_key_hash
        assert VISITOR_KEY not in repr(gemini_keys._verdict_by_key_hash)

    def test_google_check_sends_key_in_header_not_url(self, monkeypatch, google_answers):
        import web.gemini_keys as gemini_keys
        sent = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            sent.update(url=url, headers=headers, params=params, timeout=timeout)
            return SimpleNamespace(status_code=400)

        monkeypatch.setattr(gemini_keys.requests, "get", fake_get)
        assert google_answers.real_check(VISITOR_KEY) is False
        assert sent["headers"] == {"x-goog-api-key": VISITOR_KEY}
        assert VISITOR_KEY not in sent["url"] + repr(sent["params"])
        assert sent["timeout"] == config.GEMINI_KEY_CHECK_TIMEOUT_SECONDS


class TestKeyReachesGemini:
    def test_bridge_builds_google_backend_with_visitor_key(self, monkeypatch):
        import agent.llm.google as google
        from agent.subgraph.llm_bridge import LLMBridge
        seen = []
        monkeypatch.setattr(google, "GoogleBackend", lambda model, api_key=None: seen.append((model, api_key)) or object())
        LLMBridge(api_key=VISITOR_KEY)._backend_for("gemini-flash-latest")
        assert seen == [("gemini-flash-latest", VISITOR_KEY)]

    def test_local_model_still_uses_llama_server(self, monkeypatch):
        import agent.llm.gemma as gemma
        from agent.subgraph.llm_bridge import LLMBridge
        monkeypatch.setattr(gemma, "GemmaLlamaBackend", lambda: "local-backend")
        assert LLMBridge(api_key=VISITOR_KEY)._backend_for("local") == "local-backend"

    def test_runner_gives_subgraph_agent_a_bridge_with_the_key(self, minimal_machine_path):
        from agent.machine import StateMachine
        from agent.runner import MachineRunner

        class RecordingAgent:
            def __init__(self, llm_bridge=None):
                self.llm_bridge = llm_bridge

        runner = MachineRunner(StateMachine(minimal_machine_path), backend=None, tool_registry={},
                               gemini_api_key=VISITOR_KEY)
        agent = runner.make_subgraph_agent(RecordingAgent)
        assert agent.llm_bridge._api_key == VISITOR_KEY


class TestServerKeyNeverUsed:
    def test_fallback_to_local_is_off(self):
        assert config.GOOGLE_API_FALLBACK_TO_LOCAL is False

    def test_web_server_drops_its_own_gemini_keys(self, monkeypatch):
        import web.app as webapp
        monkeypatch.setenv(config.GOOGLE_API_KEY_ENV, "server-key")
        monkeypatch.setenv("GEMINI_API_KEY", "server-key")
        monkeypatch.setattr(config, "WEB_REQUIRE_USER_GEMINI_KEY", True)
        webapp.forget_server_gemini_keys()
        assert config.GOOGLE_API_KEY_ENV not in os.environ
        assert "GEMINI_API_KEY" not in os.environ

    def test_server_keys_kept_when_not_required(self, monkeypatch):
        import web.app as webapp
        monkeypatch.setenv(config.GOOGLE_API_KEY_ENV, "server-key")
        monkeypatch.setattr(config, "WEB_REQUIRE_USER_GEMINI_KEY", False)
        webapp.forget_server_gemini_keys()
        assert os.environ[config.GOOGLE_API_KEY_ENV] == "server-key"


class TestAgentRateLimit:
    def test_agent_routes_share_a_per_ip_budget(self, web, monkeypatch):
        monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config, "API_RATE_LIMIT_AGENT_PER_MINUTE", 2)
        web.app.rate_limiter.reset()
        statuses = [web.client.post(AGENT_POSTS[0][0], json=AGENT_POSTS[0][1], headers=KEY_HEADER).status_code for _ in range(2)]
        sid = awaiting_session(web)
        r = web.client.post(f"/api/research/{sid}/respond", json={"output_var": "x", "value": "y"}, headers=KEY_HEADER)
        assert statuses == [200, 200]
        assert r.status_code == 429
        assert r.json()["error_code"] == "rate_limited"
        web.app.rate_limiter.reset()

    def test_workspace_ask_counts_against_the_agent_budget(self):
        from api.rate_limit import route_bucket
        assert route_bucket(f"/api/research/{uuid.uuid4()}/workspace/ask") == "agent"
        assert route_bucket(f"/api/research/{uuid.uuid4()}/respond") == "agent"
        assert route_bucket("/api/research/start") == "agent"
        assert route_bucket("/api/query") is None
        assert route_bucket(f"/api/research/{uuid.uuid4()}/stream") is None
        assert route_bucket("/api/browse/search") == "db"


class TestSessionsCannotBeDeleted:
    def test_delete_route_is_gone_and_session_stays(self, web):
        sid = awaiting_session(web)
        r = web.client.delete(f"/api/research/{sid}")
        assert r.status_code in (404, 405)
        assert (web.sessions / f"{sid}.json").exists()


def test_legacy_query_route_is_gone(web):
    assert web.client.post("/api/query", json=AGENT_POSTS[0][1], headers=KEY_HEADER).status_code in (404, 405)
