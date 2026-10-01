"""
tests/test_request_log.py

Server logs under config.LOG_DIR: one JSON line per HTTP request (requests.jsonl), visitor questions
(questions.jsonl) and server-side tracebacks (errors.log). Clients get a request id, never a traceback,
and the visitor's Gemini key is never written anywhere.
"""

import json
import logging.handlers
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import config
from api import request_log

VISITOR_KEY = "AIzaSyVisitorKeyForTests_0123456789abc"
KEY_HEADER = {"X-Gemini-Api-Key": VISITOR_KEY}
QUESTION = "מה אמרו על דיור ציבורי?"
SECRET_PATH_FRAGMENT = "C:\\secret\\internal\\module.py"


@pytest.fixture()
def log_dir(tmp_path, monkeypatch):
    directory = tmp_path / "logs"
    monkeypatch.setattr(config, "LOG_DIR", directory)
    request_log.setup_file_logging()
    yield directory
    request_log.close_file_logging()


@pytest.fixture()
def google_answers(monkeypatch):
    import web.gemini_keys as gemini_keys
    answers = SimpleNamespace(verdict=True)
    monkeypatch.setattr(gemini_keys, "ask_google_whether_key_is_valid", lambda key: answers.verdict)
    gemini_keys.forget_gemini_key_verdicts()
    yield answers
    gemini_keys.forget_gemini_key_verdicts()


class FakeRunner:
    events: list = []

    def __init__(self, machine, backend, tool_registry, gemini_api_key=None):
        pass

    def run_stream(self, question, resume=None, user_response=None):
        for event in FakeRunner.events:
            if isinstance(event, Exception):
                raise event
            yield event


@pytest.fixture()
def web(real_db, tmp_path, monkeypatch, google_answers, log_dir):
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
    FakeRunner.events = [("token", "תשובה"), ("done", {})]
    return SimpleNamespace(client=TestClient(webapp.app), sessions=sessions, app=webapp, logs=log_dir,
                           google=google_answers)


def log_lines(log_dir: Path, name: str) -> list[dict]:
    path = log_dir / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sse_events(body: str) -> list[tuple[str, dict]]:
    events, event_name = [], None
    for line in body.splitlines():
        if line.startswith("event: "):
            event_name = line[len("event: "):]
        elif line.startswith("data: ") and event_name:
            events.append((event_name, json.loads(line[len("data: "):])))
    return events


class TestRequestLine:
    def test_one_json_line_with_expected_fields(self, web):
        response = web.client.get("/api/help?x=1", headers={"User-Agent": "pytest-agent", "CF-Ray": "abc-TLV",
                                                            "CF-IPCountry": "IL"})
        assert response.status_code == 200
        lines = log_lines(web.logs, "requests.jsonl")
        assert len(lines) == 1
        line = lines[0]
        assert line["method"] == "GET" and line["path"] == "/api/help" and line["query"] == "x=1"
        assert line["status"] == 200
        assert line["id"] == response.headers["X-Request-Id"]
        assert line["ts"].endswith("Z")
        assert line["ms"] >= 0 and line["ttfb_ms"] >= 0 and line["bytes"] == len(response.content)
        assert line["bucket"] == "web"
        assert line["ua"] == "pytest-agent" and line["cf_ray"] == "abc-TLV" and line["country"] == "IL"
        assert line["gemini_key_sent"] is False
        assert line["error"] is None

    def test_cloudflare_ip_used_only_from_trusted_peer(self, web, monkeypatch):
        web.client.get("/api/help", headers={"CF-Connecting-IP": "203.0.113.7"})
        monkeypatch.setattr(config, "API_TRUSTED_PROXY_HOSTS", ("testclient",))
        web.client.get("/api/help", headers={"CF-Connecting-IP": "203.0.113.7"})
        assert [line["ip"] for line in log_lines(web.logs, "requests.jsonl")] == ["testclient", "203.0.113.7"]

    def test_long_query_and_user_agent_are_capped(self, web):
        web.client.get("/api/help?q=" + "a" * 1000, headers={"User-Agent": "u" * 1000})
        line = log_lines(web.logs, "requests.jsonl")[0]
        assert len(line["query"]) == config.REQUEST_LOG_MAX_QUERY_CHARS
        assert len(line["ua"]) == config.REQUEST_LOG_MAX_USER_AGENT_CHARS

    def test_request_id_header_on_every_response(self, web):
        first = web.client.get("/api/help").headers["X-Request-Id"]
        second = web.client.get("/api/help").headers["X-Request-Id"]
        assert first and second and first != second

    def test_standalone_api_logs_requests(self, log_dir):
        from api.app import app as public_api
        response = TestClient(public_api).get("/agent-instructions")
        lines = log_lines(log_dir, "requests.jsonl")
        assert [line["path"] for line in lines] == ["/agent-instructions"]
        assert lines[0]["id"] == response.headers["X-Request-Id"]


class TestRejectedRequestsAreLogged:
    def test_rate_limited_request(self, web, monkeypatch):
        monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config, "API_RATE_LIMIT_WEB_PER_MINUTE", 1)
        web.app.rate_limiter.reset()
        try:
            statuses = [web.client.get("/api/help").status_code for _ in range(2)]
        finally:
            web.app.rate_limiter.reset()
        assert statuses == [200, 429]
        assert [line["status"] for line in log_lines(web.logs, "requests.jsonl")] == [200, 429]

    def test_body_too_large(self, web, monkeypatch):
        monkeypatch.setattr(config, "WEB_MAX_REQUEST_BODY_BYTES", 10)
        response = web.client.post("/api/browse/search", json={"query": "ביטחון המדינה"})
        assert response.status_code == 413
        assert log_lines(web.logs, "requests.jsonl")[0]["status"] == 413


class TestGeminiKey:
    def test_key_value_never_logged(self, web):
        web.client.post("/api/research/start", json={"question": QUESTION}, headers=KEY_HEADER)
        web.client.get("/api/help", headers=KEY_HEADER)
        log_files = [path for path in web.logs.rglob("*") if path.is_file()]
        assert log_files
        for path in log_files:
            assert VISITOR_KEY not in path.read_text(encoding="utf-8"), path
        assert all(line["gemini_key_sent"] is True for line in log_lines(web.logs, "requests.jsonl"))

    @pytest.mark.parametrize("verdict,headers,outcome", [
        (True, KEY_HEADER, "ok"),
        (False, KEY_HEADER, "invalid"),
        (None, KEY_HEADER, "unverified"),
        (True, {}, "missing"),
    ])
    def test_key_check_outcome_is_recorded(self, web, verdict, headers, outcome):
        web.google.verdict = verdict
        web.client.post("/api/research/start", json={"question": QUESTION}, headers=headers)
        assert log_lines(web.logs, "requests.jsonl")[0]["gemini_key_outcome"] == outcome

    def test_key_rejected_mid_run_is_recorded(self, web):
        FakeRunner.events = [("status", "ClientError: 400 API key not valid. reason: API_KEY_INVALID"), ("done", {})]
        web.client.post("/api/research/start", json={"question": QUESTION}, headers=KEY_HEADER)
        assert log_lines(web.logs, "requests.jsonl")[0]["gemini_key_outcome"] == "rejected_mid_run"


class TestQuestions:
    def test_research_start_question_is_logged(self, web):
        body = web.client.post("/api/research/start", json={"question": QUESTION}, headers=KEY_HEADER).text
        session_id = sse_events(body)[0][1]["session_id"]
        questions = log_lines(web.logs, "questions.jsonl")
        assert len(questions) == 1
        assert questions[0]["q"] == QUESTION
        assert questions[0]["route"] == "/api/research/start"
        assert questions[0]["session_id"] == session_id
        assert questions[0]["id"] == log_lines(web.logs, "requests.jsonl")[0]["id"]
        assert log_lines(web.logs, "requests.jsonl")[0]["session_id"] == session_id


class TestErrorsStayServerSide:
    def test_failed_run_sends_generic_message_and_logs_traceback(self, web):
        FakeRunner.events = [("token", "חלק"), RuntimeError(f"boom in {SECRET_PATH_FRAGMENT}")]
        response = web.client.post("/api/research/start", json={"question": QUESTION}, headers=KEY_HEADER)
        request_id = response.headers["X-Request-Id"]
        events = sse_events(response.text)
        session_id = events[0][1]["session_id"]
        error = next(data for name, data in events if name == "error")
        assert config.WEB_GENERIC_ERROR_MESSAGE in error["error"]
        assert error["request_id"] == request_id
        assert "Traceback" not in response.text and "boom" not in response.text
        assert "secret" not in response.text

        from web.session import load_session
        session = load_session(session_id, web.sessions)
        assert session.status == "error"
        assert config.WEB_GENERIC_ERROR_MESSAGE in session.error and "boom" not in session.error
        replay = web.client.get(f"/api/research/{session_id}/stream").text
        assert "boom" not in replay and "Traceback" not in replay

        errors = (web.logs / "errors.log").read_text(encoding="utf-8")
        assert request_id in errors and "Traceback" in errors and "boom" in errors

    def test_unhandled_route_exception_is_logged(self, log_dir):
        from fastapi import FastAPI
        app = FastAPI()
        app.add_middleware(request_log.RequestLogMiddleware)

        @app.get("/explode")
        def explode():
            raise ValueError("kaboom")

        response = TestClient(app, raise_server_exceptions=False).get("/explode")
        assert response.status_code == 500
        line = log_lines(log_dir, "requests.jsonl")[0]
        assert line["status"] == 500 and line["error"] == "ValueError"
        errors = (log_dir / "errors.log").read_text(encoding="utf-8")
        assert line["id"] in errors and "kaboom" in errors and "Traceback" in errors


    def test_public_api_5xx_traceback_goes_to_errors_log(self, log_dir, monkeypatch):
        import api.routes as routes
        from api.app import app as public_api
        from utils.tools import ToolEnvelope

        def failing_dispatch(registry, name, args, args_already_validated=False):
            return ToolEnvelope(summary="", full="", error="dispatch_exception",
                                metadata={"exception": "db exploded", "traceback": "Traceback (most recent call last):\n  x"},
                                provenance={"tool_name": name})

        monkeypatch.setattr(routes, "dispatch", failing_dispatch)
        response = TestClient(public_api).get("/v1/mks?q=abc")
        assert response.status_code == 500
        assert "db exploded" not in response.text and "Traceback" not in response.text
        errors = (log_dir / "errors.log").read_text(encoding="utf-8")
        assert response.headers["X-Request-Id"] in errors and "db exploded" in errors and "Traceback" in errors


class TestFileHandlers:
    def test_rotation_config(self, log_dir):
        handlers = request_log.file_handlers()
        assert sorted(Path(handler.baseFilename).name for handler in handlers) == [
            "errors.log", "questions.jsonl", "requests.jsonl"]
        for handler in handlers:
            assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
            assert handler.when == "MIDNIGHT" and handler.utc is True
            assert handler.backupCount == config.LOG_RETENTION_DAYS
            assert Path(handler.baseFilename).parent == log_dir

    def test_setup_is_idempotent(self, log_dir):
        request_log.setup_file_logging()
        assert len(request_log.file_handlers()) == 3


def test_request_ids_are_short_and_unique():
    ids = {request_log.new_request_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(len(request_id) <= 16 for request_id in ids)
