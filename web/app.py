"""
app.py

FastAPI web application for the KnessetLM agent.

Startup
-------
The lifespan context manager loads the singletons once:
  - StateMachine       (agent graph)
  - tool_registry      (raises at startup if any machine tool_name is unknown)

Protocol search is keyword-only (FTS5 over Data/knesset.db); no embedding
model or vector store is loaded.

Routes (main)
-------------
  GET  /                                            -> index.html
  GET  /api/health                                  -> {"status", "machine", "db": {table: row count}}
  POST /api/browse/search                           -> reading-tab meeting search
  GET  /api/research/{sid}/meeting/{mid}/hits?q=... -> matching speeches for the heatmap

Usage (via scripts/run_web.py)
-------------------------------
  cd knesset-lm
  python scripts/run_web.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Callable

# Bootstrap sys.path before importing knesset-lm modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import config
from summarization import prompts as summary_prompts
from api import validation as valid
from api.app import use_public_api_http_settings
from api.rate_limit import RateLimitMiddleware, SlidingWindowRateLimiter
from api.request_log import (RequestLogMiddleware, generic_error_message, log_question, log_server_error,
                             request_id_of, setup_file_logging)
from api.routes import router as api_router
from api.docs import install_public_docs
from api.mcp_server import McpSubdomainMiddleware, install_mcp_endpoint, running_mcp_sessions
from api.validation import install_error_handlers
from web.concurrency import ResearchRunSlots
from web.game import router as game_router
from web.gemini_keys import (forget_server_gemini_keys, gemini_text_models, stop_on_rejected_gemini_key,
                              unavailable_models_error, visitor_gemini_key_or_error)
from web.middleware import RequestBodyLimitMiddleware, SecurityHeadersMiddleware
from web.profiles import PROFILE_PAGE_PATHS, router as profiles_router
from agent.llm.google import GoogleBackend
from agent.model_choice import MODEL_ID_PATTERN, ResearchModels

# ── Tool-result lazy-load cache ───────────────────────────────────────────────
# Maps ref_id → full tool result text.  Populated when subgraph step_completed
# events are streamed; served by GET /api/research/{sid}/tool_result/{ref_id}.
_TOOL_RESULT_CACHE: dict[str, str] = {}
_TOOL_RESULT_LOCK = threading.Lock()
_TOOL_RESULT_CAP  = 5000

_RESEARCH_SLOTS = ResearchRunSlots(max_running=config.WEB_RESEARCH_MAX_CONCURRENT_RUNS)
_WORKSPACE_ASK_SLOTS = threading.BoundedSemaphore(config.WEB_WORKSPACE_MAX_CONCURRENT_ASKS)
_SENTINEL = object()
_KEEP_ALIVE = object()
BUSY_MESSAGE = "השרת עמוס כרגע. נסו שוב בעוד כמה דקות."
STOPPED_MESSAGE = "הריצה הופסקה כי החיבור לדפדפן נותק."

# ── Input validators ──────────────────────────────────────────────────────────
_UUID_RE     = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
# Hebrew block + whitespace + digits + dots/commas/question marks (user spec) + !/:-"'  (natural Hebrew punctuation)
_QUESTION_RE = re.compile(r'[֐-׿\s\d.,?!:\-"\']+')
_REF_ID_RE   = re.compile(r'[0-9a-f]{16}')
_MAX_QUESTION = 2000
_MAX_TOP_K    = 500


def _ok_session_id(sid: str) -> bool:
    return bool(_UUID_RE.fullmatch(sid))


def _ok_question(q: str) -> bool:
    return bool(q) and len(q) <= _MAX_QUESTION and bool(_QUESTION_RE.fullmatch(q))


def _clean_question(question: str) -> str:
    """valid.clean_text for a visitor question: ApiInputError (→ 400) on lone surrogates or over the length cap."""
    return valid.clean_text(question, "question", _MAX_QUESTION)


def _ok_ref_id(rid: str) -> bool:
    return bool(_REF_ID_RE.fullmatch(rid))


def _ok_numeric_id(value: str) -> bool:
    """ASCII digits only: str.isdigit() also accepts '²' and Arabic-Indic digits."""
    return value.isascii() and value.isdigit() and len(value) <= config.API_MAX_ID_DIGITS


def _ok_meeting_id(value: str) -> bool:
    """A committee meeting id (digits) or a plenum session id ("p" + digits)."""
    return _ok_numeric_id(value[1:] if value.startswith(config.PLENUM_MEETING_ID_PREFIX) else value)


def research_llm_backend(gemini_api_key: str | None, model: str) -> GoogleBackend:
    return GoogleBackend(model=model, api_key=gemini_api_key)


def _busy_response() -> JSONResponse:
    return JSONResponse({"error": "busy", "message": BUSY_MESSAGE}, status_code=503)


# ── Cited meeting info (meeting_id → {date, committee}) ──────────────────────

def _get_meeting_info(meeting_id: str) -> dict:
    """{date: DD/MM/YYYY, committee} of a knesset.db meeting; {} for a malformed or unknown meeting_id
    (the id comes from LLM / tool output, so it is checked before the lookup)."""
    if not _ok_meeting_id(meeting_id) or not store.exists():
        return {}
    conn = _connect_for_query()
    try:
        meeting = store.get_meeting(conn, meeting_id)
    except sqlite3.Error as exc:
        print(f"[web] meeting info lookup failed for {meeting_id}: {exc}", flush=True)
        return {}
    finally:
        conn.close()
    if meeting is None:
        return {}
    date, committee, _title = _meeting_title(meeting, meeting_id)
    return {key: value for key, value in (("date", date), ("committee", committee)) if value}


def _enrich_citations(citations: list[dict], footnote_by_id: dict[str, dict]) -> list[dict]:
    """Enrich citation quotes:
    - Empty/null quote → inject provenance fields as a special _no_results marker
    - meeting_id present in quote → resolve to DD/MM/YYYY date string
    """
    _PROV_QUERY_KEYS = ("query", "topic", "mk_query", "speaker", "committee")

    enriched = []
    for cit in citations:
        ev_id = cit.get("ev_id", "")
        fn = footnote_by_id.get(ev_id, {})
        ui = fn.get("ui") or {}
        enrich_fields = ui.get("enrich_fields", [])

        quote = cit.get("quote")
        if isinstance(quote, str):
            try:
                quote = json.loads(quote)
            except Exception as exc:
                print(f"[web] citation quote of {ev_id!r} is not JSON, kept as text: {exc}", flush=True)

        # Empty quote → substitute provenance so UI can show what was queried
        if quote is None or quote == "" or quote == [] or quote == {}:
            prov = fn.get("provenance") or {}
            useful = {k: v for k, v in prov.items() if k in _PROV_QUERY_KEYS and v}
            if useful:
                cit = {**cit, "quote": {"_no_results": True, **useful}}
            enriched.append(cit)
            continue

        # meeting_id enrichment (date + committee)
        if "meeting_id" not in enrich_fields:
            enriched.append(cit)
            continue

        def _apply_meeting_info(obj: dict) -> dict:
            mid = obj.get("meeting_id")
            if mid is None:
                return obj
            info = _get_meeting_info(str(mid))
            patch: dict = {}
            if info.get("date") and "date" not in obj:
                patch["date"] = info["date"]
            if info.get("committee"):
                patch["committee"] = info["committee"]
            return {**obj, **patch} if patch else obj

        if isinstance(quote, dict):
            quote = _apply_meeting_info(quote)
            cit = {**cit, "quote": quote}
        elif isinstance(quote, list):
            cit = {**cit, "quote": [
                _apply_meeting_info(item) if isinstance(item, dict) else item
                for item in quote
            ]}
        enriched.append(cit)
    return enriched


def _cache_tool_result(ref_id: str, full: str) -> None:
    with _TOOL_RESULT_LOCK:
        if len(_TOOL_RESULT_CACHE) >= _TOOL_RESULT_CAP:
            for k in list(_TOOL_RESULT_CACHE.keys())[:500]:
                del _TOOL_RESULT_CACHE[k]
        _TOOL_RESULT_CACHE[ref_id] = full


def _strip_footnote_fulls(ev_data: dict, registry=None) -> dict:
    """Strip `full` from footnotes in node_result subgraph outputs; cache each one.
    Also enriches footnotes with tool ui metadata and resolves meeting dates in citations.

    Only mutates node_result events that carry subgraph outputs with footnotes.
    All other events pass through unchanged.
    """
    subgraph = ev_data.get("subgraph")
    if not subgraph:
        return ev_data
    outputs = subgraph.get("outputs") or {}
    footnotes = outputs.get("footnotes")
    if not isinstance(footnotes, list):
        return ev_data

    # Build tool_name → ui lookup from RESEARCH_TOOL_REGISTRY (list[ToolSpec])
    ui_map: dict[str, dict] = {spec.name: spec.ui or {} for spec in RESEARCH_TOOL_REGISTRY}

    patched = []
    footnote_by_id: dict[str, dict] = {}
    for fn in footnotes:
        full = fn.get("full") or ""
        tool_name = fn.get("tool_name", "")
        ui = ui_map.get(tool_name, {})
        base = {**fn, "ui": ui}
        if full:
            ref_id = uuid.uuid4().hex[:16]
            _cache_tool_result(ref_id, full)
            enriched_fn = {**base, "full": "", "result_ref": ref_id}
        else:
            enriched_fn = {**base, "result_ref": None}
        patched.append(enriched_fn)
        footnote_by_id[fn.get("id", "")] = enriched_fn

    citations = outputs.get("citations")
    enriched_citations = (
        _enrich_citations(citations, footnote_by_id)
        if isinstance(citations, list) else citations
    )

    return {
        **ev_data,
        "subgraph": {
            **subgraph,
            "outputs": {
                **outputs,
                "footnotes": patched,
                "citations": enriched_citations,
            },
        },
    }


def _strip_tool_result_fulls(ev_data: dict) -> dict:
    """Strip full text from step_completed tool_call_results; store in cache.

    Only mutates hook/step_completed payloads.  All other events pass through
    unchanged.  Returns a shallow copy of ev_data so the original is untouched.
    """
    if ev_data.get("kind") != "hook" or ev_data.get("name") != "step_completed":
        return ev_data
    payload = ev_data.get("payload") or {}
    results = payload.get("tool_call_results")
    if not results:
        return ev_data
    patched_results = []
    for tr in results:
        full = tr.get("full") or ""
        ref_id = uuid.uuid4().hex[:16]
        _cache_tool_result(ref_id, full)
        patched_results.append({**tr, "full": "", "result_ref": ref_id})
    return {**ev_data, "payload": {**payload, "tool_call_results": patched_results}}
from agent.machine import StateMachine
from agent.runner import MachineRunner, build_tool_registry
from agent.tools import call_for_machine_runner
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from retrieval import knesset_db_store as store
from retrieval.lemmatize import lemmatize
from utils.meeting import get_transcript_path_from_id
from utils.speech import get_mk_speeches_in_committee
from utils.tools import _connect_for_query, _expand_match, _fts_exact_match, _quote_match


# ── Summary helpers (knesset.db) ──────────────────────────────────────────────

_SECTION_BY_NUM = {1: "topics", 2: "opinions", 3: "attendance"}


def _load_meeting_summary(meeting_id: str) -> dict | None:
    """{meeting, topics, opinions, attendance} from knesset.db, or None when the meeting has no summary."""
    if not store.exists():
        print(f"[web] {store.db_path()} not built; run scripts/build_knesset_db.py", flush=True)
        return None
    conn = _connect_for_query()
    try:
        meeting = store.get_meeting(conn, meeting_id)
        if meeting is None or meeting.get("is_protocol") is None:
            return None
        return {
            "meeting":    meeting,
            "topics":     [t["text"] for t in store.get_topics(conn, meeting_id)],
            "opinions":   store.get_opinions(conn, meeting_id),
            "attendance": store.get_attendance(conn, meeting_id),
        }
    except Exception as exc:
        print(f"[web] summary lookup failed for {meeting_id}: {exc}", flush=True)
        return None
    finally:
        conn.close()


def _summary_sections(data: dict) -> list[dict]:
    """Sections for the reading tab: attendance, topics, opinions grouped by speaker."""
    attendance = [{"text": f"{a['name']} ({a['party']})" if a.get("party") else a["name"],
                   "mk_id": a.get("mk_id")} for a in data["attendance"]]
    topics = [{"text": t} for t in data["topics"]]
    by_speaker: dict[str, list[dict]] = {}
    for o in data["opinions"]:
        by_speaker.setdefault(o.get("speaker_name") or o["speaker"], []).append(o)
    opinions = []
    for speaker, items in by_speaker.items():
        for o in items:
            opinions.append({
                "text":           f"**{speaker}**: {o['opinion']}",
                "quote":          o.get("quote") or "",
                "quote_verified": bool(o.get("quote_verified")),
                "speech_idx":     o.get("speech_idx"),
                "quote_offset":   o.get("quote_offset"),
                "quote_length":   o.get("quote_length"),
                "mk_id":          o.get("mk_id"),
            })
    return [
        {"index": 0, "heading": "נוכחים", "bullets": attendance},
        {"index": 1, "heading": "נושאים", "bullets": topics},
        {"index": 2, "heading": "עמדות",  "bullets": opinions},
    ]


def _meeting_title(meeting: dict, meeting_id: str) -> tuple[str, str, str]:
    """(date dd/mm/yyyy, committee, title)."""
    iso = meeting.get("date") or ""
    date = f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if len(iso) == 10 else ""
    committee = meeting.get("committee") or ""
    return date, committee, (f"{committee} — {date}" if committee and date else meeting_id)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    import web.settings as settings

    setup_file_logging()
    forget_server_gemini_keys()
    use_public_api_http_settings()
    print("[web] Loading machine …", flush=True)
    machine = StateMachine(settings.MACHINE_PATH)
    print(f"[web]   Machine: '{machine.name}' (v{machine.version})", flush=True)

    # ── Summary tools (look up paths via global meeting registry) ────────────

    def _summary_executor(name: str, args: dict) -> str:
        from summarization.summary_io import render_summary_text
        meeting_id = str(args.get("meeting_id", "")).strip()
        data = _load_meeting_summary(meeting_id)
        if data is None:
            return f"לא נמצא סיכום לישיבה '{meeting_id}'."
        section = None
        if name == "get_meeting_summary_section":
            section = _SECTION_BY_NUM.get(int(args.get("section_num", 1)))
            if section is None:
                return f"נושא {args.get('section_num')} לא קיים. נושאים קיימים: 1 נושאים, 2 עמדות, 3 נוכחים"
        elif name != "get_meeting_summary":
            return f"כלי לא מוכר: {name}"
        text = render_summary_text(data["topics"], data["opinions"], data["attendance"], section=section)
        return text[:6000] + ("\n…[קוצר]" if len(text) > 6000 else "")

    # ── Speech tool ───────────────────────────────────────────────────────────
    transcriptions_root = settings.TRANSCRIPTIONS_ROOT

    def _speech_executor(name: str, args: dict) -> str:
        if name == "get_mk_speeches_in_committee":
            return get_mk_speeches_in_committee(
                mk_name             = str(args.get("mk_name", "")).strip(),
                committee           = str(args.get("committee", "")).strip(),
                transcriptions_root = transcriptions_root,
                max_meetings        = min(int(args.get("max_meetings", 20)), 50),
                knesset_num         = int(args.get("knesset_num", 25)),
            )
        return f"כלי לא מוכר: {name}"

    # ── Build tool registry (raises on unknown function_name) ────────────────
    tool_registry = build_tool_registry(
        machine,
        summary_executor=_summary_executor,
        speech_executor=_speech_executor,
        knesset_dispatch=lambda name, args: call_for_machine_runner(RESEARCH_TOOL_REGISTRY, name, args),
    )

    # ── Sessions dir ─────────────────────────────────────────────────────────
    from web.session import cleanup_stale_sessions
    settings.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale_sessions(settings.SESSIONS_DIR, max_age_hours=config.WEB_SESSION_MAX_AGE_HOURS)

    # ── Store all state on app ────────────────────────────────────────────────
    app.state.machine       = machine
    app.state.tool_registry = tool_registry
    app.state.settings      = settings
    app.state.sessions_dir  = settings.SESSIONS_DIR

    print(f"[web] Ready — {settings.MACHINE_PATH.name}", flush=True)
    async with running_mcp_sessions(app):
        yield

    # Cleanup (none needed for local app)


# ── App ───────────────────────────────────────────────────────────────────────

WEB_API_TITLE = "KnessetLM (מעורב ירושלמי)"

app = FastAPI(title=WEB_API_TITLE, lifespan=lifespan, docs_url=None, redoc_url=None)

_STATIC_DIR    = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

rate_limiter = SlidingWindowRateLimiter()
app.add_middleware(RateLimitMiddleware, limiter=rate_limiter)
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(McpSubdomainMiddleware)
app.add_middleware(RequestLogMiddleware)
class RevalidatedStaticFiles(StaticFiles):
    """Static files the browser revalidates (ETag) on every load, so new JS/CSS reaches open browsers."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatedStaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(api_router)
app.include_router(profiles_router)
app.include_router(game_router)
install_mcp_endpoint(app, rate_limiter)
install_public_docs(app, WEB_API_TITLE)
install_error_handlers(app)
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _static_asset_url(path: str) -> str:
    """Versioned by mtime, so a deploy never pairs new HTML with JS/CSS a browser or proxy kept from before."""
    return f"/static/{path}?v={int((_STATIC_DIR / path).stat().st_mtime)}"


templates.env.globals["asset"] = _static_asset_url

_MK_PHOTO_EXTS = (".jpeg", ".jpg", ".png")

# Honorific / role prefixes stripped before photo lookup. Mirrors browser.js
# _speakerPhotoKey but also covers forms it misses (notably the definite-
# article היו"ר, שר roles, מ"מ). Speaker strings arrive as e.g. 'ח"כ אבי דיכטר',
# 'היו"ר עמית הלוי', 'השר יריב לוין'.
_HONORIFIC_RE = re.compile(
    r'^(ח"כ|ח\'כ|היו"ר|יו"ר|מ"מ\s+היו"ר|מ"מ|סגן\s+השר|סגנית\s+השרה|השרה|השר|שרה|שר|מנכ"ל|ד"ר|פרופ\'?)\s+'
)

# Lazy singleton fuzzy index over the knesset.db mks (label = canonical MK name, which matches the
# photo filenames). Loading scans the whole table, so it is built once per process.
_mk_fuzzy_index = None
_mk_fuzzy_loaded = False
_mk_fuzzy_lock = threading.Lock()

_mk_photo_cache: "OrderedDict[str, Path | None]" = OrderedDict()
_mk_photo_listing: tuple[Path | None, dict[str, Path]] = (None, {})
_mk_photo_lock = threading.Lock()


def _get_mk_fuzzy_index():
    global _mk_fuzzy_index, _mk_fuzzy_loaded
    with _mk_fuzzy_lock:
        if not _mk_fuzzy_loaded:
            _mk_fuzzy_index = _load_mk_fuzzy_index(25)
            _mk_fuzzy_loaded = True
        return _mk_fuzzy_index


def _load_mk_fuzzy_index(knesset_num: int):
    from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex
    if not store.exists():
        print(f"[web] {store.db_path()} not found; MK name resolution unavailable", flush=True)
        return None
    try:
        conn = store.connect()
        try:
            return FuzzyNameIndex(store.name_entries(conn, "mks", knesset_num))
        finally:
            conn.close()
    except Exception as exc:
        print(f"[web] failed to load mks fuzzy index: {exc}", flush=True)
        return None


def _mk_photo_paths_by_stem() -> dict[str, Path]:
    """Allowlist of servable photos: file stem → path, listed from config.MK_PHOTOS_DIR (never built
    from request text). Re-listed, and the resolution cache dropped, when the directory setting changes."""
    global _mk_photo_listing
    photos_dir = Path(config.MK_PHOTOS_DIR)
    with _mk_photo_lock:
        listed_dir, paths_by_stem = _mk_photo_listing
        if listed_dir == photos_dir:
            return paths_by_stem
        paths_by_stem = {}
        try:
            for ext in reversed(_MK_PHOTO_EXTS):
                for path in photos_dir.glob(f"*{ext}"):
                    if path.is_file():
                        paths_by_stem[path.stem] = path
        except OSError as exc:
            print(f"[mk_photo] cannot list {photos_dir}: {exc}", flush=True)
        _mk_photo_listing = (photos_dir, paths_by_stem)
        _mk_photo_cache.clear()
        return paths_by_stem


def _photo_file_for(stem: str) -> "Path | None":
    return _mk_photo_paths_by_stem().get(stem.strip())


def _remember_photo(name: str, result: "Path | None") -> None:
    with _mk_photo_lock:
        _mk_photo_cache[name] = result
        while len(_mk_photo_cache) > config.WEB_MK_PHOTO_CACHE_MAX_ENTRIES:
            _mk_photo_cache.popitem(last=False)


def _resolve_mk_photo(name: str) -> "Path | None":
    """Resolve a (possibly honorific-prefixed / variant) speaker name to a
    photo file: exact match → prefix-stripped exact match → fuzzy resolve to
    a canonical MK name. Returns None for non-MKs (guests, section headers)."""
    _mk_photo_paths_by_stem()
    with _mk_photo_lock:
        if name in _mk_photo_cache:
            return _mk_photo_cache[name]

    cleaned = _HONORIFIC_RE.sub("", name.strip()).strip()
    result = _photo_file_for(name) or _photo_file_for(cleaned)

    if result is None and cleaned:
        idx = _get_mk_fuzzy_index()
        if idx is not None:
            try:
                matches = idx.search(
                    cleaned, top_k=1, threshold=config.PARTICIPANT_FUZZY_THRESHOLD
                )
            except Exception as exc:
                print(f"[mk_photo] fuzzy resolve failed for {name!r}: {exc}")
                matches = []
            if matches:
                result = _photo_file_for(matches[0]["label"])

    _remember_photo(name, result)
    return result


@app.get("/mk-photo/{name}")
def mk_photo(name: str):
    """An MK's portrait by name (fuzzy-matched against the MK roster); 404 when none."""
    if len(name) > config.WEB_MAX_MK_PHOTO_NAME_CHARS:
        return JSONResponse({}, status_code=404)
    p = _resolve_mk_photo(name)
    if p is not None:
        return FileResponse(str(p), media_type=f"image/{p.suffix.lstrip('.').replace('jpg', 'jpeg')}",
                            headers={"Cache-Control": "public, max-age=86400"})
    return JSONResponse({}, status_code=404)


# ── Models ────────────────────────────────────────────────────────────────────

ModelId = Annotated[str, Field(pattern=MODEL_ID_PATTERN.pattern)]


class ResearchModelsChoice(BaseModel):
    """The visitor's model for each part of the run (no server defaults)."""
    intent: ModelId
    planner: ModelId
    critic: ModelId
    executor: ModelId
    synthesizer: ModelId
    answer_editor: ModelId

    def research_models(self) -> ResearchModels:
        return ResearchModels(**self.model_dump())


class ResearchStartRequest(BaseModel):
    question: str = Field(max_length=_MAX_QUESTION)
    models: ResearchModelsChoice


class ResearchRespondRequest(BaseModel):
    output_var: str = Field(max_length=config.WEB_MAX_OUTPUT_VAR_CHARS)
    value: Any


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get(config.CHAT_PAGE_PATH, response_class=HTMLResponse, include_in_schema=False)
@app.get(config.RESEARCH_PAGE_PATH, response_class=HTMLResponse, include_in_schema=False)
@app.get(config.PROTOCOLS_PAGE_PATH, response_class=HTMLResponse, include_in_schema=False)
@app.get(config.GAME_PAGE_PATH, response_class=HTMLResponse, include_in_schema=False)
@app.get(PROFILE_PAGE_PATHS[0], response_class=HTMLResponse, include_in_schema=False)
@app.get(PROFILE_PAGE_PATHS[1], response_class=HTMLResponse, include_in_schema=False)
@app.get(PROFILE_PAGE_PATHS[2], response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    """One page for every tab; the path (and the reading tab's query parameters) pick the starting view."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(str(_STATIC_DIR / "favicon.ico"), media_type="image/x-icon")


@app.get("/api/help", response_class=PlainTextResponse)
async def help_content():
    """The site's help page (Hebrew markdown)."""
    path = Path(__file__).parent / "templates" / "user-help.md"
    return path.read_text(encoding="utf-8")


SITE_DATA_PROMPTS = [
    {"title": "נושאי הישיבה", "used_for": "רשימת הנושאים של כל ישיבה בדיוני הכנסת. מקבל את הפרוטוקול המלא.",
     "text": summary_prompts.SYSTEM_PROMPT_TOPICS},
    {"title": "עמדות וציטוטים", "used_for": "העמדות והציטוטים של כל דובר בישיבה, בדיוני הכנסת ובפרופילים. מקבל את הפרוטוקול המלא, "
                                         "וכל ציטוט נבדק מול הפרוטוקול אחרי שהמודל מחזיר את התשובה שלו.",
     "text": summary_prompts.SYSTEM_PROMPT_OPINIONS},
    {"title": "נושאים מרכזיים של ח\"כ", "used_for": "הנושאים המרכזיים בפרופיל של כל ח\"כ. מקבל את כל העמדות שחולצו לאותו ח\"כ.",
     "text": summary_prompts.SYSTEM_PROMPT_MK_THEMES},
]


@app.get("/api/help/prompts")
async def help_prompts():
    """The system prompts that built the summaries and themes shown on the site."""
    return SITE_DATA_PROMPTS


@app.get("/api/health")
def health(request: Request):
    """Web server health: loaded state machine and protocol database row counts."""
    db_row_counts: dict[str, int] = {}
    if store.exists():
        try:
            conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
            try:
                db_row_counts = store.table_row_counts(conn)
            finally:
                conn.close()
        except Exception as exc:
            print(f"[web] health: row count failed for {store.db_path()}: {exc}", flush=True)
    else:
        print(f"[web] health: {store.db_path()} missing", flush=True)
    return {
        "status":  "ok",
        "machine": request.app.state.machine.name,
        "db":      db_row_counts,
    }


# ── Research session routes ───────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sse_response(events) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _next_worker_item(queue: asyncio.Queue):
    """Next item a worker thread emitted, or _KEEP_ALIVE after WEB_SSE_KEEPALIVE_SECONDS of silence."""
    try:
        return await asyncio.wait_for(queue.get(), timeout=config.WEB_SSE_KEEPALIVE_SECONDS)
    except asyncio.TimeoutError:
        return _KEEP_ALIVE


def _thread_safe_emitter(queue: asyncio.Queue) -> Callable[[object], None]:
    loop = asyncio.get_running_loop()
    return lambda item: loop.call_soon_threadsafe(queue.put_nowait, item)


# ── Research runs ─────────────────────────────────────────────────────────────

@dataclass
class ResearchRun:
    """One MachineRunner run executed by run_research in a worker thread; the SSE generator sets
    stop_requested when its client goes away."""
    session_id: str
    question: str
    created_at: str
    sessions_dir: Path
    machine: Any
    tool_registry: Any
    gemini_api_key: str | None
    models: ResearchModels
    resume: dict | None
    user_response: dict | None
    workspace_data: dict | None
    install_llm_event_sink: bool
    log_prefix: str
    emit: Callable[[object], None]
    stop_requested: threading.Event
    slots: ResearchRunSlots
    request_id: str | None = None
    request_state: dict = field(default_factory=dict)


def run_research(run: ResearchRun) -> None:
    """Wait for a research slot, run the machine and relay its events through run.emit. The session
    file is saved before the event that ends the run is emitted, so a client that reacts to
    user_input_required / done finds it on disk; a run stopped without an outcome is saved as an error."""
    from web.session import ResearchSession, save_session

    final_state_saved = False

    def save_final_state(status: str, **fields) -> None:
        nonlocal final_state_saved
        save_session(ResearchSession(
            session_id=run.session_id, status=status, original_question=run.question,
            created_at=run.created_at, updated_at=_now_iso(), models=run.models.to_dict(), **fields,
        ), run.sessions_dir)
        final_state_saved = True

    if run.install_llm_event_sink:
        from agent.subgraph.llm_bridge import set_thread_event_sink
        set_thread_event_sink(
            lambda ev: run.emit(("subgraph_event", {"kind": ev.kind, "name": ev.name, "payload": ev.payload}))
        )

    if not run.slots.acquire(run.stop_requested, on_queued=lambda: run.emit(("queued", {}))):
        message = STOPPED_MESSAGE if run.stop_requested.is_set() else BUSY_MESSAGE
        print(f"[{run.log_prefix}] session {run.session_id} got no research slot: {message}", flush=True)
        try:
            save_final_state("error", error=message)
        except Exception as exc:
            print(f"[{run.log_prefix}] saving the no-slot session failed: {exc}", flush=True)
        run.emit(("error", message))
        run.emit(_SENTINEL)
        return

    runner_events = None
    key_guarded_events = None
    final_token = ""
    event_log: list[dict] = []  # selective event log for reconnect replay
    try:
        intent_backend = research_llm_backend(run.gemini_api_key, run.models.intent)
        runner = MachineRunner(
            machine        = run.machine,
            backend        = intent_backend,
            tool_registry  = run.tool_registry,
            gemini_api_key = run.gemini_api_key,
            models         = run.models,
            backend_by_model_role = {
                "intent":        intent_backend,
                "answer_editor": research_llm_backend(run.gemini_api_key, run.models.answer_editor),
            },
        )
        runner_events = runner.run_stream(question=run.question, resume=run.resume, user_response=run.user_response)
        key_guarded_events = stop_on_rejected_gemini_key(
            runner_events, on_key_rejected=lambda: run.request_state.update(gemini_key_outcome="rejected_mid_run"))
        for event in key_guarded_events:
            if run.stop_requested.is_set():
                print(f"[{run.log_prefix}] client of session {run.session_id} is gone; stopping the run", flush=True)
                break
            _t, _d = event
            if _t == "token":
                final_token += _d
            elif _t == "done":
                save_final_state("done", final_answer=final_token, event_log=list(event_log) or None)
            elif _t == "error":
                save_final_state("error", error=str(_d))
            elif _t == "user_input_required":
                save_final_state("awaiting_user", machine_checkpoint=_d.get("checkpoint", {}),
                                 workspace_data=run.workspace_data)
            elif _t == "node_start":
                if isinstance(_d, dict) and _d.get("subgraph"):
                    event_log.append({"type": "node_start", "data": _d})
            elif _t == "node_result":
                _d = _strip_footnote_fulls(_d, registry=run.tool_registry)
                event = (_t, _d)
                if isinstance(_d, dict) and _d.get("subgraph"):
                    event_log.append({"type": "node_result", "data": _d})
            elif _t == "subgraph_event":
                _stripped = _strip_tool_result_fulls(_d)
                _d = {
                    "type":    "subgraph_event",
                    "kind":    _stripped.get("kind"),
                    "name":    _stripped.get("name"),
                    "payload": _stripped.get("payload", {}),
                }
                event = (_t, _d)
                _k, _n = _d["kind"], _d["name"]
                if _k == "done" or (_k == "hook" and _n in ("step_completed", "synthesizer_completed")):
                    event_log.append({"type": "subgraph_event", "data": _d})
            run.emit(event)
    except Exception as exc:
        print(f"[{run.log_prefix}] run of session {run.session_id} failed: {exc}", flush=True)
        log_server_error(run.request_id, f"{run.log_prefix} run of session {run.session_id} failed",
                         traceback.format_exc())
        client_message = generic_error_message(run.request_id)
        try:
            save_final_state("error", error=client_message)
        except Exception as save_exc:
            print(f"[{run.log_prefix}] saving the failed session failed: {save_exc}", flush=True)
        run.emit(("error", client_message))
    finally:
        for events in (key_guarded_events, runner_events):
            if events is None:
                continue
            try:
                events.close()
            except Exception as exc:
                print(f"[{run.log_prefix}] closing the run's event stream failed: {exc}", flush=True)
        run.slots.release()
        if not final_state_saved:
            try:
                save_final_state("error", error=STOPPED_MESSAGE)
            except Exception as exc:
                print(f"[{run.log_prefix}] saving the stopped session failed: {exc}", flush=True)
        run.emit(_SENTINEL)


def _start_research_thread(request: Request, queue: asyncio.Queue, **run_fields) -> ResearchRun:
    run = ResearchRun(
        sessions_dir   = request.app.state.sessions_dir,
        machine        = request.app.state.machine,
        tool_registry  = request.app.state.tool_registry,
        emit           = _thread_safe_emitter(queue),
        stop_requested = threading.Event(),
        slots          = _RESEARCH_SLOTS,
        request_id     = request_id_of(request),
        request_state  = request.scope.setdefault("state", {}),
        **run_fields,
    )
    threading.Thread(target=run_research, args=(run,), daemon=True).start()
    return run


async def _research_sse_events(run: ResearchRun, queue: asyncio.Queue, announce_session_id: bool):
    try:
        if announce_session_id:
            yield _sse("session_id", {"session_id": run.session_id})
        while True:
            item = await _next_worker_item(queue)
            if item is _KEEP_ALIVE:
                yield ": keep-alive\n\n"
                continue
            if item is _SENTINEL:
                break
            ev_type, ev_data = item

            if ev_type == "token":
                yield _sse("token", {"text": ev_data})

            elif ev_type == "status":
                yield _sse("status", {"msg": ev_data})

            elif ev_type in ("node_start", "node_result", "subgraph_event"):
                yield _sse(ev_type, ev_data)

            elif ev_type == "thinking_token":
                yield _sse("thinking_token", {"text": ev_data})

            elif ev_type in ("queued", "gemini_key_invalid"):
                yield _sse(ev_type, {})

            elif ev_type == "user_input_required":
                ui_event = {k: v for k, v in ev_data.items() if k != "checkpoint"}
                ui_event["session_id"] = run.session_id
                yield _sse("user_input_required", ui_event)
                yield _sse("user_paused", {"session_id": run.session_id})
                return

            elif ev_type == "done":
                yield _sse("done", {})
                return

            elif ev_type == "error":
                yield _sse("error", {"error": ev_data, "request_id": run.request_id})
                return

    except Exception as exc:
        print(f"[research_sse] stream of session {run.session_id} failed: {exc}", flush=True)
        log_server_error(run.request_id, f"research SSE stream of session {run.session_id} failed",
                         traceback.format_exc())
        yield _sse("error", {"error": generic_error_message(run.request_id), "request_id": run.request_id})
    finally:
        run.stop_requested.set()


def _log_visitor_question(request: Request, session_id: str | None, question: Any) -> None:
    try:
        log_question(request, request.url.path, session_id, question)
    except Exception as exc:
        print(f"[web] logging the question of session {session_id} failed: {exc}", flush=True)


def _save_new_running_session(session_id: str, question: str, created_at: str, models: ResearchModels,
                              sessions_dir: Path) -> None:
    from web.session import ResearchSession, maybe_cleanup_stale_sessions, save_session
    maybe_cleanup_stale_sessions(sessions_dir)
    save_session(ResearchSession(
        session_id=session_id, status="running",
        original_question=question, created_at=created_at, updated_at=created_at, models=models.to_dict(),
    ), sessions_dir)


@app.get("/api/gemini/models")
async def gemini_models(request: Request):
    """The Gemini/Gemma text models the visitor's key can call: [{id, name}] for the settings' model choices."""
    gemini_api_key, gemini_key_error = await asyncio.to_thread(visitor_gemini_key_or_error, request)
    if gemini_key_error is not None:
        return gemini_key_error
    if gemini_api_key is None:
        return {"models": []}
    text_models = await asyncio.to_thread(gemini_text_models, gemini_api_key)
    if text_models is None:
        return JSONResponse({"error": "gemini_models_unverified",
                             "message": "לא ניתן לקבל כרגע את רשימת המודלים מ-Google. נסו שוב בעוד רגע."},
                            status_code=503)
    return {"models": text_models}


@app.post("/api/research/start")
async def research_start(req: ResearchStartRequest, request: Request):
    """
    Start a new research session.

    Streams SSE events.  The FIRST event is always ``session_id``.
    If the machine pauses at a ``user_input`` node, emits ``user_input_required``
    followed by ``user_paused`` and then closes the stream.
    """
    question = _clean_question(req.question)
    if not question:
        return JSONResponse({"error": "שאלה ריקה"}, status_code=400)
    if not _ok_question(question):
        return JSONResponse({"error": "שאלה מכילה תווים לא חוקיים או ארוכה מדי"}, status_code=400)

    gemini_api_key, gemini_key_error = await asyncio.to_thread(visitor_gemini_key_or_error, request)
    if gemini_key_error is not None:
        return gemini_key_error
    models = req.models.research_models()
    models_error = await asyncio.to_thread(unavailable_models_error, gemini_api_key, models.model_ids())
    if models_error is not None:
        return models_error
    if not _RESEARCH_SLOTS.can_admit():
        return _busy_response()

    session_id = str(uuid.uuid4())
    request.state.session_id = session_id
    _log_visitor_question(request, session_id, question)
    created_at = _now_iso()
    await asyncio.to_thread(_save_new_running_session, session_id, question, created_at, models,
                            request.app.state.sessions_dir)
    queue: asyncio.Queue = asyncio.Queue()
    run = _start_research_thread(
        request, queue,
        session_id=session_id, question=question, created_at=created_at, gemini_api_key=gemini_api_key,
        models=models, resume=None, user_response=None, workspace_data=None,
        install_llm_event_sink=True, log_prefix="research_start",
    )
    return _sse_response(_research_sse_events(run, queue, announce_session_id=True))


@app.get("/api/research/{session_id}/stream")
async def research_stream(session_id: str, request: Request):
    """
    Re-attach to an existing session after a browser reconnect.

    Replays the minimal state needed for the client to continue:
    - awaiting_user → re-emits ``user_input_required``
    - done          → re-emits ``token`` (full answer) + ``done``
    - error         → re-emits ``error``
    """
    if not _ok_session_id(session_id):
        return JSONResponse({"error": "Invalid session ID"}, status_code=400)

    from web.session import load_session

    session = await asyncio.to_thread(load_session, session_id, request.app.state.sessions_dir)

    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    async def generate():
        if session.status == "awaiting_user":
            checkpoint  = session.machine_checkpoint or {}
            pending_ui  = checkpoint.get("pending_ui_event", {})
            ui_event    = dict(pending_ui)
            ui_event["session_id"] = session_id
            yield _sse("user_input_required", ui_event)

        elif session.status == "running":
            yield _sse("still_running", {"session_id": session_id})

        elif session.status == "done":
            if session.event_log:
                for item in session.event_log:
                    yield _sse(item["type"], item["data"])
            yield _sse("token", {"text": session.final_answer or ""})
            yield _sse("done", {})

        elif session.status == "error":
            yield _sse("error", {"error": session.error or "Unknown error"})

    return _sse_response(generate())


def _same_json_value(a: Any, b: Any) -> bool:
    """Equality that keeps JSON types apart (in Python True == 1)."""
    return type(a) is type(b) and a == b


def _is_offered_option(pending_ui_event: dict, value: Any) -> bool:
    offered = [opt.get("value") if isinstance(opt, dict) else opt for opt in pending_ui_event.get("options") or []]

    def is_offered(candidate: Any) -> bool:
        return any(_same_json_value(candidate, option) for option in offered)

    if pending_ui_event.get("multi_select"):
        return isinstance(value, list) and 0 < len(value) <= len(offered) and all(is_offered(v) for v in value)
    return is_offered(value)


def _validated_user_response(pending_ui_event: Any, output_var: str, value: Any) -> tuple[dict | None, str | None]:
    """({"output_var", "value"}, None) for an answer that fits the input the session paused at, else (None, reason).
    output_var must be the paused node's; value must fit its ui type: an offered option, meeting ids, or a question."""
    if not isinstance(pending_ui_event, dict):
        return None, "the session is not waiting for a known input"
    if output_var != pending_ui_event.get("output_var"):
        return None, "output_var does not match the input the session is waiting for"
    ui_type = pending_ui_event.get("ui", "text_input")
    if ui_type == "option_select":
        value_is_valid = _is_offered_option(pending_ui_event, value)
    elif ui_type == "deep_dive":
        value_is_valid = (isinstance(value, list) and len(value) <= config.WEB_MAX_DEEP_DIVE_MEETINGS
                          and all(isinstance(v, str) and _ok_meeting_id(v) for v in value))
    elif ui_type == "text_input":
        value = _clean_question(value) if isinstance(value, str) else value
        value_is_valid = isinstance(value, str) and _ok_question(value)
    else:
        return None, f"unsupported input type: {str(ui_type)[:40]}"
    if not value_is_valid:
        return None, f"value is not valid for this {ui_type} input"
    return {"output_var": output_var, "value": value}, None


@app.post("/api/research/{session_id}/respond")
async def research_respond(
    session_id: str,
    req: ResearchRespondRequest,
    request: Request,
):
    """
    Provide a user response to a paused session and resume execution.

    Streams SSE events exactly like ``/api/research/start``.
    """
    if not _ok_session_id(session_id):
        return JSONResponse({"error": "Invalid session ID"}, status_code=400)
    gemini_api_key, gemini_key_error = await asyncio.to_thread(visitor_gemini_key_or_error, request)
    if gemini_key_error is not None:
        return gemini_key_error

    from web.session import load_session, mark_running_if_awaiting_user

    sessions_dir = request.app.state.sessions_dir
    session = await asyncio.to_thread(load_session, session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if session.status != "awaiting_user":
        return JSONResponse(
            {"error": f"Session is not awaiting user input (status: {session.status})"},
            status_code=409,
        )
    pending_ui_event = (session.machine_checkpoint or {}).get("pending_ui_event")
    user_response, invalid_reason = _validated_user_response(pending_ui_event, req.output_var, req.value)
    if invalid_reason is not None:
        return JSONResponse({"error": invalid_reason}, status_code=400)
    if session.models is None:
        return JSONResponse({"error": "המחקר הזה התחיל בלי בחירת מודלים. אפשר להתחיל מחקר חדש."}, status_code=409)
    models = ResearchModels.from_dict(session.models)
    models_error = await asyncio.to_thread(unavailable_models_error, gemini_api_key, models.model_ids())
    if models_error is not None:
        return models_error
    if isinstance(user_response["value"], str):
        _log_visitor_question(request, session_id, user_response["value"])
    if not _RESEARCH_SLOTS.can_admit():
        return _busy_response()

    claimed = await asyncio.to_thread(mark_running_if_awaiting_user, session_id, sessions_dir)
    if claimed is None:
        return JSONResponse({"error": "Session is not awaiting user input (already resumed)"}, status_code=409)

    queue: asyncio.Queue = asyncio.Queue()
    run = _start_research_thread(
        request, queue,
        session_id=session_id, question=claimed.original_question, created_at=claimed.created_at,
        gemini_api_key=gemini_api_key, models=models, resume=claimed.machine_checkpoint or {},
        user_response=user_response,
        workspace_data=claimed.workspace_data, install_llm_event_sink=False, log_prefix="research_respond",
    )
    return _sse_response(_research_sse_events(run, queue, announce_session_id=False))


@app.get("/api/research/{session_id}/tool_result/{ref_id}")
def get_tool_result(session_id: str, ref_id: str):
    """Return the full text for a lazily-loaded tool result panel."""
    if not _ok_session_id(session_id) or not _ok_ref_id(ref_id):
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)
    full = _TOOL_RESULT_CACHE.get(ref_id)
    if full is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"full": full})


# ── Browse (reading tab) ──────────────────────────────────────────────────────

BROWSE_SORTS = ("relevance", "date")
DB_UNAVAILABLE_MESSAGE = "מסד הנתונים אינו זמין כרגע"
QUERY_TIMEOUT_MESSAGE = "החיפוש ארך זמן רב מדי: נסו מילות מפתח ממוקדות יותר או הוסיפו סינון (ועדה, תאריכים, חברי כנסת)"
META_UNAVAILABLE_MESSAGE = "רשימות הסינון אינן זמינות כרגע. נסו שוב בעוד רגע."


class BrowseFilterRequest(BaseModel):
    committees: list[str] = []
    mks:        list[str] = []
    parties:    list[str] = []
    guest:      str | None = None
    date_from:  str | None = None
    date_to:    str | None = None
    meeting_ids: list[str] = []


class BrowseSearchRequest(BaseModel):
    query:   str = ""
    top_k:   int | None = None
    sort:    str = "relevance"
    filters: BrowseFilterRequest | None = None


class _QueryTimedOut(Exception):
    pass


def _validated_browse_filters(filters: BrowseFilterRequest | None) -> BrowseFilterRequest:
    """The /v1 caps on list sizes, name lengths and date formats; raises ApiInputError (→ 400)."""
    filters = filters or BrowseFilterRequest()

    def names(values: list[str], field_name: str) -> list[str]:
        cleaned = (valid.name_filter(v, field_name) for v in valid.list_param(values, field_name))
        return [name for name in cleaned if name]

    return BrowseFilterRequest(
        committees=names(filters.committees, "committees"),
        mks=names(filters.mks, "mks"),
        parties=names(filters.parties, "parties"),
        guest=valid.name_filter(filters.guest, "guest"),
        date_from=valid.iso_date(filters.date_from, "date_from"),
        date_to=valid.iso_date(filters.date_to, "date_to"),
        meeting_ids=[valid.meeting_id(m, "meeting_ids") for m in valid.list_param(filters.meeting_ids, "meeting_ids")
                     if m],
    )


_meta_cache: dict[str, Any] = {}
_meta_lock = threading.Lock()


def forget_meta_cache() -> None:
    with _meta_lock:
        _meta_cache.clear()


def _meta_payload() -> dict:
    """Committees, MKs and parties for the filter dropdowns, cached per process for WEB_META_CACHE_SECONDS
    (only successful answers are cached)."""
    from utils import knesset_db

    with _meta_lock:
        if _meta_cache and _meta_cache["expires_monotonic"] > time.monotonic():
            return _meta_cache["payload"]
        committees = knesset_db.get_all_committees(25)
        mks = knesset_db.get_all_mks(25)
        parties = knesset_db.get_all_parties(25)

        def _mk_name(m: dict) -> str:
            first = (m.get("mk_individual_first_name") or "").strip()
            last  = (m.get("mk_individual_name")       or "").strip()
            return f"{first} {last}".strip() or last or first

        payload = {
            "committees": [c["Name"] for c in committees],
            "mks":        sorted({_mk_name(m) for m in mks if _mk_name(m)}),
            "parties":    [p["party"] for p in parties],
        }
        _meta_cache.update(payload=payload, expires_monotonic=time.monotonic() + config.WEB_META_CACHE_SECONDS)
        return payload


@app.get("/api/meta")
def get_meta():
    """Return committees, MKs, and parties for filter dropdowns."""
    try:
        return _meta_payload()
    except Exception as exc:
        print(f"[meta] loading filter lists failed: {type(exc).__name__}: {exc}", flush=True)
        return JSONResponse({"error": META_UNAVAILABLE_MESSAGE}, status_code=503)


def _speech_match_expression(query: str) -> str:
    """FTS5 MATCH expression over speeches_fts: lemmatized, ktiv-expanded, tokens AND-ed."""
    normalized = lemmatize(query)
    return _expand_match(normalized, "speeches_fts") or _quote_match(normalized)


_HEATMAP_MIN_WORD_CHARS = 3


def _speech_word_matches(query: str) -> list[str]:
    """One FTS5 MATCH expression per distinct query word (ktiv-expanded), for ranking speeches by
    how many of the words they contain. Words shorter than 3 letters (על, של, את) are dropped
    unless nothing else remains."""
    return [m for m in (_expand_match(w, "speeches_fts") or _quote_match(w) for w in _heatmap_words(query))
            if m.strip()]


def _speech_exact_word_matches(query: str) -> list[str]:
    """_speech_word_matches without spelling / prefix variants: the words as typed."""
    return [m for m in (_quote_match(w) for w in _heatmap_words(query)) if m.strip()]


def _heatmap_words(query: str) -> list[str]:
    words = list(dict.fromkeys(lemmatize(query).split()))
    return [w for w in words if len(w) >= _HEATMAP_MIN_WORD_CHARS] or words


def _resolve_participant_filters(filters: BrowseFilterRequest) -> tuple[list[str], str | None]:
    """
    (mk_ids, guest_name) for the MK-name and guest filters. The guest is a
    free-text name: when it does not resolve to an MK it is matched as a
    substring of attendance.name instead of being dropped.
    """
    names = list(filters.mks or []) + ([filters.guest] if filters.guest else [])
    if not names:
        return [], None
    fuzzy_mk_index = _get_mk_fuzzy_index()
    if fuzzy_mk_index is None:
        print(f"[browse_search] cannot resolve MK/guest names to mk_id; participant filter skipped for {names}",
              flush=True)
        return [], filters.guest
    mk_ids: list[str] = []
    unresolved_names: list[str] = []
    for name in names:
        matches = fuzzy_mk_index.search(name, top_k=1, threshold=config.PARTICIPANT_FUZZY_THRESHOLD)
        if matches:
            mk_ids.append(str(matches[0]["extra"].get("mk_id") or matches[0]["id"]))
        else:
            unresolved_names.append(name)
    guest_name = filters.guest if filters.guest and filters.guest in unresolved_names else None
    unresolved_mk_names = [n for n in unresolved_names if n != guest_name]
    if unresolved_mk_names:
        print(f"[browse_search] could not resolve to a known MK, filter skipped for: {unresolved_mk_names}",
              flush=True)
    return mk_ids, guest_name


def _browse_search_meetings(query: str, sort: str, filters: BrowseFilterRequest, top_k: int,
                            knesset_num: int) -> list[dict]:
    """
    Candidate meetings from the structural filters, then either FTS over their
    speeches (query given; one row per meeting, best speech as excerpt, score
    normalized so the best meeting is 1.0) or the newest candidates (empty query;
    first summary topic as excerpt, score 0). Raises _QueryTimedOut past DB_QUERY_TIMEOUT_SECONDS.
    """
    mk_ids, guest_name = _resolve_participant_filters(filters)
    conn = _connect_for_query()
    try:
        candidate_meeting_ids = store.query_candidate_meeting_ids(
            conn, knesset_num,
            committees=[c.replace("_", " ").strip() for c in filters.committees] or None,
            date_from=filters.date_from or None, date_to=filters.date_to or None,
            mk_ids=mk_ids or None, parties=list(filters.parties) or None, guest_name=guest_name,
        )
        if filters.meeting_ids:
            candidate_meeting_ids = [m for m in filters.meeting_ids
                                     if candidate_meeting_ids is None or m in set(candidate_meeting_ids)]
        if not query:
            rows = store.recent_meetings(conn, knesset_num, limit=top_k,
                                         candidate_meeting_ids=candidate_meeting_ids)
            first_topics = store.first_topic_by_meeting(conn, [r["meeting_id"] for r in rows])
            for r in rows:
                r["excerpt"] = first_topics.get(r["meeting_id"], "")
                r["score"] = 0.0
        else:
            match = _speech_match_expression(query)
            if not match:
                return []
            try:
                rows = store.meetings_by_best_speech(conn, match, knesset_num, limit=top_k, sort=sort,
                                                     candidate_meeting_ids=candidate_meeting_ids,
                                                     exact_match=_fts_exact_match(query))
            except sqlite3.Error as exc:
                if store.deadline_passed(conn):
                    raise
                print(f"[browse_search] FTS query failed for {match!r}: {exc}", flush=True)
                return []
            display_scores = _tiered_display_scores(rows)
            for r, display_score in zip(rows, display_scores):
                r["excerpt"] = store.speech_snippet(conn, match, r["best_speech_rowid"])
                r["score"] = display_score
    except sqlite3.OperationalError as exc:
        if store.deadline_passed(conn):
            print(f"[browse_search] query timed out for {query!r}: {exc}", flush=True)
            raise _QueryTimedOut() from exc
        raise
    finally:
        conn.close()
    meetings_out = []
    for r in rows:
        date, committee, title = _meeting_title(r, r["meeting_id"])
        meetings_out.append({"meeting_id": r["meeting_id"], "date": date, "committee": committee,
                             "title": title, "excerpt": r["excerpt"], "score": r["score"]})
    return meetings_out


def _tiered_display_scores(rows: list[dict]) -> list[float]:
    """bm25 scores normalized to (0, 1], best = 1, per exact-match tier. When both tiers are present,
    tier 0 (the query words as typed) maps to (0.5, 1] and tier 1 (variants only) to (0, 0.5], so a
    client ordering by score keeps exact matches first."""
    tiers = sorted({r.get("tier", 0) for r in rows})
    best_score_by_tier = {tier: min(r["score"] for r in rows if r.get("tier", 0) == tier) for tier in tiers}
    display_scores = []
    for r in rows:
        best_score = best_score_by_tier[r.get("tier", 0)]
        normalized = r["score"] / best_score if best_score < 0 else 1.0
        if len(tiers) > 1:
            normalized = 0.5 * normalized + (0.5 if r.get("tier", 0) == tiers[0] else 0.0)
        display_scores.append(round(normalized, 4))
    return display_scores


@app.post("/api/browse/search")
def browse_search(req: BrowseSearchRequest, request: Request):
    """
    Session-less entry point for the reading tab: keyword search over speeches
    (or newest meetings for an empty query) within the structural filters.

    Creates a fresh ResearchSession so the /api/research/{sid}/meeting/... routes
    work against the returned session_id.
    """
    from web.session import ResearchSession, maybe_cleanup_stale_sessions, save_session

    query = valid.keyword_query(req.query)
    sort = valid.one_of(req.sort, BROWSE_SORTS, "sort")
    filters = _validated_browse_filters(req.filters)
    if not store.exists():
        print(f"[browse_search] {store.db_path()} missing; run scripts/build_knesset_db.py", flush=True)
        return JSONResponse({"error": DB_UNAVAILABLE_MESSAGE}, status_code=503)

    settings = request.app.state.settings
    top_k = max(1, min(req.top_k or settings.TOP_K_BROWSE, _MAX_TOP_K))
    try:
        meetings_out = _browse_search_meetings(query, sort, filters, top_k, knesset_num=25)
    except _QueryTimedOut:
        return JSONResponse({"error": QUERY_TIMEOUT_MESSAGE}, status_code=503)

    sessions_dir = request.app.state.sessions_dir
    maybe_cleanup_stale_sessions(sessions_dir)
    session_id = str(uuid.uuid4())
    now = _now_iso()
    save_session(ResearchSession(
        session_id=session_id, status="done", original_question=query,
        created_at=now, updated_at=now, workspace_data={"selected_chunks": []},
    ), sessions_dir)
    return {"session_id": session_id, "meetings": meetings_out, "query_used": query}


# ── Workspace models ──────────────────────────────────────────────────────────

class WorkspaceSelectRequest(BaseModel):
    chunk_id: str = Field(min_length=1, max_length=config.API_MAX_ID_DIGITS, pattern=r"^[0-9]+$")
    text: str = Field(min_length=1, max_length=config.WEB_MAX_WORKSPACE_CHUNK_CHARS)
    source_meeting_id: str = Field(min_length=1, max_length=config.API_MAX_ID_DIGITS + 1, pattern=r"^p?[0-9]+$")


class WorkspaceAskRequest(BaseModel):
    question: str = Field(max_length=_MAX_QUESTION)
    model: ModelId
    meeting_id: str | None = Field(default=None, max_length=config.API_MAX_ID_DIGITS + 1)


# ── Meeting routes (reading tab) ──────────────────────────────────────────────

def _invalid_meeting_route(session_id: str, meeting_id: str) -> JSONResponse | None:
    if not _ok_session_id(session_id) or not _ok_meeting_id(meeting_id):
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)
    return None


@app.get("/api/research/{session_id}/meeting/{meeting_id}/summary")
def research_meeting_summary(session_id: str, meeting_id: str, request: Request):
    """
    Return a meeting's summary as sections (attendance, topics, opinions) for the reading tab.
    """
    if (invalid := _invalid_meeting_route(session_id, meeting_id)) is not None:
        return invalid

    from web.session import load_session

    if load_session(session_id, request.app.state.sessions_dir) is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    data = _load_meeting_summary(meeting_id)
    if data is None:
        return JSONResponse({"error": f"No summary for meeting '{meeting_id}'"}, status_code=404)
    date, committee, title = _meeting_title(data["meeting"], meeting_id)
    return {
        "meeting_id":  meeting_id,
        "date":        date,
        "committee":   committee,
        "title":       title,
        "is_protocol": bool(data["meeting"].get("is_protocol")),
        "topics":      _summary_sections(data),
    }


@app.get("/api/research/{session_id}/meeting/{meeting_id}/transcript")
def research_meeting_transcript(session_id: str, meeting_id: str, request: Request):
    """
    Return the transcript of a retrieved meeting as a list of chunks.
    """
    if (invalid := _invalid_meeting_route(session_id, meeting_id)) is not None:
        return invalid

    from web.session import load_session

    if load_session(session_id, request.app.state.sessions_dir) is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    from utils.meeting import load_meeting, format_meeting_chunks, count_header_chunks
    transcript_path = get_transcript_path_from_id(meeting_id)
    if not transcript_path or not transcript_path.exists():
        print(f"[transcript] no transcript file for meeting {meeting_id}: {transcript_path}", flush=True)
        return JSONResponse({"error": f"No transcript for meeting '{meeting_id}'."}, status_code=404)

    meeting = load_meeting(transcript_path)

    parts = transcript_path.stem.split("_")
    date = f"{parts[0]}/{parts[1]}/{parts[2]}" if len(parts) >= 4 else ""
    committee = transcript_path.parent.name

    chunks = format_meeting_chunks(meeting)
    return {
        "meeting_id":   meeting_id,
        "date":         date,
        "committee":    committee,
        "chunks":       chunks,
        "header_count": count_header_chunks(chunks),
    }


@app.get("/api/research/{session_id}/meeting/{meeting_id}/hits")
def research_meeting_hits(session_id: str, meeting_id: str, q: str = ""):
    """Speeches of the meeting matching q (keyword FTS): score in (0, 1] (1 = best, for ordering),
    matched_words of query_words, and ranges ([offset, length] of each matched word in the speech text).
    Feeds the heatmap and the keyword highlights."""
    if (invalid := _invalid_meeting_route(session_id, meeting_id)) is not None:
        return invalid
    query = valid.keyword_query(q, config.WEB_MAX_HITS_QUERY_CHARS, config.WEB_MAX_HITS_QUERY_WORDS)
    if not query:
        return {"hits": []}
    word_matches = _speech_word_matches(query)
    if not word_matches or not store.exists():
        return {"hits": []}
    exact_word_matches = _speech_exact_word_matches(query)

    conn = _connect_for_query()
    try:
        rows = store.meeting_speech_hits(conn, word_matches, meeting_id, exact_word_matches)
        ranges_by_speech = store.meeting_speech_keyword_ranges(conn, word_matches, meeting_id)
    except sqlite3.Error as exc:
        print(f"[hits] FTS query failed for meeting {meeting_id}, {word_matches!r}: {exc}", flush=True)
        if store.deadline_passed(conn):
            return JSONResponse({"error": QUERY_TIMEOUT_MESSAGE}, status_code=503)
        rows, ranges_by_speech = [], {}
    finally:
        conn.close()
    # Rank by words matched as typed, then matched word count; bm25 relevance only breaks ties.
    best_relevance = max((r["relevance"] for r in rows), default=0.0) or 1.0
    word_count_weight = len(word_matches) + 1
    rank_keys = {r["speech_idx"]: r["exact_words"] * word_count_weight + r["matched_words"]
                 + 0.5 * max(r["relevance"], 0.0) / best_relevance for r in rows}
    best_rank_key = max(rank_keys.values(), default=1.0)
    return {"hits": [{"speech_idx": r["speech_idx"], "score": rank_keys[r["speech_idx"]] / best_rank_key,
                      "matched_words": r["matched_words"], "query_words": len(word_matches),
                      "ranges": ranges_by_speech.get(r["speech_idx"], [])} for r in rows]}


@app.get("/api/research/{session_id}/meeting/{meeting_id}/participants")
def research_meeting_participants(session_id: str, meeting_id: str, request: Request):
    """Return the list of speakers / attendees for a meeting."""
    if (invalid := _invalid_meeting_route(session_id, meeting_id)) is not None:
        return invalid

    from web.session import load_session

    if load_session(session_id, request.app.state.sessions_dir) is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    from utils.meeting import load_meeting, extract_attendance
    transcript_path = get_transcript_path_from_id(meeting_id)
    if not transcript_path or not transcript_path.exists():
        return JSONResponse({"participants": []})

    meeting = load_meeting(transcript_path)
    participants = extract_attendance(meeting)

    return {"meeting_id": meeting_id, "participants": participants}


# ── Workspace routes ──────────────────────────────────────────────────────────

_workspace_lock = threading.Lock()


@app.post("/api/research/{session_id}/workspace/select")
def workspace_select(session_id: str, req: WorkspaceSelectRequest, request: Request):
    """Append a transcript chunk to the session workspace for later querying."""
    if not _ok_session_id(session_id):
        return JSONResponse({"error": "Invalid session ID"}, status_code=400)
    chunk_text = valid.verbatim_text(req.text, "text", config.WEB_MAX_WORKSPACE_CHUNK_CHARS)
    if not chunk_text.strip():
        return JSONResponse({"error": "empty text"}, status_code=400)

    from web.session import load_session, save_session

    sessions_dir = request.app.state.sessions_dir
    with _workspace_lock:
        session = load_session(session_id, sessions_dir)
        if session is None:
            return JSONResponse({"error": "Session not found"}, status_code=404)

        workspace = session.workspace_data or {}
        selected: list[dict] = workspace.setdefault("selected_chunks", [])
        if len(selected) >= config.WEB_MAX_WORKSPACE_SELECTED_CHUNKS:
            return JSONResponse(
                {"error": f"at most {config.WEB_MAX_WORKSPACE_SELECTED_CHUNKS} selected chunks per session"},
                status_code=400)

        selected.append({
            "chunk_id":          req.chunk_id,
            "text":              chunk_text,
            "source_meeting_id": req.source_meeting_id,
        })
        save_session(dataclass_replace(session, workspace_data=workspace, updated_at=_now_iso()), sessions_dir)

    return {"ok": True, "total_selected": len(selected)}


def _workspace_context(session, meeting_id: str | None) -> str:
    """Selected chunks (+ the meeting's summary first when it fits), capped at WEB_WORKSPACE_ASK_MAX_CONTEXT_CHARS."""
    max_context_chars = config.WEB_WORKSPACE_ASK_MAX_CONTEXT_CHARS
    workspace = session.workspace_data or {}
    context_parts: list[str] = []
    used_chars = 0
    for chunk in workspace.get("selected_chunks", []):
        text = str(chunk.get("text", "")).strip()
        if not text:
            continue
        piece = f"[ישיבה {chunk.get('source_meeting_id', '')}]\n{text}"
        if used_chars + len(piece) > max_context_chars:
            break
        context_parts.append(piece)
        used_chars += len(piece)

    if meeting_id and used_chars < max_context_chars:
        data = _load_meeting_summary(meeting_id)
        if data:
            from summarization.summary_io import render_summary_text
            summary_block = "סיכום ישיבה:\n" + render_summary_text(
                data["topics"], data["opinions"][:30], data["attendance"])
            if used_chars + len(summary_block) <= max_context_chars:
                context_parts.insert(0, summary_block)

    return "\n\n---\n\n".join(context_parts) if context_parts else "(אין מידע נבחר)"


def _stream_workspace_answer(backend, prepared_messages: list[dict], emit: Callable[[object], None],
                             stop_requested: threading.Event, slots: threading.BoundedSemaphore,
                             request_id: str | None = None) -> None:
    """Relay the model's events through emit until done or stop_requested; closing the backend
    stream drops the model connection so generation stops too. Releases one of slots."""
    answer_events = None
    try:
        answer_events = backend.stream(prepared_messages, tools=None, temperature=0.7,
                                       max_tokens=config.WEB_WORKSPACE_ASK_MAX_TOKENS)
        for event in answer_events:
            if stop_requested.is_set():
                print("[workspace_ask] client is gone; stopping generation", flush=True)
                break
            emit(event)
    except Exception as exc:
        print(f"[workspace_ask] generation failed: {exc}", flush=True)
        log_server_error(request_id, "workspace_ask generation failed", traceback.format_exc())
        emit(("__error__", generic_error_message(request_id)))
    finally:
        if answer_events is not None:
            try:
                answer_events.close()
            except Exception as exc:
                print(f"[workspace_ask] closing the model stream failed: {exc}", flush=True)
        slots.release()
        emit(_SENTINEL)


async def _workspace_answer_sse_events(queue: asyncio.Queue, stop_requested: threading.Event,
                                       request_id: str | None):
    from agent.llm.base import TokenEvent
    try:
        while True:
            item = await _next_worker_item(queue)
            if item is _KEEP_ALIVE:
                yield ": keep-alive\n\n"
                continue
            if item is _SENTINEL:
                yield _sse("done", {})
                break
            if isinstance(item, tuple) and len(item) == 2 and item[0] == "__error__":
                yield _sse("error", {"error": item[1], "request_id": request_id})
                break
            if isinstance(item, TokenEvent):
                yield _sse("token", {"text": item.text})
    except Exception as exc:
        print(f"[workspace_ask] answer stream failed: {exc}", flush=True)
        log_server_error(request_id, "workspace_ask answer stream failed", traceback.format_exc())
        yield _sse("error", {"error": generic_error_message(request_id), "request_id": request_id})
    finally:
        stop_requested.set()


@app.post("/api/research/{session_id}/workspace/ask")
async def workspace_ask(session_id: str, req: WorkspaceAskRequest, request: Request):
    """
    Ask the visitor's chosen model a question grounded in the session's selected workspace chunks,
    with the visitor's Gemini key.

    Streams SSE token/done/error events.
    """
    if not _ok_session_id(session_id):
        return JSONResponse({"error": "Invalid session ID"}, status_code=400)
    question = _clean_question(req.question)
    if not question:
        return JSONResponse({"error": "שאלה ריקה"}, status_code=400)
    if not _ok_question(question):
        return JSONResponse({"error": "שאלה מכילה תווים לא חוקיים או ארוכה מדי"}, status_code=400)
    if req.meeting_id is not None and not _ok_meeting_id(req.meeting_id):
        return JSONResponse({"error": "Invalid meeting ID"}, status_code=400)

    gemini_api_key, gemini_key_error = await asyncio.to_thread(visitor_gemini_key_or_error, request)
    if gemini_key_error is not None:
        return gemini_key_error
    models_error = await asyncio.to_thread(unavailable_models_error, gemini_api_key, {req.model})
    if models_error is not None:
        return models_error

    from web.session import load_session

    session = await asyncio.to_thread(load_session, session_id, request.app.state.sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    _log_visitor_question(request, session_id, question)

    context = await asyncio.to_thread(_workspace_context, session, req.meeting_id)
    system_prompt = (
        "אתה עוזר לניתוח פרוטוקולים של ועדות הכנסת. "
        "ענה בעברית בהתבסס על המידע שניתן לך בלבד."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": f"הקשר:\n{context}\n\n---\n\nשאלה: {question}"},
    ]
    backend = research_llm_backend(gemini_api_key, req.model)
    prepared = backend.prepare_messages(messages, suppress_thinking=True)

    slots = _WORKSPACE_ASK_SLOTS
    if not slots.acquire(blocking=False):
        return _busy_response()
    queue: asyncio.Queue = asyncio.Queue()
    stop_requested = threading.Event()
    threading.Thread(target=_stream_workspace_answer,
                     args=(backend, prepared, _thread_safe_emitter(queue), stop_requested, slots,
                           request_id_of(request)),
                     daemon=True).start()
    return _sse_response(_workspace_answer_sse_events(queue, stop_requested, request_id_of(request)))


# ── SSE helper ────────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
