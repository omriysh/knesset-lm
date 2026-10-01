"""
tests/test_web_security.py

Hardening of the public web server (web.app behind a Cloudflare tunnel): MK photo path
allowlist, request/body caps, workspace routes, research respond validation, concurrency
slots and client-disconnect stops, rate-limit buckets, reading-tab input limits and SQLite
timeout, fail-fast upstream settings, security headers + CSP, sanitized markdown on the
frontend, and no server paths in response bodies. Runs on the real knesset.db (conftest.real_db).
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
from retrieval import knesset_db_store as store
from tests.test_web_gemini_key import KEY_HEADER, FakeRunner, google_answers, sse_events, web  # noqa: F401

WEB_DIR = Path(__file__).parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"
WELL_FORMED_MEETING_ID = "2199065"


def save(web, status="awaiting_user", checkpoint=None, workspace_data=None) -> str:
    from web.session import ResearchSession, save_session
    sid = str(uuid.uuid4())
    save_session(ResearchSession(session_id=sid, status=status, original_question="שאלה",
                                 created_at="2026-09-26T00:00:00.000Z", updated_at="2026-09-26T00:00:00.000Z",
                                 machine_checkpoint=checkpoint, workspace_data=workspace_data), web.sessions)
    return sid


def load(web, sid):
    from web.session import load_session
    return load_session(sid, web.sessions)


def paused_at(ui_event: dict) -> dict:
    return {"question": "שאלה", "pending_ui_event": ui_event}


COST_GATE = {"ui": "option_select", "output_var": "approve", "multi_select": False,
             "options": [{"value": True, "label": "כן"}, {"value": False, "label": "לא"}]}
TEXT_INPUT = {"ui": "text_input", "output_var": "clarification"}
DEEP_DIVE = {"ui": "deep_dive", "output_var": "selected_meetings"}


class RecordingRunner(FakeRunner):
    responses: list = []

    def run_stream(self, question, resume=None, user_response=None):
        RecordingRunner.responses.append(user_response)
        yield from FakeRunner.events


@pytest.fixture()
def recording(web, monkeypatch):
    monkeypatch.setattr(web.app, "MachineRunner", RecordingRunner)
    RecordingRunner.responses = []
    return web


# ── H1 /mk-photo ─────────────────────────────────────────────────────────────

@pytest.fixture()
def photos(web, real_db, tmp_path, monkeypatch):
    photos_dir = tmp_path / "mk_photos"
    photos_dir.mkdir()
    (photos_dir / f"{real_db.mk_name}.jpg").write_bytes(b"\xff\xd8\xff mk photo")
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG outside the photos dir")
    monkeypatch.setattr(config, "MK_PHOTOS_DIR", photos_dir)
    return SimpleNamespace(dir=photos_dir, secret=secret, mk_name=real_db.mk_name)


class TestMkPhoto:
    def test_known_mk_photo_is_served(self, web, photos):
        r = web.client.get(f"/mk-photo/{quote(photos.mk_name)}")
        assert r.status_code == 200
        assert r.content.startswith(b"\xff\xd8\xff")

    def test_honorific_prefix_still_resolves(self, web, photos):
        assert web.client.get(f"/mk-photo/{quote('ח' + chr(34) + 'כ ' + photos.mk_name)}").status_code == 200

    @pytest.mark.parametrize("make_name", [
        lambda secret: str(secret.with_suffix("")),
        lambda secret: "..\\" + secret.stem,
        lambda secret: "../" + secret.stem,
        lambda secret: "..%5C" + secret.stem,
        lambda secret: "%2E%2E%5C" + secret.stem,
        lambda secret: str(secret.with_suffix("")).replace("\\", "/"),
    ])
    def test_files_outside_the_photos_dir_are_never_served(self, web, photos, make_name):
        name = make_name(photos.secret)
        for path in (f"/mk-photo/{quote(name, safe='')}", f"/mk-photo/{quote(name, safe='%')}"):
            r = web.client.get(path)
            assert r.status_code == 404, path
            assert b"PNG outside" not in r.content

    def test_long_names_are_404_without_lookup(self, web, photos, monkeypatch):
        monkeypatch.setattr(web.app, "_get_mk_fuzzy_index", lambda: pytest.fail("fuzzy search on a long name"))
        r = web.client.get("/mk-photo/" + quote("א" * (config.WEB_MAX_MK_PHOTO_NAME_CHARS + 1)))
        assert r.status_code == 404

    def test_resolution_cache_is_bounded(self, web, photos, monkeypatch):
        monkeypatch.setattr(config, "WEB_MK_PHOTO_CACHE_MAX_ENTRIES", 5)
        for i in range(20):
            web.client.get(f"/mk-photo/{quote('אורח ' + str(i))}")
        assert len(web.app._mk_photo_cache) <= 5

    def test_photo_route_is_rate_limited(self):
        from api.rate_limit import route_bucket
        assert route_bucket("/mk-photo/x") == "web"


# ── H2 request body limit + workspace/select ─────────────────────────────────

class TestBodyLimit:
    def test_oversized_body_is_413(self, web):
        sid = save(web, status="done", workspace_data={"selected_chunks": []})
        body = {"chunk_id": "1", "source_meeting_id": WELL_FORMED_MEETING_ID, "text": "א" * config.WEB_MAX_REQUEST_BODY_BYTES}
        r = web.client.post(f"/api/research/{sid}/workspace/select", json=body)
        assert r.status_code == 413
        assert load(web, sid).workspace_data == {"selected_chunks": []}

    def test_chunked_post_without_length_is_rejected(self, web):
        r = web.client.post("/api/browse/search", content=(part for part in [b'{"query": ""}']),
                            headers={"Content-Type": "application/json"})
        assert r.status_code == 411

    def test_get_requests_pass(self, web):
        assert web.client.get("/api/help").status_code == 200


class TestWorkspaceSelect:
    def url(self, sid):
        return f"/api/research/{sid}/workspace/select"

    def test_valid_chunk_is_stored(self, web):
        sid = save(web, status="done", workspace_data={"selected_chunks": []})
        r = web.client.post(self.url(sid), json={"chunk_id": "3", "text": "קטע", "source_meeting_id": WELL_FORMED_MEETING_ID})
        assert r.status_code == 200 and r.json()["total_selected"] == 1
        assert load(web, sid).workspace_data["selected_chunks"][0]["text"] == "קטע"

    @pytest.mark.parametrize("body", [
        {"chunk_id": "3", "text": "א" * (config.WEB_MAX_WORKSPACE_CHUNK_CHARS + 1), "source_meeting_id": WELL_FORMED_MEETING_ID},
        {"chunk_id": "3", "text": "", "source_meeting_id": WELL_FORMED_MEETING_ID},
        {"chunk_id": "abc", "text": "קטע", "source_meeting_id": WELL_FORMED_MEETING_ID},
        {"chunk_id": "١٢", "text": "קטע", "source_meeting_id": WELL_FORMED_MEETING_ID},
        {"chunk_id": "3", "text": "קטע", "source_meeting_id": "../../etc"},
        {"chunk_id": "3", "text": "קטע", "source_meeting_id": "²"},
        {"chunk_id": "3", "text": ["list"], "source_meeting_id": WELL_FORMED_MEETING_ID},
    ])
    def test_invalid_fields_are_400(self, web, body):
        sid = save(web, status="done", workspace_data={"selected_chunks": []})
        assert web.client.post(self.url(sid), json=body).status_code == 400
        assert load(web, sid).workspace_data == {"selected_chunks": []}

    def test_selected_chunk_count_is_capped(self, web, monkeypatch):
        monkeypatch.setattr(config, "WEB_MAX_WORKSPACE_SELECTED_CHUNKS", 2)
        sid = save(web, status="done", workspace_data={"selected_chunks": []})
        statuses = [web.client.post(self.url(sid), json={"chunk_id": str(i), "text": "קטע", "source_meeting_id": WELL_FORMED_MEETING_ID})
                    .status_code for i in range(3)]
        assert statuses == [200, 200, 400]
        assert len(load(web, sid).workspace_data["selected_chunks"]) == 2

    def test_select_is_rate_limited(self):
        from api.rate_limit import route_bucket
        assert route_bucket(f"/api/research/{uuid.uuid4()}/workspace/select") == "web"


class TestStaleSessionCleanup:
    def test_stale_sessions_are_removed_while_serving(self, web, monkeypatch):
        import web.session as session_module
        old = save(web, status="done")
        three_hours_ago = time.time() - 3 * 3600
        os.utime(web.sessions / f"{old}.json", (three_hours_ago, three_hours_ago))
        monkeypatch.setattr(session_module, "_last_cleanup_monotonic", None)
        assert web.client.post("/api/browse/search", json={"query": ""}).status_code == 200
        assert not (web.sessions / f"{old}.json").exists()

    def test_cleanup_runs_at_most_once_per_interval(self, web, monkeypatch):
        import web.session as session_module
        runs = []
        monkeypatch.setattr(session_module, "cleanup_stale_sessions", lambda d, max_age_hours: runs.append(d) or 0)
        monkeypatch.setattr(session_module, "_last_cleanup_monotonic", None)
        for _ in range(3):
            session_module.maybe_cleanup_stale_sessions(web.sessions)
        assert len(runs) == 1


# ── H3 workspace/ask ─────────────────────────────────────────────────────────

class FakeLocalBackend:
    def __init__(self, tokens=("תשובה", " קצרה")):
        self.tokens = tokens
        self.stream_calls = []
        self.prepare_calls = []
        self.closed = False

    def prepare_messages(self, messages, suppress_thinking=False):
        self.prepare_calls.append(suppress_thinking)
        return messages

    def stream(self, messages, tools=None, temperature=None, max_tokens=None):
        from agent.llm.base import TokenEvent
        self.stream_calls.append(max_tokens)
        try:
            for token in self.tokens:
                yield TokenEvent(token)
        finally:
            self.closed = True


@pytest.fixture()
def local_llm(web):
    backend = FakeLocalBackend()
    web.app.app.state.backend = backend
    return backend


def ask_url(sid):
    return f"/api/research/{sid}/workspace/ask"


class TestWorkspaceAsk:
    def test_requires_a_valid_gemini_key(self, web, local_llm):
        sid = save(web, status="done")
        r = web.client.post(ask_url(sid), json={"question": "מה נאמר?"})
        assert r.status_code == 401
        assert local_llm.stream_calls == []

    def test_rejected_key_is_401(self, web, local_llm, google_answers):
        google_answers.verdict = False
        r = web.client.post(ask_url(save(web, status="done")), json={"question": "מה נאמר?"}, headers=KEY_HEADER)
        assert r.status_code == 401 and local_llm.stream_calls == []

    def test_answers_with_capped_tokens_and_no_thinking(self, web, local_llm):
        r = web.client.post(ask_url(save(web, status="done")), json={"question": "מה נאמר?"}, headers=KEY_HEADER)
        assert r.status_code == 200
        assert sse_events(r.text) == ["token", "token", "done"]
        assert local_llm.stream_calls == [config.WEB_WORKSPACE_ASK_MAX_TOKENS]
        assert local_llm.prepare_calls == [True]

    def test_busy_local_llm_is_503(self, web, local_llm, monkeypatch):
        slots = threading.BoundedSemaphore(1)
        monkeypatch.setattr(web.app, "_LOCAL_LLM_ASK_SLOTS", slots)
        assert slots.acquire(blocking=False)
        r = web.client.post(ask_url(save(web, status="done")), json={"question": "מה נאמר?"}, headers=KEY_HEADER)
        assert r.status_code == 503 and r.json()["error"] == "busy"
        assert local_llm.stream_calls == []
        slots.release()

    def test_slot_is_released_after_the_answer(self, web, local_llm, monkeypatch):
        slots = threading.BoundedSemaphore(1)
        monkeypatch.setattr(web.app, "_LOCAL_LLM_ASK_SLOTS", slots)
        for _ in range(2):
            assert web.client.post(ask_url(save(web, status="done")), json={"question": "מה נאמר?"},
                                   headers=KEY_HEADER).status_code == 200
        assert slots.acquire(blocking=False)

    @pytest.mark.parametrize("body", [
        {"question": "מה נאמר?", "meeting_id": "١٢٣"},
        {"question": "מה נאמר?", "meeting_id": "../x"},
        {"question": "<script>alert(1)</script>"},
        {"question": "א" * 2001},
    ])
    def test_invalid_input_is_400(self, web, local_llm, body):
        r = web.client.post(ask_url(save(web, status="done")), json=body, headers=KEY_HEADER)
        assert r.status_code == 400
        assert local_llm.stream_calls == []

    def test_generation_stops_when_the_client_is_gone(self):
        import web.app as webapp
        backend = FakeLocalBackend(tokens=[f"t{i}" for i in range(100)])
        stop_requested = threading.Event()
        emitted = []

        def emit(item):
            emitted.append(item)
            stop_requested.set()

        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        webapp._stream_local_llm_answer(backend, [], emit, stop_requested, slots)
        tokens = [item for item in emitted if not isinstance(item, tuple) and item is not webapp._SENTINEL]
        assert len(tokens) <= 1
        assert backend.closed
        assert emitted[-1] is webapp._SENTINEL
        assert slots.acquire(blocking=False)

    def test_ask_counts_against_the_agent_budget(self):
        from api.rate_limit import route_bucket
        assert route_bucket(ask_url(uuid.uuid4())) == "agent"


# ── M2 respond ───────────────────────────────────────────────────────────────

def respond(web, sid, output_var, value):
    return web.client.post(f"/api/research/{sid}/respond", json={"output_var": output_var, "value": value},
                           headers=KEY_HEADER)


class TestRespondValidation:
    def test_output_var_must_match_the_pending_input(self, recording):
        sid = save(recording, checkpoint=paused_at(TEXT_INPUT))
        r = respond(recording, sid, "question", "שאלה חדשה")
        assert r.status_code == 400
        assert RecordingRunner.responses == []
        assert load(recording, sid).status == "awaiting_user"

    def test_session_without_pending_input_is_rejected(self, recording):
        sid = save(recording, checkpoint={"question": "שאלה"})
        assert respond(recording, sid, "x", "כן").status_code == 400

    @pytest.mark.parametrize("value", [True, False])
    def test_option_select_offered_value_passes(self, recording, value):
        sid = save(recording, checkpoint=paused_at(COST_GATE))
        assert respond(recording, sid, "approve", value).status_code == 200
        assert RecordingRunner.responses == [{"output_var": "approve", "value": value}]

    @pytest.mark.parametrize("value", [1, 0, "true", None, [True], {"a": 1}, pytest.param("א" * 30000, id="long_text")])
    def test_option_select_other_values_are_400(self, recording, value):
        sid = save(recording, checkpoint=paused_at(COST_GATE))
        assert respond(recording, sid, "approve", value).status_code == 400
        assert RecordingRunner.responses == []

    def test_multi_select_subset_passes(self, recording):
        multi = {**COST_GATE, "multi_select": True,
                 "options": [{"value": "א"}, {"value": "ב"}, {"value": "ג"}]}
        sid = save(recording, checkpoint=paused_at(multi))
        assert respond(recording, sid, "approve", ["א", "ג"]).status_code == 200
        sid = save(recording, checkpoint=paused_at(multi))
        assert respond(recording, sid, "approve", ["א", "ד"]).status_code == 400
        sid = save(recording, checkpoint=paused_at(multi))
        assert respond(recording, sid, "approve", "א").status_code == 400

    def test_text_input_uses_the_question_rules(self, recording):
        sid = save(recording, checkpoint=paused_at(TEXT_INPUT))
        assert respond(recording, sid, "clarification", "<img src=x onerror=alert(1)>").status_code == 400
        assert respond(recording, sid, "clarification", "א" * 24000).status_code == 400
        assert respond(recording, sid, "clarification", ["לא", "טקסט"]).status_code == 400
        assert respond(recording, sid, "clarification", "  מה עם ועדת הכספים?  ").status_code == 200
        assert RecordingRunner.responses == [{"output_var": "clarification", "value": "מה עם ועדת הכספים?"}]

    def test_deep_dive_takes_meeting_ids(self, recording, monkeypatch):
        monkeypatch.setattr(config, "WEB_MAX_DEEP_DIVE_MEETINGS", 2)
        for value in (["١٢"], ["12", "x"], "123", ["1", "2", "3"], [12]):
            sid = save(recording, checkpoint=paused_at(DEEP_DIVE))
            assert respond(recording, sid, "selected_meetings", value).status_code == 400, value
        sid = save(recording, checkpoint=paused_at(DEEP_DIVE))
        assert respond(recording, sid, "selected_meetings", [WELL_FORMED_MEETING_ID, "2199062"]).status_code == 200

    def test_unknown_ui_type_is_rejected(self, recording):
        sid = save(recording, checkpoint=paused_at({"ui": "free_json", "output_var": "x"}))
        assert respond(recording, sid, "x", "כן").status_code == 400

    def test_output_var_length_is_capped(self, recording):
        sid = save(recording, checkpoint=paused_at(TEXT_INPUT))
        assert respond(recording, sid, "x" * (config.WEB_MAX_OUTPUT_VAR_CHARS + 1), "כן").status_code == 400


class BlockingRunner(FakeRunner):
    release = threading.Event()

    def run_stream(self, question, resume=None, user_response=None):
        BlockingRunner.release.wait(10)
        yield from FakeRunner.events


class TestRespondRace:
    def test_second_parallel_respond_is_409(self, web, monkeypatch):
        monkeypatch.setattr(web.app, "MachineRunner", BlockingRunner)
        BlockingRunner.release = threading.Event()
        sid = save(web, checkpoint=paused_at(TEXT_INPUT))
        first = {}
        thread = threading.Thread(target=lambda: first.update(r=respond(web, sid, "clarification", "כן")))
        thread.start()
        for _ in range(100):
            if load(web, sid).status == "running":
                break
            time.sleep(0.05)
        assert load(web, sid).status == "running"
        second = respond(web, sid, "clarification", "כן")
        BlockingRunner.release.set()
        thread.join(10)
        assert second.status_code == 409
        assert first["r"].status_code == 200
        assert len(FakeRunner.created) == 1


# ── M6 research slots + stop on disconnect ───────────────────────────────────

START = ("/api/research/start", {"question": "מה אמרו על דיור ציבורי?"})


class TestResearchSlots:
    def test_full_queue_is_503(self, web, monkeypatch):
        from web.concurrency import ResearchRunSlots
        slots = ResearchRunSlots(max_running=1)
        assert slots.acquire(threading.Event(), on_queued=lambda: None)
        monkeypatch.setattr(web.app, "_RESEARCH_SLOTS", slots)
        monkeypatch.setattr(config, "WEB_RESEARCH_MAX_QUEUED_RUNS", 0)
        r = web.client.post(START[0], json=START[1], headers=KEY_HEADER)
        assert r.status_code == 503 and r.json()["error"] == "busy"
        assert FakeRunner.created == []

    def test_waiting_too_long_is_a_busy_error_event(self, web, monkeypatch):
        from web.concurrency import ResearchRunSlots
        slots = ResearchRunSlots(max_running=1)
        assert slots.acquire(threading.Event(), on_queued=lambda: None)
        monkeypatch.setattr(web.app, "_RESEARCH_SLOTS", slots)
        monkeypatch.setattr(config, "WEB_RESEARCH_SLOT_WAIT_SECONDS", 0.2)
        r = web.client.post(START[0], json=START[1], headers=KEY_HEADER)
        events = sse_events(r.text)
        assert events[:2] == ["session_id", "queued"] and events[-1] == "error"
        assert FakeRunner.created == []
        sid = json.loads(r.text.split("data: ", 1)[1].split("\n", 1)[0])["session_id"]
        assert load(web, sid).status == "error"
        assert slots.waiting_count() == 0

    def test_slot_is_freed_after_each_run(self, web, monkeypatch):
        from web.concurrency import ResearchRunSlots
        slots = ResearchRunSlots(max_running=1)
        monkeypatch.setattr(web.app, "_RESEARCH_SLOTS", slots)
        monkeypatch.setattr(config, "WEB_RESEARCH_SLOT_WAIT_SECONDS", 0.5)
        for _ in range(3):
            assert sse_events(web.client.post(START[0], json=START[1], headers=KEY_HEADER).text)[-1] == "done"


class EndlessRunner:
    closed = False

    def __init__(self, *args, **kwargs):
        pass

    def run_stream(self, question, resume=None, user_response=None):
        try:
            for i in range(1000):
                yield ("token", f"t{i}")
            yield ("done", {})
        finally:
            EndlessRunner.closed = True


class PausingRunner(EndlessRunner):
    def run_stream(self, question, resume=None, user_response=None):
        yield ("user_input_required", {**TEXT_INPUT, "checkpoint": paused_at(TEXT_INPUT)})


def research_run(web, sid, stop_requested, emit, **overrides):
    import web.app as webapp
    from web.concurrency import ResearchRunSlots
    fields = dict(session_id=sid, question="שאלה", created_at="2026-09-26T00:00:00.000Z",
                  sessions_dir=web.sessions, machine=None, backend=None, tool_registry={}, gemini_api_key=None,
                  resume=None, user_response=None, workspace_data=None, install_llm_event_sink=False,
                  log_prefix="test", emit=emit, stop_requested=stop_requested,
                  slots=ResearchRunSlots(max_running=1))
    fields.update(overrides)
    return webapp.ResearchRun(**fields)


class TestStopOnDisconnect:
    def test_run_stops_and_session_is_marked(self, web, monkeypatch):
        import web.app as webapp
        monkeypatch.setattr(webapp, "MachineRunner", EndlessRunner)
        EndlessRunner.closed = False
        sid = save(web, status="running")
        stop_requested = threading.Event()
        emitted = []

        def emit(item):
            emitted.append(item)
            if isinstance(item, tuple) and item[0] == "token":
                stop_requested.set()

        run = research_run(web, sid, stop_requested, emit)
        webapp.run_research(run)
        assert len([item for item in emitted if isinstance(item, tuple) and item[0] == "token"]) == 1
        assert EndlessRunner.closed
        assert emitted[-1] is webapp._SENTINEL
        assert load(web, sid).status == "error"
        assert run.slots.acquire(threading.Event(), on_queued=lambda: None)

    def test_paused_run_is_saved_awaiting_user_even_after_disconnect(self, web, monkeypatch):
        import web.app as webapp
        monkeypatch.setattr(webapp, "MachineRunner", PausingRunner)
        sid = save(web, status="running")
        stop_requested = threading.Event()
        order = []

        def emit(item):
            if isinstance(item, tuple) and item[0] == "user_input_required":
                order.append(load(web, sid).status)
                stop_requested.set()

        webapp.run_research(research_run(web, sid, stop_requested, emit, workspace_data={"selected_chunks": []}))
        saved = load(web, sid)
        assert order == ["awaiting_user"], "session saved before the client learns it may respond"
        assert saved.status == "awaiting_user"
        assert saved.machine_checkpoint["pending_ui_event"]["output_var"] == "clarification"
        assert saved.workspace_data == {"selected_chunks": []}


# ── M3 rate-limit buckets + ASCII ids ────────────────────────────────────────

class TestRateLimitBuckets:
    @pytest.mark.parametrize("path", [
        "/api/research/{sid}/meeting/1/transcript", "/api/research/{sid}/meeting/1/participants",
        "/api/research/{sid}/meeting/1/hits", "/api/research/{sid}/meeting/1/summary",
        "/api/research/{sid}/tool_result/0123456789abcdef", "/api/research/{sid}/workspace/select",
        "/api/meta", "/api/help", "/v1/tools", "/mk-photo/x", "/no-such-route", "/health",
    ])
    def test_web_routes_are_limited(self, path):
        from api.rate_limit import route_bucket
        assert route_bucket(path.format(sid=uuid.uuid4())) == "web"

    @pytest.mark.parametrize("path", ["/api/health", "/v1/meta"])
    def test_db_count_routes_share_the_db_budget(self, path):
        from api.rate_limit import route_bucket
        assert route_bucket(path) == "db"

    @pytest.mark.parametrize("path", [
        "/", "/favicon.ico", "/static/app.js", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json",
        "/llms.txt", "/llms-full.txt", "/agent-instructions", "/api/research/{sid}/stream",
    ])
    def test_unlimited_routes(self, path):
        from api.rate_limit import route_bucket
        assert route_bucket(path.format(sid=uuid.uuid4())) is None

    def test_web_bucket_limit_applies(self, web, monkeypatch):
        monkeypatch.setattr(config, "API_RATE_LIMIT_ENABLED", True)
        monkeypatch.setattr(config, "API_RATE_LIMIT_WEB_PER_MINUTE", 2)
        web.app.rate_limiter.reset()
        statuses = [web.client.get("/api/help").status_code for _ in range(3)]
        assert web.client.get("/").status_code == 200
        web.app.rate_limiter.reset()
        assert statuses == [200, 200, 429]


class TestAsciiIds:
    @pytest.mark.parametrize("route", ["summary", "transcript", "participants", "hits"])
    @pytest.mark.parametrize("meeting_id", ["١٢٣", "²", "12a", "1" * 30])
    def test_non_ascii_digit_meeting_ids_are_400(self, web, route, meeting_id):
        sid = save(web, status="done")
        r = web.client.get(f"/api/research/{sid}/meeting/{quote(meeting_id)}/{route}", params={"q": "העלייה"})
        assert r.status_code == 400


# ── M4 reading tab inputs + query timeout ────────────────────────────────────

def browse(web, **body):
    return web.client.post("/api/browse/search", json=body)


class TestBrowseLimits:
    @pytest.mark.parametrize("body", [
        {"query": "א" * (config.API_MAX_QUERY_CHARS + 1)},
        {"query": " ".join(["מילה"] * (config.API_MAX_QUERY_WORDS + 1))},
        {"filters": {"mks": ["x" * 50] * (config.API_MAX_LIST_ITEMS + 1)}},
        {"filters": {"parties": ["x"] * (config.API_MAX_LIST_ITEMS + 1)}},
        {"filters": {"committees": ["x"] * (config.API_MAX_LIST_ITEMS + 1)}},
        {"filters": {"mks": ["א" * (config.API_MAX_NAME_CHARS + 1)]}},
        {"filters": {"guest": "א" * (config.API_MAX_NAME_CHARS + 1)}},
        {"filters": {"date_from": "2023-13-01"}},
        {"filters": {"date_to": "yesterday"}},
        {"sort": "random()"},
        {"filters": {"mks": "not a list"}},
    ])
    def test_out_of_bounds_input_is_400(self, web, body):
        r = browse(web, **body)
        assert r.status_code == 400
        assert r.json().get("error")

    @pytest.mark.parametrize("wildcard", ["%", "_"])
    def test_guest_like_wildcards_are_literal(self, web, real_conn, wildcard):
        response = browse(web, filters={"guest": wildcard})
        assert response.status_code == 200
        guests_with_the_literal_character = {r[0] for r in real_conn.execute(
            "SELECT DISTINCT a.meeting_id FROM attendance a JOIN meetings m ON m.meeting_id = a.meeting_id "
            "WHERE a.mk_id IS NULL AND instr(a.name, ?) > 0 AND (m.is_protocol IS NULL OR m.is_protocol != 0)",
            (wildcard,))}
        assert len(guests_with_the_literal_character) < 50
        assert {m["meeting_id"] for m in response.json()["meetings"]} == guests_with_the_literal_character

    def test_fuzzy_index_is_built_once(self, web, monkeypatch):
        import web.app as webapp
        loads = []
        real_load = webapp._load_mk_fuzzy_index
        monkeypatch.setattr(webapp, "_load_mk_fuzzy_index", lambda k: loads.append(k) or real_load(k))
        monkeypatch.setattr(webapp, "_mk_fuzzy_index", None)
        monkeypatch.setattr(webapp, "_mk_fuzzy_loaded", False)
        for _ in range(3):
            assert browse(web, filters={"mks": ["גלעד קריב"]}).status_code == 200
        assert len(loads) == 1

    def test_slow_query_is_503(self, web, monkeypatch):
        monkeypatch.setattr(config, "DB_QUERY_TIMEOUT_SECONDS", -1)
        monkeypatch.setattr(store, "_PROGRESS_HANDLER_OPCODES", 1)
        r = browse(web, query="העלייה")
        assert r.status_code == 503
        assert "interrupted" not in r.text

    def test_slow_hits_query_is_503(self, web, monkeypatch):
        monkeypatch.setattr(config, "DB_QUERY_TIMEOUT_SECONDS", -1)
        monkeypatch.setattr(store, "_PROGRESS_HANDLER_OPCODES", 1)
        sid = save(web, status="done")
        assert web.client.get(f"/api/research/{sid}/meeting/{WELL_FORMED_MEETING_ID}/hits", params={"q": "העלייה"}).status_code == 503

    def test_hits_query_is_capped(self, web):
        sid = save(web, status="done")
        r = web.client.get(f"/api/research/{sid}/meeting/{WELL_FORMED_MEETING_ID}/hits",
                           params={"q": "א" * (config.WEB_MAX_HITS_QUERY_CHARS + 1)})
        assert r.status_code == 400


# ── M5 fail-fast upstream + /api/meta cache ──────────────────────────────────

class TestWebProcessSettings:
    def test_lifespan_applies_the_public_http_settings(self, web, monkeypatch, tmp_path):
        import web.app as webapp
        import web.settings as settings
        for name in ("API_RETRY_ATTEMPTS", "API_RETRY_SLEEP", "HTTP_TIMEOUT_SECONDS"):
            monkeypatch.setattr(config, name, getattr(config, name))
        monkeypatch.setattr(config, "API_RETRY_ATTEMPTS", 5)
        monkeypatch.setattr(config, "WEB_REQUIRE_USER_GEMINI_KEY", False)
        monkeypatch.setattr(webapp, "StateMachine", lambda path: SimpleNamespace(name="m", version=1))
        monkeypatch.setattr(webapp, "GemmaLlamaBackend", lambda url: None)
        monkeypatch.setattr(webapp, "build_tool_registry", lambda machine, **kwargs: {})
        monkeypatch.setattr(settings, "SESSIONS_DIR", tmp_path / "lifespan_sessions")

        async def enter_and_leave():
            async with webapp.lifespan(webapp.app):
                pass

        asyncio.run(enter_and_leave())
        assert config.API_RETRY_ATTEMPTS == config.PUBLIC_API_RETRY_ATTEMPTS
        assert config.API_RETRY_SLEEP == config.PUBLIC_API_RETRY_SLEEP
        assert config.HTTP_TIMEOUT_SECONDS == config.PUBLIC_API_HTTP_TIMEOUT_SECONDS

    def test_meta_is_cached_in_memory(self, web, monkeypatch):
        import utils.knesset_db as kdb
        calls = []
        monkeypatch.setattr(kdb, "get_all_committees", lambda k: calls.append("c") or [{"Name": "ועדה"}])
        monkeypatch.setattr(kdb, "get_all_mks", lambda k: calls.append("m") or [
            {"mk_individual_first_name": "גלעד", "mk_individual_name": "קריב"}])
        monkeypatch.setattr(kdb, "get_all_parties", lambda k: calls.append("p") or [{"party": "העבודה"}])
        web.app.forget_meta_cache()
        bodies = [web.client.get("/api/meta").json() for _ in range(3)]
        assert bodies[0] == bodies[2] == {"committees": ["ועדה"], "mks": ["גלעד קריב"], "parties": ["העבודה"]}
        assert sorted(calls) == ["c", "m", "p"]
        web.app.forget_meta_cache()

    def test_meta_failure_is_not_cached(self, web, monkeypatch):
        import utils.knesset_db as kdb
        attempts = []

        def failing(k):
            attempts.append(k)
            raise RuntimeError("oknesset down")

        monkeypatch.setattr(kdb, "get_all_committees", failing)
        monkeypatch.setattr(kdb, "get_all_mks", lambda k: [])
        monkeypatch.setattr(kdb, "get_all_parties", lambda k: [])
        web.app.forget_meta_cache()
        for _ in range(2):
            r = web.client.get("/api/meta")
            assert r.status_code == 503 and "oknesset down" not in r.text
        assert len(attempts) == 2
        web.app.forget_meta_cache()


# ── M8 security headers + CSP ────────────────────────────────────────────────

def csp_directives(policy: str) -> dict[str, list[str]]:
    directives = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            directives[words[0]] = words[1:]
    return directives


def source_matches(source: str, url: str) -> bool:
    """CSP host-source matching: a bare origin allows the whole host, a path ending in '/' is a prefix,
    any other path must equal the URL's path (the query string is never compared)."""
    origin = re.match(r"https://[^/?#]+", url).group(0)
    path = re.sub(r"[?#].*", "", url[len(origin):]) or "/"
    source_origin = re.match(r"https://[^/?#]+", source)
    if not source_origin or source_origin.group(0) != origin:
        return False
    source_path = source[len(origin):]
    if not source_path:
        return True
    return path.startswith(source_path) if source_path.endswith("/") else path == source_path


def allowed(policy: str, directive: str, url: str) -> bool:
    sources = csp_directives(policy).get(directive) or csp_directives(policy)["default-src"]
    return any(source_matches(source, url) for source in sources)


class TestSecurityHeaders:
    @pytest.mark.parametrize("path", ["/", "/static/app.js", "/docs", "/redoc", "/openapi.json", "/api/help",
                                      "/no-such-route"])
    def test_headers_on_every_response(self, web, path):
        r = web.client.get(path)
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        policy = csp_directives(r.headers["Content-Security-Policy"])
        assert policy["frame-ancestors"] == ["'none'"]
        assert policy["object-src"] == ["'none'"]
        assert policy["base-uri"] == ["'none'"]
        assert policy["connect-src"] == ["'self'"]

    def test_rejections_carry_the_headers_too(self, web):
        r = web.client.post("/api/browse/search", content=(p for p in [b"{}"]))
        assert r.status_code == 411 and r.headers["X-Frame-Options"] == "DENY"

    def test_index_external_resources_are_allowed_by_the_page_csp(self, web):
        r = web.client.get("/")
        policy = r.headers["Content-Security-Policy"]
        assert "'unsafe-eval'" not in policy
        scripts = re.findall(r'<script[^>]+src="(https://[^"]+)"', r.text)
        styles = re.findall(r'<link[^>]+href="(https://[^"]+)"[^>]*rel="stylesheet"', r.text)
        assert scripts and styles
        assert all(allowed(policy, "script-src", url) for url in scripts), scripts
        assert all(allowed(policy, "style-src", url) for url in styles), styles
        assert "https://fonts.gstatic.com" in csp_directives(policy)["font-src"]

    @pytest.mark.parametrize("path", ["/docs", "/redoc"])
    def test_docs_assets_are_allowed_by_the_docs_csp(self, web, path):
        r = web.client.get(path)
        assert r.status_code == 200
        policy = r.headers["Content-Security-Policy"]
        for tag, attr, directive in (("script", "src", "script-src"), ("link", "href", "style-src")):
            for url in re.findall(rf'<{tag}[^>]+{attr}="(https://[^"]+)"', r.text):
                kind = "img-src" if "favicon" in url or url.endswith(".png") else directive
                assert allowed(policy, kind, url), (kind, url)
        assert "'unsafe-inline'" in csp_directives(policy)["script-src"]

    def test_page_script_src_has_no_inline_and_no_whole_hosts(self, web):
        script_sources = csp_directives(web.client.get("/").headers["Content-Security-Policy"])["script-src"]
        assert "'unsafe-inline'" not in script_sources and "'unsafe-eval'" not in script_sources
        for source in script_sources:
            if source == "'self'":
                continue
            assert re.fullmatch(r"https://[a-z0-9.-]+/[^*\s]*[^/*\s]", source), source

    def test_tailwind_is_pinned_to_one_version(self, web):
        r = web.client.get("/")
        tailwind = re.findall(r'<script[^>]+src="(https://cdn\.tailwindcss\.com[^"]*)"', r.text)
        assert len(tailwind) == 1 and re.match(r"https://cdn\.tailwindcss\.com/\d+\.\d+\.\d+\?", tailwind[0])
        assert allowed(r.headers["Content-Security-Policy"], "script-src", tailwind[0])

    def test_docs_and_openapi_stay_public(self, web):
        assert web.client.get("/openapi.json").status_code == 200
        assert web.client.get("/docs").status_code == 200


# ── M7 sanitized markdown on the frontend ────────────────────────────────────

class TestFrontendSanitizing:
    def index_html(self) -> str:
        return (WEB_DIR / "templates" / "index.html").read_text(encoding="utf-8")

    def static_js(self) -> dict[str, str]:
        return {str(p.relative_to(STATIC_DIR)): p.read_text(encoding="utf-8") for p in STATIC_DIR.rglob("*.js")}

    def test_cdn_scripts_are_pinned_with_sri(self):
        tags = re.findall(r"<script[^>]+src=\"https://cdn\.jsdelivr\.net[^>]*>", self.index_html())
        assert len(tags) == 2
        for tag in tags:
            assert re.search(r"@\d+\.\d+\.\d+/", tag), tag
            assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]{64}"', tag), tag
            assert 'crossorigin="anonymous"' in tag, tag
        assert any("dompurify" in tag for tag in tags) and any("marked" in tag for tag in tags)

    def test_markdown_helper_loads_after_its_libraries(self):
        html = self.index_html()
        assert html.index("dompurify@") < html.index("/static/markdown.js")
        assert html.index("marked@") < html.index("/static/markdown.js") < html.index("/static/browser.js")

    def test_every_marked_call_goes_through_the_hardened_sanitizer(self):
        for name, source in self.static_js().items():
            if name == "markdown.js":
                assert source.count("sanitizeHtml(marked.") == source.count("marked.parse")
                assert source.count("DOMPurify.sanitize(") == 1
                continue
            assert "marked.parse" not in source, name
            assert "DOMPurify.sanitize" not in source, name

    def test_sanitizer_forbids_page_hijacking_markup(self):
        source = (STATIC_DIR / "markdown.js").read_text(encoding="utf-8")
        forbidden_tags = re.search(r"FORBID_TAGS:\s*\[([^\]]*)\]", source).group(1)
        for tag in ("form", "input", "textarea", "select", "button", "style", "iframe", "object", "embed",
                    "img", "image", "video", "audio", "source", "picture", "track"):
            assert f"'{tag}'" in forbidden_tags, tag
        forbidden_attributes = re.search(r"FORBID_ATTR:\s*\[([^\]]*)\]", source).group(1)
        for attribute in ("style", "class", "id"):
            assert f"'{attribute}'" in forbidden_attributes, attribute
        assert "ALLOW_DATA_ATTR: false" in source
        assert "addHook('afterSanitizeAttributes'" in source
        assert "noopener noreferrer" in source

    def test_no_inline_event_handlers_in_templates_or_js_html(self):
        sources = {**self.static_js(), "index.html": self.index_html()}
        for name, source in sources.items():
            assert not re.search(r"""\son[a-z]+\s*=\s*["'`]""", source), name
            assert "javascript:" not in source, name

    def test_no_inline_script_blocks(self):
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", self.index_html())

    def test_every_declared_action_is_registered(self):
        actions_source = (STATIC_DIR / "actions.js").read_text(encoding="utf-8")
        registered = set(re.findall(r"^\s{2}(\w+):", actions_source, flags=re.MULTILINE))
        sources = {**self.static_js(), "index.html": self.index_html()}
        used = {action for source in sources.values()
                for action in re.findall(r'data-(?:click|input|change|enter)="(\w+)"', source)}
        assert used and used <= registered, used - registered

    def test_only_own_action_names_resolve(self):
        actions_source = (STATIC_DIR / "actions.js").read_text(encoding="utf-8")
        assert "Object.hasOwn(PAGE_ACTIONS," in actions_source
        assert not re.search(r"PAGE_ACTIONS\[el\.getAttribute", actions_source)

    def test_data_attribute_interpolations_are_escaped(self):
        escapers = ("_esc(", "esc(", "_rfEsc(", "encodeURIComponent(", "CSS.escape(")
        for name, source in self.static_js().items():
            for value in re.findall(r'data-[a-z-]+="\$\{([^}]*)\}"', source):
                assert value.startswith(escapers), (name, value)

    def test_actions_script_loads_before_the_app(self):
        html = self.index_html()
        assert html.index("/static/filters.js") < html.index("/static/actions.js") < html.index("/static/app.js")

    def test_no_data_interpolated_into_inline_handlers(self):
        for name, source in self.static_js().items():
            assert not re.search(r"on[a-z]+=\"[^\"]*\$\{", source), name


# ── cited meeting info (date + committee) from knesset.db ────────────────────

class TestMeetingInfo:
    def test_real_meeting_resolves_from_the_db(self, web, real_db):
        year, month, day = real_db.meeting_date.split("-")
        assert web.app._get_meeting_info(real_db.meeting_id) == {"date": f"{day}/{month}/{year}",
                                                                 "committee": real_db.meeting_committee}

    @pytest.mark.parametrize("meeting_id", ["../../x", "1 OR 1", "٣", "²", "1" * 5000, "", "999999999"])
    def test_malformed_or_unknown_ids_are_not_found(self, web, monkeypatch, meeting_id):
        import glob
        monkeypatch.setattr(glob, "glob", lambda *a, **k: pytest.fail("no filesystem scan"))
        assert web.app._get_meeting_info(meeting_id) == {}

    def test_citations_are_enriched_from_the_db(self, web, real_db):
        footnotes = {"ev_1": {"ui": {"enrich_fields": ["meeting_id"]}}}
        [citation] = web.app._enrich_citations(
            [{"ev_id": "ev_1", "quote": {"meeting_id": real_db.meeting_id, "text": "x"}}], footnotes)
        assert citation["quote"]["committee"] == real_db.meeting_committee
        assert citation["quote"]["date"].endswith(real_db.meeting_date[:4])


# ── L2 no server paths in responses ──────────────────────────────────────────

class TestNoServerPaths:
    def test_health_has_no_db_path(self, web, real_db):
        r = web.client.get("/api/health")
        assert r.status_code == 200
        assert "db_path" not in r.json()
        assert str(real_db.db_path) not in r.text and real_db.db_path.name not in r.text

    def test_transcript_404_has_no_path(self, web, monkeypatch, tmp_path):
        import web.app as webapp
        missing = tmp_path / "private" / "01_01_2023_999.json"
        monkeypatch.setattr(webapp, "get_transcript_path_from_id", lambda mid: missing)
        r = web.client.get(f"/api/research/{save(web, status='done')}/meeting/999/transcript")
        assert r.status_code == 404
        assert "private" not in r.text and str(tmp_path) not in r.text

    def test_missing_db_hides_path(self, web, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "nowhere" / "knesset.db")
        r = browse(web, query="")
        assert r.status_code == 503
        assert "nowhere" not in r.text and "build_knesset_db" not in r.text


class TestPlenumMeetingIds:
    """Plenum sessions have meeting_id "p" + PlenumSessionID; the meeting-id checks accept them."""

    @pytest.mark.parametrize("value,ok", [("p2245272", True), ("2199065", True), ("p", False), ("pp1", False),
                                          ("P1", False), ("p²", False), ("../p1", False)])
    def test_ok_meeting_id(self, value, ok):
        import web.app as webapp
        assert webapp._ok_meeting_id(value) is ok

    def test_workspace_chunk_may_come_from_a_plenum_session(self):
        import pydantic
        import web.app as webapp
        assert webapp.WorkspaceSelectRequest(chunk_id="3", text="קטע", source_meeting_id="p2245272").source_meeting_id == "p2245272"
        with pytest.raises(pydantic.ValidationError):
            webapp.WorkspaceSelectRequest(chunk_id="p3", text="קטע", source_meeting_id="p2245272")
