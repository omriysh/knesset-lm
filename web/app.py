"""
app.py

FastAPI web application for the KnessetLM agent.

Startup
-------
The lifespan context manager loads the singletons once:
  - StateMachine       (agent graph)
  - GemmaLlamaBackend  (LLM client)
  - tool_registry      (raises at startup if any machine tool_name is unknown)

Protocol search is keyword-only (FTS5 over Data/knesset.db); no embedding
model or vector store is loaded.

Routes (main)
-------------
  GET  /                                            -> index.html
  POST /api/query                                   -> SSE stream
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
import traceback
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Bootstrap sys.path before importing knesset-lm modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
from agent.llm.gemma import GemmaLlamaBackend

# ── Tool-result lazy-load cache ───────────────────────────────────────────────
# Maps ref_id → full tool result text.  Populated when subgraph step_completed
# events are streamed; served by GET /api/research/{sid}/tool_result/{ref_id}.
_TOOL_RESULT_CACHE: dict[str, str] = {}
_TOOL_RESULT_LOCK = threading.Lock()
_TOOL_RESULT_CAP  = 5000

# ── Concurrency controls ──────────────────────────────────────────────────────
# Limits simultaneous active research sessions so the llama-server queue does
# not become saturated.  Clients that exceed this see a "queued" SSE
# event and wait until a slot opens.
_RESEARCH_SEM = threading.Semaphore(5)

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


def _ok_ref_id(rid: str) -> bool:
    return bool(_REF_ID_RE.fullmatch(rid))


# ── Meeting info cache (meeting_id → {date, committee}) ───────────────────────
_MEETING_INFO_CACHE: dict[str, dict] = {}


def _get_meeting_info(meeting_id: str) -> dict:
    """Resolve a meeting_id to {date: DD/MM/YYYY, committee: Hebrew name}.

    Scans raw_transcriptions across all Knesset numbers; filenames are
    DD_MM_YYYY_<session_id>.json, stored under <knesset_num>/<committee>/.
    Result is cached in-process.  Returns {} if not found.
    """
    import glob as _glob
    if not meeting_id.isdigit():
        return {}
    if meeting_id in _MEETING_INFO_CACHE:
        return _MEETING_INFO_CACHE[meeting_id]
    base = config.transcriptions_dir(25).parent  # Data/raw_transcriptions/
    matches = _glob.glob(str(base / "**" / f"*_{meeting_id}.json"), recursive=True)
    info: dict = {}
    if matches:
        p = Path(matches[0])
        parts = p.stem.split("_")            # DD_MM_YYYY_session_id
        if len(parts) >= 4:
            info["date"] = f"{parts[0]}/{parts[1]}/{parts[2]}"
        info["committee"] = p.parent.name.replace("_", " ")
    _MEETING_INFO_CACHE[meeting_id] = info
    return info


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
            except Exception:
                pass  # keep as string

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
from utils.tools import _expand_match, _quote_match


# ── Summary helpers (knesset.db) ──────────────────────────────────────────────

_SECTION_BY_NUM = {1: "topics", 2: "opinions", 3: "attendance"}


def _load_meeting_summary(meeting_id: str) -> dict | None:
    """{meeting, topics, opinions, attendance} from knesset.db, or None when the meeting has no summary."""
    if not store.exists():
        print(f"[web] {store.db_path()} not built; run scripts/build_knesset_db.py", flush=True)
        return None
    conn = store.connect()
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

    print("[web] Loading machine …", flush=True)
    machine = StateMachine(settings.MACHINE_PATH)
    print(f"[web]   Machine: '{machine.name}' (v{machine.version})", flush=True)

    backend = GemmaLlamaBackend(url=settings.LLAMA_SERVER)

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
    cleanup_stale_sessions(settings.SESSIONS_DIR)

    # ── Store all state on app ────────────────────────────────────────────────
    app.state.machine       = machine
    app.state.backend       = backend
    app.state.tool_registry = tool_registry
    app.state.settings      = settings
    app.state.sessions_dir  = settings.SESSIONS_DIR

    print(f"[web] Ready — {settings.MACHINE_PATH.name}", flush=True)
    yield

    # Cleanup (none needed for local app)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="KnessetLM", lifespan=lifespan)

_STATIC_DIR    = Path(__file__).parent / "static"
_TEMPLATES_DIR = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_MK_PHOTOS_DIR = config.MK_PHOTOS_DIR
_MK_PHOTO_EXTS = (".jpeg", ".jpg", ".png")

# Honorific / role prefixes stripped before photo lookup. Mirrors browser.js
# _speakerPhotoKey but also covers forms it misses (notably the definite-
# article היו"ר, שר roles, מ"מ). Speaker strings arrive as e.g. 'ח"כ אבי דיכטר',
# 'היו"ר עמית הלוי', 'השר יריב לוין'.
_HONORIFIC_RE = re.compile(
    r'^(ח"כ|ח\'כ|היו"ר|יו"ר|מ"מ\s+היו"ר|מ"מ|סגן\s+השר|סגנית\s+השרה|השרה|השר|שרה|שר|מנכ"ל|ד"ר|פרופ\'?)\s+'
)

# Lazy singleton fuzzy index over mks.db (label = canonical MK name, which
# matches the photo filenames). Loading scans the whole table, so cache it —
# the reading tab fires one /mk-photo request per distinct speaker.
_mk_fuzzy_index = None
_mk_fuzzy_loaded = False
_mk_photo_cache: dict[str, "Path | None"] = {}


def _get_mk_fuzzy_index():
    global _mk_fuzzy_index, _mk_fuzzy_loaded
    if _mk_fuzzy_loaded:
        return _mk_fuzzy_index
    _mk_fuzzy_loaded = True
    _mk_fuzzy_index = _load_mk_fuzzy_index(25)
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


def _photo_file_for(stem: str) -> "Path | None":
    stem = stem.strip()
    if not stem:
        return None
    for ext in _MK_PHOTO_EXTS:
        p = _MK_PHOTOS_DIR / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def _resolve_mk_photo(name: str) -> "Path | None":
    """Resolve a (possibly honorific-prefixed / variant) speaker name to a
    photo file: exact match → prefix-stripped exact match → fuzzy resolve to
    a canonical MK name. Returns None for non-MKs (guests, section headers)."""
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

    _mk_photo_cache[name] = result
    return result


@app.get("/mk-photo/{name}")
async def mk_photo(name: str):
    p = _resolve_mk_photo(name)
    if p is not None:
        return FileResponse(str(p), media_type=f"image/{p.suffix.lstrip('.')}")
    return JSONResponse({}, status_code=404)


# ── Models ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str


class ResearchStartRequest(BaseModel):
    question: str


class ResearchRespondRequest(BaseModel):
    output_var: str
    value: Any


# ── Query log ─────────────────────────────────────────────────────────────────
_QUERY_LOG      = Path(__file__).parent / "query_log.jsonl"
_QUERY_LOG_LOCK = threading.Lock()

def _log_query(question: str, ip: str) -> None:
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ip": ip,
        "q":  question,
    }, ensure_ascii=False)
    with _QUERY_LOG_LOCK:
        with open(_QUERY_LOG, "a", encoding="utf-8") as f:
            f.write(entry + "\n")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/help", response_class=PlainTextResponse)
async def help_content():
    path = Path(__file__).parent / "templates" / "user-help.md"
    return path.read_text(encoding="utf-8")


@app.get("/api/health")
async def health(request: Request):
    db_row_counts: dict[str, int] = {}
    if store.exists():
        def _count_rows():
            conn = store.connect()
            try:
                return store.table_row_counts(conn)
            finally:
                conn.close()
        try:
            db_row_counts = await asyncio.get_event_loop().run_in_executor(None, _count_rows)
        except Exception as exc:
            print(f"[web] health: row count failed: {exc}", flush=True)
    return {
        "status":  "ok",
        "machine": request.app.state.machine.name,
        "db_path": str(store.db_path()),
        "db":      db_row_counts,
    }


@app.post("/api/query")
async def query(req: QueryRequest, request: Request):
    question = req.question.strip()
    if not question:
        return JSONResponse({"error": "שאלה ריקה"}, status_code=400)
    if not _ok_question(question):
        return JSONResponse({"error": "שאלה מכילה תווים לא חוקיים או ארוכה מדי"}, status_code=400)

    _log_query(question, request.client.host if request.client else "unknown")

    machine       = request.app.state.machine
    backend       = request.app.state.backend
    tool_registry = request.app.state.tool_registry

    async def generate():
        loop      = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def _run_sync():
            from agent.subgraph.llm_bridge import set_thread_event_sink
            set_thread_event_sink(
                lambda ev: loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("subgraph_event", {"kind": ev.kind, "name": ev.name, "payload": ev.payload}),
                )
            )
            if not _RESEARCH_SEM.acquire(blocking=False):
                loop.call_soon_threadsafe(queue.put_nowait, ("queued", {}))
                _RESEARCH_SEM.acquire()
            try:
                runner = MachineRunner(
                    machine       = machine,
                    backend       = backend,
                    tool_registry = tool_registry,
                )
                for event in runner.run_stream(question):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("error", str(exc) + "\n" + traceback.format_exc()),
                )
            finally:
                _RESEARCH_SEM.release()
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        thread = threading.Thread(target=_run_sync, daemon=True)
        thread.start()

        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                ev_type, ev_data = item
                if ev_type == "token":
                    yield _sse("token",          {"text": ev_data})
                elif ev_type == "status":
                    yield _sse("status",         {"msg": ev_data})
                elif ev_type == "node_start":
                    yield _sse("node_start",     ev_data)
                elif ev_type == "thinking_token":
                    yield _sse("thinking_token", {"text": ev_data})
                elif ev_type == "node_result":
                    yield _sse("node_result",    ev_data)
                elif ev_type == "subgraph_event":
                    yield _sse("subgraph_event", {
                        "type":    "subgraph_event",
                        "kind":    ev_data.get("kind"),
                        "name":    ev_data.get("name"),
                        "payload": ev_data.get("payload", {}),
                    })
                elif ev_type == "queued":
                    yield _sse("queued",         {})
                elif ev_type == "done":
                    yield _sse("done",           {})
                elif ev_type == "error":
                    yield _sse("error",          {"error": ev_data})
        except Exception as exc:
            yield _sse("error", {"error": str(exc) + "\n" + traceback.format_exc()})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Research session routes ───────────────────────────────────────────────────

@app.post("/api/research/start")
async def research_start(req: ResearchStartRequest, request: Request):
    """
    Start a new research session.

    Streams SSE events.  The FIRST event is always ``session_id``.
    If the machine pauses at a ``user_input`` node, emits ``user_input_required``
    followed by ``user_paused`` and then closes the stream.
    """
    from web.session import ResearchSession, save_session

    question = req.question.strip()
    if not question:
        return JSONResponse({"error": "שאלה ריקה"}, status_code=400)
    if not _ok_question(question):
        return JSONResponse({"error": "שאלה מכילה תווים לא חוקיים או ארוכה מדי"}, status_code=400)

    machine       = request.app.state.machine
    backend       = request.app.state.backend
    tool_registry = request.app.state.tool_registry
    sessions_dir  = request.app.state.sessions_dir

    session_id = str(uuid.uuid4())

    async def generate():
        from datetime import datetime, timezone

        def _now():
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        # First event: session_id
        yield _sse("session_id", {"session_id": session_id})

        # Persist "running" status immediately — reconnect endpoint uses this
        # to signal still-in-progress to clients that reconnect mid-execution.
        _run_ts = _now()
        save_session(ResearchSession(
            session_id=session_id, status="running",
            original_question=question, created_at=_run_ts, updated_at=_run_ts,
        ), sessions_dir)

        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()
        _event_log: list[dict] = []  # selective event log for reconnect replay

        def _run_sync():
            from agent.subgraph.llm_bridge import set_thread_event_sink
            set_thread_event_sink(
                lambda ev: loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("subgraph_event", {"kind": ev.kind, "name": ev.name, "payload": ev.payload}),
                )
            )
            if not _RESEARCH_SEM.acquire(blocking=False):
                loop.call_soon_threadsafe(queue.put_nowait, ("queued", {}))
                _RESEARCH_SEM.acquire()
            _final_token = ""
            _outcome: tuple | None = None  # ('done', answer) | ('error', msg) | ('user_paused',)
            try:
                runner = MachineRunner(
                    machine       = machine,
                    backend       = backend,
                    tool_registry = tool_registry,
                )
                for event in runner.run_stream(question):
                    _t, _d = event
                    if _t == "token":
                        _final_token += _d
                    elif _t == "done":
                        _outcome = ("done", _final_token)
                    elif _t == "error":
                        _outcome = ("error", _d)
                    elif _t == "user_input_required":
                        _outcome = ("user_paused", _d)  # store checkpoint so finally can save it
                    elif _t == "node_start":
                        if isinstance(_d, dict) and _d.get("subgraph"):
                            _event_log.append({"type": "node_start", "data": _d})
                    elif _t == "node_result":
                        _d = _strip_footnote_fulls(_d, registry=tool_registry)
                        event = (_t, _d)
                        if isinstance(_d, dict) and _d.get("subgraph"):
                            _event_log.append({"type": "node_result", "data": _d})
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
                            _event_log.append({"type": "subgraph_event", "data": _d})
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:
                _err = str(exc) + "\n" + traceback.format_exc()
                loop.call_soon_threadsafe(queue.put_nowait, ("error", _err))
                _outcome = ("error", _err)
            finally:
                _RESEARCH_SEM.release()
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)
                # Safety net: generate() may be cancelled by client disconnect.
                # _event_log is complete here (built above), so the saved session
                # has full footnotes/citations for reconnect replay.
                if not _outcome:
                    pass
                elif _outcome[0] == "user_paused":
                    # Client disconnected while agent reached a user-input node.
                    # Save awaiting_user so the session can be resumed on reconnect.
                    try:
                        _ts = _now()
                        save_session(ResearchSession(
                            session_id=session_id, status="awaiting_user",
                            original_question=question,
                            created_at=_run_ts, updated_at=_ts,
                            machine_checkpoint=_outcome[1],
                        ), sessions_dir)
                    except Exception as exc:
                        print(f"[research_start] safety-net save failed: {exc}", flush=True)
                else:
                    try:
                        _ts = _now()
                        if _outcome[0] == "done":
                            save_session(ResearchSession(
                                session_id=session_id, status="done",
                                original_question=question,
                                created_at=_run_ts, updated_at=_ts,
                                final_answer=_outcome[1],
                                event_log=list(_event_log) or None,
                            ), sessions_dir)
                        else:
                            save_session(ResearchSession(
                                session_id=session_id, status="error",
                                original_question=question,
                                created_at=_run_ts, updated_at=_ts,
                                error=str(_outcome[1]),
                            ), sessions_dir)
                    except Exception as exc:
                        print(f"[research_start] safety-net save failed: {exc}", flush=True)

        thread = threading.Thread(target=_run_sync, daemon=True)
        thread.start()

        created_at  = _now()
        final_token = ""

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is _SENTINEL:
                    break
                ev_type, ev_data = item

                if ev_type == "token":
                    final_token += ev_data
                    yield _sse("token", {"text": ev_data})

                elif ev_type == "status":
                    yield _sse("status", {"msg": ev_data})

                elif ev_type == "node_start":
                    yield _sse("node_start", ev_data)

                elif ev_type == "thinking_token":
                    yield _sse("thinking_token", {"text": ev_data})

                elif ev_type == "node_result":
                    yield _sse("node_result", ev_data)

                elif ev_type == "subgraph_event":
                    yield _sse("subgraph_event", ev_data)

                elif ev_type == "queued":
                    yield _sse("queued", {})

                elif ev_type == "user_input_required":
                    # ev_data contains the full payload including "checkpoint"
                    checkpoint   = ev_data.get("checkpoint", {})
                    pending_ui   = checkpoint.get("pending_ui_event", {})

                    session = ResearchSession(
                        session_id          = session_id,
                        status              = "awaiting_user",
                        original_question   = question,
                        created_at          = created_at,
                        updated_at          = _now(),
                        machine_checkpoint  = checkpoint,
                        final_answer        = None,
                        error               = None,
                    )
                    save_session(session, sessions_dir)

                    # Emit ui event without the internal "checkpoint" key
                    ui_event = {k: v for k, v in ev_data.items() if k != "checkpoint"}
                    ui_event["session_id"] = session_id
                    yield _sse("user_input_required", ui_event)
                    yield _sse("user_paused", {"session_id": session_id})
                    return  # close stream

                elif ev_type == "done":
                    session = ResearchSession(
                        session_id          = session_id,
                        status              = "done",
                        original_question   = question,
                        created_at          = created_at,
                        updated_at          = _now(),
                        machine_checkpoint  = None,
                        final_answer        = final_token,
                        error               = None,
                        event_log           = _event_log or None,
                    )
                    save_session(session, sessions_dir)
                    yield _sse("done", {})
                    return

                elif ev_type == "error":
                    session = ResearchSession(
                        session_id          = session_id,
                        status              = "error",
                        original_question   = question,
                        created_at          = created_at,
                        updated_at          = _now(),
                        machine_checkpoint  = None,
                        final_answer        = None,
                        error               = ev_data,
                    )
                    save_session(session, sessions_dir)
                    yield _sse("error", {"error": ev_data})
                    return

        except Exception as exc:
            err = str(exc) + "\n" + traceback.format_exc()
            yield _sse("error", {"error": err})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)

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

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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

    from web.session import load_session, save_session, ResearchSession

    sessions_dir  = request.app.state.sessions_dir
    session       = load_session(session_id, sessions_dir)

    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if session.status != "awaiting_user":
        return JSONResponse(
            {"error": f"Session is not awaiting user input (status: {session.status})"},
            status_code=409,
        )

    machine       = request.app.state.machine
    backend       = request.app.state.backend
    tool_registry = request.app.state.tool_registry

    checkpoint = session.machine_checkpoint or {}

    question       = session.original_question
    user_response  = {"output_var": req.output_var, "value": req.value}

    async def generate():
        from datetime import datetime, timezone

        def _now():
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()
        _event_log: list[dict] = []  # selective event log for reconnect replay

        def _run_sync():
            if not _RESEARCH_SEM.acquire(blocking=False):
                loop.call_soon_threadsafe(queue.put_nowait, ("queued", {}))
                _RESEARCH_SEM.acquire()
            _final_token = ""
            _outcome: tuple | None = None
            try:
                runner = MachineRunner(
                    machine       = machine,
                    backend       = backend,
                    tool_registry = tool_registry,
                )
                for event in runner.run_stream(
                    question      = question,
                    resume        = checkpoint,
                    user_response = user_response,
                ):
                    _t, _d = event
                    if _t == "token":
                        _final_token += _d
                    elif _t == "done":
                        _outcome = ("done", _final_token)
                    elif _t == "error":
                        _outcome = ("error", _d)
                    elif _t == "user_input_required":
                        _outcome = ("user_paused", _d)  # store checkpoint so finally can save it
                    elif _t == "node_start":
                        if isinstance(_d, dict) and _d.get("subgraph"):
                            _event_log.append({"type": "node_start", "data": _d})
                    elif _t == "node_result":
                        _d = _strip_footnote_fulls(_d, registry=tool_registry)
                        event = (_t, _d)
                        if isinstance(_d, dict) and _d.get("subgraph"):
                            _event_log.append({"type": "node_result", "data": _d})
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
                            _event_log.append({"type": "subgraph_event", "data": _d})
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:
                _err = str(exc) + "\n" + traceback.format_exc()
                loop.call_soon_threadsafe(queue.put_nowait, ("error", _err))
                _outcome = ("error", _err)
            finally:
                _RESEARCH_SEM.release()
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)
                if not _outcome:
                    pass
                elif _outcome[0] == "user_paused":
                    try:
                        _ts = _now()
                        save_session(ResearchSession(
                            session_id=session_id, status="awaiting_user",
                            original_question=question,
                            created_at=session.created_at, updated_at=_ts,
                            machine_checkpoint=_outcome[1],
                            workspace_data=session.workspace_data,
                        ), sessions_dir)
                    except Exception as exc:
                        print(f"[research_respond] safety-net save failed: {exc}", flush=True)
                else:
                    try:
                        _ts = _now()
                        if _outcome[0] == "done":
                            save_session(ResearchSession(
                                session_id=session_id, status="done",
                                original_question=question,
                                created_at=session.created_at, updated_at=_ts,
                                final_answer=_outcome[1],
                                event_log=list(_event_log) or None,
                            ), sessions_dir)
                        else:
                            save_session(ResearchSession(
                                session_id=session_id, status="error",
                                original_question=question,
                                created_at=session.created_at, updated_at=_ts,
                                error=str(_outcome[1]),
                            ), sessions_dir)
                    except Exception as exc:
                        print(f"[research_respond] safety-net save failed: {exc}", flush=True)

        thread = threading.Thread(target=_run_sync, daemon=True)
        thread.start()

        final_token = ""

        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if item is _SENTINEL:
                    break
                ev_type, ev_data = item

                if ev_type == "token":
                    final_token += ev_data
                    yield _sse("token", {"text": ev_data})

                elif ev_type == "status":
                    yield _sse("status", {"msg": ev_data})

                elif ev_type == "node_start":
                    yield _sse("node_start", ev_data)

                elif ev_type == "thinking_token":
                    yield _sse("thinking_token", {"text": ev_data})

                elif ev_type == "node_result":
                    yield _sse("node_result", ev_data)

                elif ev_type == "subgraph_event":
                    yield _sse("subgraph_event", ev_data)

                elif ev_type == "queued":
                    yield _sse("queued", {})

                elif ev_type == "user_input_required":
                    new_checkpoint = ev_data.get("checkpoint", {})

                    updated = ResearchSession(
                        session_id          = session_id,
                        status              = "awaiting_user",
                        original_question   = question,
                        created_at          = session.created_at,
                        updated_at          = _now(),
                        machine_checkpoint  = new_checkpoint,
                        workspace_data      = session.workspace_data,
                        final_answer        = None,
                        error               = None,
                    )
                    save_session(updated, sessions_dir)

                    ui_event = {k: v for k, v in ev_data.items() if k != "checkpoint"}
                    ui_event["session_id"] = session_id
                    yield _sse("user_input_required", ui_event)
                    yield _sse("user_paused", {"session_id": session_id})
                    return

                elif ev_type == "done":
                    updated = ResearchSession(
                        session_id          = session_id,
                        status              = "done",
                        original_question   = question,
                        created_at          = session.created_at,
                        updated_at          = _now(),
                        machine_checkpoint  = None,
                        final_answer        = final_token,
                        error               = None,
                        event_log           = _event_log or None,
                    )
                    save_session(updated, sessions_dir)
                    yield _sse("done", {})
                    return

                elif ev_type == "error":
                    updated = ResearchSession(
                        session_id          = session_id,
                        status              = "error",
                        original_question   = question,
                        created_at          = session.created_at,
                        updated_at          = _now(),
                        machine_checkpoint  = None,
                        final_answer        = None,
                        error               = ev_data,
                    )
                    save_session(updated, sessions_dir)
                    yield _sse("error", {"error": ev_data})
                    return

        except Exception as exc:
            err = str(exc) + "\n" + traceback.format_exc()
            yield _sse("error", {"error": err})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/api/research/{session_id}")
async def research_delete(session_id: str, request: Request):
    """Delete a research session file from disk."""
    if not _ok_session_id(session_id):
        from fastapi.responses import Response
        return Response(status_code=400)

    from web.session import delete_session

    sessions_dir = request.app.state.sessions_dir
    delete_session(session_id, sessions_dir)
    # Return 204 regardless of whether the file existed (idempotent delete)
    from fastapi.responses import Response
    return Response(status_code=204)


@app.get("/api/research/{session_id}/tool_result/{ref_id}")
async def get_tool_result(session_id: str, ref_id: str):
    """Return the full text for a lazily-loaded tool result panel."""
    if not _ok_session_id(session_id) or not _ok_ref_id(ref_id):
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)
    full = _TOOL_RESULT_CACHE.get(ref_id)
    if full is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"full": full})


# ── Browse (reading tab) ──────────────────────────────────────────────────────

class BrowseFilterRequest(BaseModel):
    committees: list[str] = []
    mks:        list[str] = []
    parties:    list[str] = []
    guest:      str | None = None
    date_from:  str | None = None
    date_to:    str | None = None


class BrowseSearchRequest(BaseModel):
    query:   str = ""
    top_k:   int | None = None
    sort:    str = "relevance"
    filters: BrowseFilterRequest | None = None


@app.get("/api/meta")
async def get_meta():
    """Return committees, MKs, and parties for filter dropdowns."""
    from utils.knesset_db import get_all_committees, get_all_mks, get_all_parties
    loop = asyncio.get_event_loop()
    committees, mks, parties = await asyncio.gather(
        loop.run_in_executor(None, lambda: get_all_committees(25)),
        loop.run_in_executor(None, lambda: get_all_mks(25)),
        loop.run_in_executor(None, lambda: get_all_parties(25)),
    )
    def _mk_name(m: dict) -> str:
        first = (m.get("mk_individual_first_name") or "").strip()
        last  = (m.get("mk_individual_name")       or "").strip()
        return f"{first} {last}".strip() or last or first

    return {
        "committees": [c["Name"] for c in committees],
        "mks":        sorted({_mk_name(m) for m in mks if _mk_name(m)}),
        "parties":    [p["party"] for p in parties],
    }


def _speech_match_expression(query: str) -> str:
    """FTS5 MATCH expression over speeches_fts: lemmatized, ktiv-expanded, tokens AND-ed."""
    normalized = lemmatize(query)
    return _expand_match(normalized, "speeches_fts") or _quote_match(normalized)


_HEATMAP_MIN_WORD_CHARS = 3


def _speech_word_matches(query: str) -> list[str]:
    """One FTS5 MATCH expression per distinct query word (ktiv-expanded), for ranking speeches by
    how many of the words they contain. Words shorter than 3 letters (על, של, את) are dropped
    unless nothing else remains."""
    words = list(dict.fromkeys(lemmatize(query).split()))
    content_words = [w for w in words if len(w) >= _HEATMAP_MIN_WORD_CHARS] or words
    return [m for m in (_expand_match(w, "speeches_fts") or _quote_match(w) for w in content_words) if m.strip()]


def _resolve_participant_filters(filters: BrowseFilterRequest, knesset_num: int) -> tuple[list[str], str | None]:
    """
    (mk_ids, guest_name) for the MK-name and guest filters. The guest is a
    free-text name: when it does not resolve to an MK it is matched as a
    substring of attendance.name instead of being dropped.
    """
    names = list(filters.mks or []) + ([filters.guest] if filters.guest else [])
    if not names:
        return [], None
    fuzzy_mk_index = _load_mk_fuzzy_index(knesset_num)
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


def _browse_search_meetings(req: BrowseSearchRequest, top_k: int, knesset_num: int) -> list[dict]:
    """
    Candidate meetings from the structural filters, then either FTS over their
    speeches (query given; one row per meeting, best speech as excerpt, score
    normalized so the best meeting is 1.0) or the newest candidates (empty query;
    first summary topic as excerpt, score 0).
    """
    query = req.query.strip()
    filters = req.filters or BrowseFilterRequest()
    mk_ids, guest_name = _resolve_participant_filters(filters, knesset_num)
    conn = store.connect()
    try:
        candidate_meeting_ids = store.query_candidate_meeting_ids(
            conn, knesset_num,
            committees=[c.replace("_", " ").strip() for c in filters.committees] or None,
            date_from=filters.date_from or None, date_to=filters.date_to or None,
            mk_ids=mk_ids or None, parties=list(filters.parties) or None, guest_name=guest_name,
        )
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
                rows = store.meetings_by_best_speech(conn, match, knesset_num, limit=top_k, sort=req.sort,
                                                     candidate_meeting_ids=candidate_meeting_ids)
            except sqlite3.Error as exc:
                print(f"[browse_search] FTS query failed for {match!r}: {exc}", flush=True)
                return []
            best_score = min((r["score"] for r in rows), default=0.0)
            for r in rows:
                r["excerpt"] = store.speech_snippet(conn, match, r["best_speech_rowid"])
                r["score"] = round(r["score"] / best_score, 4) if best_score < 0 else 1.0
    finally:
        conn.close()
    meetings_out = []
    for r in rows:
        date, committee, title = _meeting_title(r, r["meeting_id"])
        meetings_out.append({"meeting_id": r["meeting_id"], "date": date, "committee": committee,
                             "title": title, "excerpt": r["excerpt"], "score": r["score"]})
    return meetings_out


@app.post("/api/browse/search")
async def browse_search(req: BrowseSearchRequest, request: Request):
    """
    Session-less entry point for the reading tab: keyword search over speeches
    (or newest meetings for an empty query) within the structural filters.

    Creates a fresh ResearchSession so the /api/research/{sid}/meeting/... routes
    work against the returned session_id.
    """
    from web.session import ResearchSession, save_session

    query = req.query.strip()
    if len(query) > _MAX_QUESTION:
        return JSONResponse({"error": "שאילתה ארוכה מדי"}, status_code=400)
    if req.sort not in ("relevance", "date"):
        return JSONResponse({"error": f"sort לא חוקי: {req.sort}"}, status_code=400)
    if not store.exists():
        print(f"[browse_search] {store.db_path()} missing", flush=True)
        return JSONResponse({"error": "מסד הנתונים לא נבנה עדיין. יש להריץ scripts/build_knesset_db.py"},
                            status_code=503)

    settings = request.app.state.settings
    top_k = max(1, min(req.top_k or settings.TOP_K_BROWSE, _MAX_TOP_K))
    knesset_num = 25

    meetings_out = await asyncio.get_event_loop().run_in_executor(
        None, _browse_search_meetings, req, top_k, knesset_num)

    session_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    save_session(ResearchSession(
        session_id=session_id, status="done", original_question=query,
        created_at=now, updated_at=now, workspace_data={"selected_chunks": []},
    ), request.app.state.sessions_dir)
    return {"session_id": session_id, "meetings": meetings_out, "query_used": query}


# ── Workspace models ──────────────────────────────────────────────────────────

class WorkspaceSelectRequest(BaseModel):
    chunk_id: str
    text: str
    source_meeting_id: str


class WorkspaceAskRequest(BaseModel):
    question: str
    meeting_id: str | None = None


# ── Workspace routes ──────────────────────────────────────────────────────────



@app.get("/api/research/{session_id}/meeting/{meeting_id}/summary")
async def research_meeting_summary(session_id: str, meeting_id: str, request: Request):
    """
    Return a meeting's summary as sections (attendance, topics, opinions) for the reading tab.
    """
    if not _ok_session_id(session_id) or not meeting_id.isdigit():
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    from web.session import load_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    data = await asyncio.get_event_loop().run_in_executor(None, _load_meeting_summary, meeting_id)
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
async def research_meeting_transcript(session_id: str, meeting_id: str, request: Request):
    """
    Return the transcript of a retrieved meeting as a list of chunks.
    """
    if not _ok_session_id(session_id) or not meeting_id.isdigit():
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    from web.session import load_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    from utils.meeting import load_meeting, format_meeting_chunks
    transcript_path = get_transcript_path_from_id(meeting_id)
    if not transcript_path:
        return JSONResponse(
            {"error": f"No transcript for meeting '{meeting_id}'."},
            status_code=404,
        )
    if not transcript_path.exists():
        return JSONResponse(
            {"error": f"Transcript file not found: {transcript_path}"},
            status_code=404,
        )

    meeting = load_meeting(transcript_path)

    # Derive date and committee from filename
    name = transcript_path.stem
    parts = name.split("_")
    date = f"{parts[0]}/{parts[1]}/{parts[2]}" if len(parts) >= 4 else ""
    committee = transcript_path.parent.name

    return {
        "meeting_id": meeting_id,
        "date":       date,
        "committee":  committee,
        "chunks":     format_meeting_chunks(meeting),
    }


@app.get("/api/research/{session_id}/meeting/{meeting_id}/hits")
async def research_meeting_hits(session_id: str, meeting_id: str, request: Request, q: str = ""):
    """Speeches of the meeting matching q (keyword FTS) with score in (0, 1], 1 = best. Feeds the heatmap."""
    if not _ok_session_id(session_id) or not meeting_id.isdigit():
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)
    query = q.strip()
    if not query:
        return {"hits": []}
    if len(query) > _MAX_QUESTION:
        return JSONResponse({"error": "שאילתה ארוכה מדי"}, status_code=400)
    word_matches = _speech_word_matches(query)
    if not word_matches or not store.exists():
        return {"hits": []}

    def _hits():
        conn = store.connect()
        try:
            return store.meeting_speech_hits(conn, word_matches, meeting_id)
        except sqlite3.Error as exc:
            print(f"[hits] FTS query failed for meeting {meeting_id}, {word_matches!r}: {exc}", flush=True)
            return []
        finally:
            conn.close()

    rows = await asyncio.get_event_loop().run_in_executor(None, _hits)
    # Rank by matched word count; bm25 relevance only breaks ties within the same count.
    best_relevance = max((r["relevance"] for r in rows), default=0.0) or 1.0
    rank_keys = {r["speech_idx"]: r["matched_words"] + 0.5 * max(r["relevance"], 0.0) / best_relevance for r in rows}
    best_rank_key = max(rank_keys.values(), default=1.0)
    return {"hits": [{"speech_idx": idx, "score": key / best_rank_key} for idx, key in rank_keys.items()]}


@app.get("/api/research/{session_id}/meeting/{meeting_id}/participants")
async def research_meeting_participants(session_id: str, meeting_id: str, request: Request):
    """Return the list of speakers / attendees for a meeting."""
    if not _ok_session_id(session_id) or not meeting_id.isdigit():
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    from web.session import load_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    from utils.meeting import load_meeting, extract_attendance
    transcript_path = get_transcript_path_from_id(meeting_id)
    if not transcript_path or not transcript_path.exists():
        return JSONResponse({"participants": []})

    meeting = load_meeting(transcript_path)
    participants = extract_attendance(meeting)

    return {"meeting_id": meeting_id, "participants": participants}


@app.post("/api/research/{session_id}/workspace/select")
async def workspace_select(
    session_id: str,
    req: WorkspaceSelectRequest,
    request: Request,
):
    """Append a transcript chunk to the session workspace for later querying."""
    if not _ok_session_id(session_id):
        return JSONResponse({"error": "Invalid session ID"}, status_code=400)

    from web.session import load_session, save_session
    from datetime import datetime, timezone
    from dataclasses import replace as _dc_replace

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    workspace = session.workspace_data or {}
    selected: list[dict] = workspace.setdefault("selected_chunks", [])

    selected.append({
        "chunk_id":         req.chunk_id,
        "text":             req.text,
        "source_meeting_id": req.source_meeting_id,
    })

    updated = _dc_replace(
        session,
        workspace_data=workspace,
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    )
    save_session(updated, sessions_dir)

    return {"ok": True, "total_selected": len(selected)}


@app.post("/api/research/{session_id}/workspace/ask")
async def workspace_ask(
    session_id: str,
    req: WorkspaceAskRequest,
    request: Request,
):
    """
    Ask the LLM a question grounded in the session's selected workspace chunks.

    Streams SSE token/done/error events.
    """
    if not _ok_session_id(session_id):
        return JSONResponse({"error": "Invalid session ID"}, status_code=400)

    from web.session import load_session
    from agent.llm.base import TokenEvent

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    backend  = request.app.state.backend
    question = req.question.strip()
    if not question:
        return JSONResponse({"error": "שאלה ריקה"}, status_code=400)
    if not _ok_question(question):
        return JSONResponse({"error": "שאלה מכילה תווים לא חוקיים או ארוכה מדי"}, status_code=400)

    workspace = session.workspace_data or {}
    selected_chunks: list[dict] = workspace.get("selected_chunks", [])

    # Build context from selected chunks (capped at ~8000 chars)
    MAX_CTX_CHARS = 8000
    context_parts: list[str] = []
    used_chars = 0
    for chunk in selected_chunks:
        text = chunk.get("text", "").strip()
        if not text:
            continue
        mid   = chunk.get("source_meeting_id", "")
        piece = f"[ישיבה {mid}]\n{text}"
        if used_chars + len(piece) > MAX_CTX_CHARS:
            break
        context_parts.append(piece)
        used_chars += len(piece)

    # Optionally append meeting summary for the requested meeting_id
    if req.meeting_id and used_chars < MAX_CTX_CHARS:
        data = _load_meeting_summary(req.meeting_id)
        if data:
            from summarization.summary_io import render_summary_text
            summary_block = "סיכום ישיבה:\n" + render_summary_text(
                data["topics"], data["opinions"][:30], data["attendance"])
            if used_chars + len(summary_block) <= MAX_CTX_CHARS:
                context_parts.insert(0, summary_block)

    context = "\n\n---\n\n".join(context_parts) if context_parts else "(אין מידע נבחר)"

    system_prompt = (
        "אתה עוזר לניתוח פרוטוקולים של ועדות הכנסת. "
        "ענה בעברית בהתבסס על המידע שניתן לך בלבד."
    )
    user_content = f"הקשר:\n{context}\n\n---\n\nשאלה: {question}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_content},
    ]
    prepared = backend.prepare_messages(messages, suppress_thinking=False)

    async def generate():
        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def _run_sync():
            try:
                for event in backend.stream(prepared, tools=None, temperature=0.7, max_tokens=4096):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("__error__", str(exc) + "\n" + traceback.format_exc()),
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        thread = threading.Thread(target=_run_sync, daemon=True)
        thread.start()

        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    yield _sse("done", {})
                    break
                # Handle error sentinel tuple
                if isinstance(item, tuple) and len(item) == 2 and item[0] == "__error__":
                    yield _sse("error", {"error": item[1]})
                    break
                if isinstance(item, TokenEvent):
                    yield _sse("token", {"text": item.text})
        except Exception as exc:
            yield _sse("error", {"error": str(exc) + "\n" + traceback.format_exc()})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── SSE helper ────────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
