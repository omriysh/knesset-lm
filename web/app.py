"""
app.py

FastAPI web application for the KnessetLM agent.

Startup
-------
The lifespan context manager loads all heavy singletons once:
  - ProtocolEmbedder   (embedding model)
  - chromadb client    (vector store)
  - StateMachine       (agent graph)
  - Qwen3LlamaBackend  (LLM client)
  - tool_registry      (raises at startup if any machine tool_name is unknown)

A threading.Lock protects the embedder from concurrent inference.

Routes
------
  GET  /            → index.html
  POST /api/query   → SSE stream (event: status / node_result / token / done / error)
  GET  /api/health  → {"status": "ok", "machine": "...", "collections": {...}}

Usage (via scripts/run_web.py)
-------------------------------
  cd knesset-lm
  python scripts/run_web.py --cuda --quantize int4
"""

from __future__ import annotations

import asyncio
import json
import re
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

import chromadb
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
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
# Limits simultaneous active research sessions so llama-server and the embedder
# queue do not become saturated.  Clients that exceed this see a "queued" SSE
# event and wait until a slot opens.
_RESEARCH_SEM = threading.Semaphore(5)

# ── Input validators ──────────────────────────────────────────────────────────
_UUID_RE     = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
# Hebrew block + whitespace + digits + dots/commas/question marks (user spec) + !/:-"'  (natural Hebrew punctuation)
_QUESTION_RE = re.compile(r'[֐-׿\s\d.,?!:\-"\']+')
_REF_ID_RE   = re.compile(r'[0-9a-f]{16}')
_MAX_QUESTION = 2000
_MAX_TOP_K    = 500
_MAX_TOP_N    = 500


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
from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
from indexing.embedder import ProtocolEmbedder
from indexing.parse_summary import parse_summary_bullets
from retrieval.protocol_rag import query_retrieve
from utils.meeting import register_meeting_paths, get_summary_path_from_id, get_transcript_path_from_id
from utils.speech import get_mk_speeches_in_committee
from utils.tools import dispatch as knesset_dispatch


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    import web.settings as settings

    print("[web] Loading machine …", flush=True)
    machine = StateMachine(settings.MACHINE_PATH)
    print(f"[web]   Machine: '{machine.name}' (v{machine.version})", flush=True)

    print("[web] Loading embedding model …", flush=True)
    embedder    = ProtocolEmbedder(
        model_path=settings.EMBED_MODEL_PATH,
        use_cuda=settings.CUDA,
        quantize=settings.QUANTIZE,
        batch_size=1,
    )
    embed_lock  = threading.Lock()
    from indexing.embedder import set_global_embedder
    set_global_embedder(embedder, embed_lock)

    print("[web] Loading ChromaDB …", flush=True)
    chroma = chromadb.PersistentClient(path=str(settings.CHROMA_DIR))
    try:
        bullets_count = chroma.get_collection(config.BULLETS_COLLECTION).count()
        pass2_count   = chroma.get_collection(config.PASS2_COLLECTION).count()
        pass1_count   = chroma.get_collection(config.PASS1_COLLECTION).count()
        print(
            f"[web]   bullets: {bullets_count}  pass-2: {pass2_count}  pass-1: {pass1_count}",
            flush=True,
        )
    except Exception as e:
        print(f"[web]   WARNING: could not count collections: {e}", flush=True)

    backend = GemmaLlamaBackend(url=settings.LLAMA_SERVER)

    # ── Summary tools (look up paths via global meeting registry) ────────────
    summaries_root = settings.SUMMARIES_ROOT

    def _summary_executor(name: str, args: dict) -> str:
        from utils.meeting import get_summary_path_from_id
        meeting_id = str(args.get("meeting_id", "")).strip()
        path = get_summary_path_from_id(meeting_id)
        if not path:
            return f"ישיבה '{meeting_id}' לא נמצאה. הרץ שאילתת RAG תחילה."
        if not path.exists():
            return f"קובץ הסיכום לא נמצא: {path}"
        if name == "get_meeting_summary":
            text = path.read_text(encoding="utf-8")
            return text[:6000] + ("\n…[קוצר]" if len(text) > 6000 else "")
        if name == "get_meeting_summary_section":
            section_num = int(args.get("section_num", 1))
            bullets = parse_summary_bullets(path, frozenset({section_num}))
            if bullets:
                return "\n".join(f"• {b['text']}" for b in bullets)
            all_b = parse_summary_bullets(path)
            avail = sorted({b["section"] for b in all_b})
            return f"נושא {section_num} לא נמצא. נושאים קיימים: {avail}"
        return f"כלי לא מוכר: {name}"

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
        knesset_dispatch=knesset_dispatch,
    )

    # ── Retriever closure (thread-safe embed via lock) ────────────────────────
    def _retriever(question: str, top_k: int, top_n: int) -> tuple:
        with embed_lock:
            return query_retrieve(
                question,
                chroma_client=chroma,
                embedder=embedder,
                top_k=top_k,
                top_n=top_n,
            )

    # ── Sessions dir ─────────────────────────────────────────────────────────
    from web.session import cleanup_stale_sessions
    settings.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale_sessions(settings.SESSIONS_DIR)

    # ── Store all state on app ────────────────────────────────────────────────
    app.state.machine       = machine
    app.state.backend       = backend
    app.state.retriever     = _retriever
    app.state.tool_registry = tool_registry
    app.state.chroma        = chroma
    app.state.embedder      = embedder
    app.state.embed_lock    = embed_lock
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


# ── Models ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    top_k: int | None = None
    top_n: int | None = None


class ResearchStartRequest(BaseModel):
    question: str
    top_k: int | None = None
    top_n: int | None = None


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
    settings = request.app.state.settings
    try:
        chroma = request.app.state.chroma
        counts = {
            col: chroma.get_collection(col).count()
            for col in (
                config.BULLETS_COLLECTION,
                config.PASS2_COLLECTION,
                config.PASS1_COLLECTION,
            )
        }
    except Exception:
        counts = {}
    return {
        "status":   "ok",
        "machine":  request.app.state.machine.name,
        "chroma":   str(settings.CHROMA_DIR),
        "collections": counts,
    }


@app.post("/api/query")
async def query(req: QueryRequest, request: Request):
    question = req.question.strip()
    if not question:
        return JSONResponse({"error": "שאלה ריקה"}, status_code=400)
    if not _ok_question(question):
        return JSONResponse({"error": "שאלה מכילה תווים לא חוקיים או ארוכה מדי"}, status_code=400)

    _log_query(question, request.client.host if request.client else "unknown")

    settings      = request.app.state.settings
    machine       = request.app.state.machine
    backend       = request.app.state.backend
    retriever     = request.app.state.retriever
    tool_registry = request.app.state.tool_registry

    top_k = min(req.top_k or settings.TOP_K_MEETINGS, _MAX_TOP_K)
    top_n = min(req.top_n or settings.TOP_N_DIALOGS, _MAX_TOP_N)

    async def generate():
        loop      = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def _run_sync():
            if not _RESEARCH_SEM.acquire(blocking=False):
                loop.call_soon_threadsafe(queue.put_nowait, ("queued", {}))
                _RESEARCH_SEM.acquire()
            try:
                runner = MachineRunner(
                    machine       = machine,
                    backend       = backend,
                    retriever     = retriever,
                    tool_registry = tool_registry,
                    top_k         = top_k,
                    top_n         = top_n,
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

    settings      = request.app.state.settings
    machine       = request.app.state.machine
    backend       = request.app.state.backend
    retriever     = request.app.state.retriever
    tool_registry = request.app.state.tool_registry
    sessions_dir  = request.app.state.sessions_dir

    top_k = min(req.top_k or settings.TOP_K_MEETINGS, _MAX_TOP_K)
    top_n = min(req.top_n or settings.TOP_N_DIALOGS, _MAX_TOP_N)

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
            if not _RESEARCH_SEM.acquire(blocking=False):
                loop.call_soon_threadsafe(queue.put_nowait, ("queued", {}))
                _RESEARCH_SEM.acquire()
            _final_token = ""
            _outcome: tuple | None = None  # ('done', answer) | ('error', msg) | ('user_paused',)
            try:
                runner = MachineRunner(
                    machine       = machine,
                    backend       = backend,
                    retriever     = retriever,
                    tool_registry = tool_registry,
                    top_k         = top_k,
                    top_n         = top_n,
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
                        _chk = _outcome[1]
                        _cv  = _chk.get("ctx_snapshot", {}).get("vars", {})
                        _mp  = _cv.get("meeting_paths") or {}
                        _rcbm = _cv.get("rag_chunks_by_meeting") or {}
                        _ws: dict = {}
                        if _mp:
                            register_meeting_paths(_mp)
                            _ws = {"meeting_paths": _mp, "rag_chunks_by_meeting": _rcbm,
                                   "selected_chunks": []}
                        save_session(ResearchSession(
                            session_id=session_id, status="awaiting_user",
                            original_question=question,
                            created_at=_run_ts, updated_at=_ts,
                            machine_checkpoint=_chk,
                            workspace_data=_ws or None,
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

                    # Extract meeting_paths + heatmap scores from ctx_snapshot
                    ctx_snap = checkpoint.get("ctx_snapshot", {})
                    ctx_vars = ctx_snap.get("vars", {})
                    meeting_paths_snap   = ctx_vars.get("meeting_paths") or {}
                    rag_chunks_by_mtg    = ctx_vars.get("rag_chunks_by_meeting") or {}
                    workspace_snap: dict = {}
                    if meeting_paths_snap:
                        register_meeting_paths(meeting_paths_snap)
                        workspace_snap["meeting_paths"]       = meeting_paths_snap
                        workspace_snap["rag_chunks_by_meeting"] = rag_chunks_by_mtg
                        workspace_snap.setdefault("selected_chunks", [])

                    session = ResearchSession(
                        session_id          = session_id,
                        status              = "awaiting_user",
                        original_question   = question,
                        created_at          = created_at,
                        updated_at          = _now(),
                        machine_checkpoint  = checkpoint,
                        workspace_data      = workspace_snap or None,
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

    settings      = request.app.state.settings
    machine       = request.app.state.machine
    backend       = request.app.state.backend
    retriever     = request.app.state.retriever
    tool_registry = request.app.state.tool_registry

    # Preserve the top-k/top-n that were used for the original run (best effort:
    # fall back to current defaults if not stored in checkpoint).
    checkpoint = session.machine_checkpoint or {}
    top_k = settings.TOP_K_MEETINGS
    top_n = settings.TOP_N_DIALOGS

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
                    retriever     = retriever,
                    tool_registry = tool_registry,
                    top_k         = top_k,
                    top_n         = top_n,
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
                        _chk = _outcome[1]
                        _cv  = _chk.get("ctx_snapshot", {}).get("vars", {})
                        _mp  = _cv.get("meeting_paths") or {}
                        _rcbm = _cv.get("rag_chunks_by_meeting") or {}
                        _prev = session.workspace_data or {}
                        if _mp:
                            register_meeting_paths(_mp)
                            _ws = {
                                **_prev,
                                "meeting_paths":         {**_prev.get("meeting_paths", {}), **_mp},
                                "rag_chunks_by_meeting": {**_prev.get("rag_chunks_by_meeting", {}), **_rcbm},
                            }
                            _ws.setdefault("selected_chunks", [])
                        else:
                            _ws = _prev or None
                        save_session(ResearchSession(
                            session_id=session_id, status="awaiting_user",
                            original_question=question,
                            created_at=session.created_at, updated_at=_ts,
                            machine_checkpoint=_chk,
                            workspace_data=_ws or None,
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

                    # Extract meeting_paths + heatmap scores from ctx_snapshot
                    ctx_snap2   = new_checkpoint.get("ctx_snapshot", {})
                    ctx_vars2   = ctx_snap2.get("vars", {})
                    mp2         = ctx_vars2.get("meeting_paths") or {}
                    rcbm2       = ctx_vars2.get("rag_chunks_by_meeting") or {}
                    if mp2:
                        register_meeting_paths(mp2)
                    prev_workspace = session.workspace_data or {}
                    if mp2:
                        merged_workspace: dict = {
                            **prev_workspace,
                            "meeting_paths":        {**prev_workspace.get("meeting_paths", {}), **mp2},
                            "rag_chunks_by_meeting": {**prev_workspace.get("rag_chunks_by_meeting", {}), **rcbm2},
                        }
                        merged_workspace.setdefault("selected_chunks", [])
                    else:
                        merged_workspace = prev_workspace or None

                    updated = ResearchSession(
                        session_id          = session_id,
                        status              = "awaiting_user",
                        original_question   = question,
                        created_at          = session.created_at,
                        updated_at          = _now(),
                        machine_checkpoint  = new_checkpoint,
                        workspace_data      = merged_workspace or None,
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


# ── Workspace models ──────────────────────────────────────────────────────────

class WorkspaceSelectRequest(BaseModel):
    chunk_id: str
    text: str
    source_meeting_id: str


class WorkspaceAskRequest(BaseModel):
    question: str
    meeting_id: str | None = None


# ── Workspace routes ──────────────────────────────────────────────────────────

_ATTEND_RE = __import__("re").compile(r"נוכח|נעדר|חסר")


@app.get("/api/research/{session_id}/rag")
async def research_rag(session_id: str, query: str, request: Request, top_k: int = 0):
    """
    Run 3-level RAG retrieval for a workspace query.

    Returns a ranked list of meetings with excerpt and score.
    Saves meeting_paths into session workspace_data for subsequent endpoints.
    """
    if not _ok_session_id(session_id):
        return JSONResponse({"error": "Invalid session ID"}, status_code=400)

    from web.session import load_session, save_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    query = query.strip()
    if not query:
        return JSONResponse({"error": "שאלה ריקה"}, status_code=400)
    if not _ok_question(query):
        return JSONResponse({"error": "שאלה מכילה תווים לא חוקיים או ארוכה מדי"}, status_code=400)

    settings = request.app.state.settings
    retriever = request.app.state.retriever
    top_k = min(top_k, _MAX_TOP_K) if top_k > 0 else settings.TOP_K_MEETINGS
    top_n = settings.TOP_N_DIALOGS

    loop = asyncio.get_event_loop()
    context_str, debug = await loop.run_in_executor(
        None,
        lambda: retriever(question=query, top_k=top_k, top_n=top_n),
    )

    meeting_ids: list[str] = debug["meetings"]
    meeting_scores: dict[str, float] = debug.get("meeting_scores", {})
    selected_pass1: list[dict] = debug["selected_pass1"]
    meeting_paths: dict[str, str] = debug["meeting_paths"]
    register_meeting_paths(meeting_paths)

    # Build a quick lookup: meeting_id → first pass-1 chunk meta
    meta_by_meeting: dict[str, dict] = {}
    for item in selected_pass1:
        mid = item["meta"].get("meeting_id", "")
        if mid and mid not in meta_by_meeting:
            meta_by_meeting[mid] = item["meta"]

    # Derive date/committee from pass-1 meta or fall back to filename
    def _parse_filename(path_str: str) -> tuple[str, str]:
        """Return (date, committee) from a summary .txt filename."""
        p = Path(path_str)
        # Filename: DD_MM_YYYY_<session_id>.txt  →  parts[-1] = committee dir
        name = p.stem  # e.g. "14_07_2023_12345"
        parts = name.split("_")
        if len(parts) >= 4:
            date_str = f"{parts[0]}/{parts[1]}/{parts[2]}"
        else:
            date_str = ""
        committee = p.parent.name
        return date_str, committee

    def _first_non_attendance_bullet(path_str: str) -> str:
        """Return the text of the first non-attendance bullet in a summary."""
        try:
            bullets = parse_summary_bullets(Path(path_str))
            for b in bullets:
                return b["text"]
        except Exception:
            pass
        return ""

    meetings_out = []
    for rank, mid in enumerate(meeting_ids):
        meta = meta_by_meeting.get(mid)
        if meta:
            date = meta.get("date", "")
            committee = meta.get("committee", "")
        else:
            summary_p = get_summary_path_from_id(mid)
            date, committee = _parse_filename(str(summary_p)) if summary_p else ("", "")

        summary_p = get_summary_path_from_id(mid)
        excerpt = _first_non_attendance_bullet(str(summary_p)) if summary_p else ""

        score = meeting_scores.get(mid, 0.0)

        title = f"{committee} — {date}" if committee and date else mid
        meetings_out.append({
            "meeting_id": mid,
            "date": date,
            "committee": committee,
            "title": title,
            "excerpt": excerpt,
            "score": score,
        })

    # Persist meeting_paths into workspace_data so later endpoints can use them
    from datetime import datetime, timezone
    workspace = session.workspace_data or {}
    workspace["meeting_paths"] = meeting_paths
    # Preserve selected_chunks if already set
    workspace.setdefault("selected_chunks", [])

    # Build per-meeting RAG chunk index (speech ranges + scores) for the heatmap (pass-1 only).
    rag_by_meeting: dict[str, list[dict]] = {}
    for item in debug["selected_pass1"]:
        meta = item["meta"]
        mid  = meta.get("meeting_id", "")
        if not mid:
            continue
        rag_by_meeting.setdefault(mid, []).append({
            "start": meta.get("start_speech_idx", 0),
            "end":   meta.get("end_speech_idx",   0),
            "sim":   round(float(item["p1_sim"]), 4),
            "tvec":  item.get("topic_scores_vec", []),
        })
    workspace["rag_chunks_by_meeting"] = rag_by_meeting

    from dataclasses import replace as _dc_replace
    updated = _dc_replace(
        session,
        workspace_data=workspace,
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    )
    save_session(updated, sessions_dir)

    return {"meetings": meetings_out, "query_used": query}


@app.get("/api/research/{session_id}/meeting/{meeting_id}/summary")
async def research_meeting_summary(session_id: str, meeting_id: str, request: Request):
    """
    Return the parsed summary of a retrieved meeting, grouped by topic section.
    """
    if not _ok_session_id(session_id) or not meeting_id.isdigit():
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    from web.session import load_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    register_meeting_paths((session.workspace_data or {}).get("meeting_paths", {}))
    summary_path = get_summary_path_from_id(meeting_id)
    if not summary_path:
        return JSONResponse(
            {"error": f"Meeting '{meeting_id}' not in workspace. Run /rag first."},
            status_code=404,
        )
    if not summary_path.exists():
        return JSONResponse(
            {"error": f"Summary file not found: {summary_path}"},
            status_code=404,
        )

    bullets = parse_summary_bullets(summary_path)

    # Group bullets by section; preserve global bullet_idx for heatmap reranking
    sections_map: dict[int, list[dict]] = {}
    for b in bullets:
        sections_map.setdefault(b["section"], []).append({
            "text":       b["text"],
            "bullet_idx": b["idx"],
        })

    # Re-read raw headings for proper labels
    raw_text = summary_path.read_text(encoding="utf-8")
    import re as _re
    _NUMBERED_SEC = _re.compile(r"^(?:#{1,4}\s+|\*\*\s*)(\d+)[.\s]+(.*)")
    _MD_HEADING   = _re.compile(r"^#{1,4}\s+\D(.*)")
    _STOP_PAT     = _re.compile(r"^#{1,2}\s+.*חוק", _re.UNICODE)
    headings: dict[int, str] = {}
    current_num: int | None = None
    auto_counter = 0
    for line in raw_text.split("\n"):
        stripped = line.strip()
        if _STOP_PAT.match(stripped):
            break
        m = _NUMBERED_SEC.match(stripped)
        if m:
            current_num = int(m.group(1))
            heading_text = _re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(2)).strip()
            headings[current_num] = heading_text or f"נושא {current_num}"
            continue
        m2 = _MD_HEADING.match(stripped)
        if m2:
            auto_counter = (current_num + 1) if current_num is not None else (auto_counter + 1)
            current_num = auto_counter
            heading_text = _re.sub(r"\*\*(.+?)\*\*", r"\1", m2.group(0).lstrip("#").strip())
            headings[current_num] = heading_text or f"נושא {current_num}"

    # Build topics list, skipping attendance sections
    topics = []
    for idx, sec_num in enumerate(sorted(sections_map)):
        heading = headings.get(sec_num, f"נושא {sec_num}")
        if _ATTEND_RE.search(heading):
            continue
        topics.append({
            "index": idx,
            "heading": heading,
            "bullets": sections_map[sec_num],
        })

    # Derive date and committee from filename
    name = summary_path.stem
    parts = name.split("_")
    date = f"{parts[0]}/{parts[1]}/{parts[2]}" if len(parts) >= 4 else ""
    committee = summary_path.parent.name
    title = f"{committee} — {date}" if committee and date else meeting_id

    return {
        "meeting_id": meeting_id,
        "date": date,
        "committee": committee,
        "title": title,
        "topics": topics,
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

    register_meeting_paths((session.workspace_data or {}).get("meeting_paths", {}))
    transcript_path = get_transcript_path_from_id(meeting_id)
    if not transcript_path:
        return JSONResponse(
            {"error": f"Meeting '{meeting_id}' not in workspace. Run /rag first."},
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


@app.get("/api/research/{session_id}/meeting/{meeting_id}/pass2_chunks")
async def research_meeting_pass2_chunks(session_id: str, meeting_id: str, request: Request):
    """Return pass-2 chunk metadata for a meeting (no scoring — fast)."""
    if not _ok_session_id(session_id) or not meeting_id.isdigit():
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)

    import config as _cfg
    chroma = request.app.state.chroma
    try:
        coll = chroma.get_collection(_cfg.PASS2_COLLECTION)
        rows = coll.get(
            where={"meeting_id": {"$eq": meeting_id}},
            include=["metadatas"],
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    chunks = [
        {
            "chunk_id": cid,
            "start":    meta.get("start_speech_idx", 0),
            "end":      meta.get("end_speech_idx",   0),
            "chars":    meta.get("char_count",        0),
        }
        for cid, meta in zip(rows["ids"], rows["metadatas"])
    ]
    return {"count": len(chunks), "chunks": chunks}


class ScorePass2Request(BaseModel):
    query: str


@app.post("/api/research/{session_id}/meeting/{meeting_id}/score_pass2")
async def research_meeting_score_pass2(
    session_id: str, meeting_id: str, req: ScorePass2Request, request: Request
):
    """Embed query and cosine-score all pass-2 chunks for a meeting."""
    if not _ok_session_id(session_id) or not meeting_id.isdigit():
        return JSONResponse({"error": "Invalid parameters"}, status_code=400)
    if not _ok_question(req.query.strip()):
        return JSONResponse({"error": "שאלה מכילה תווים לא חוקיים או ארוכה מדי"}, status_code=400)

    import json as _json
    import numpy as _np
    import config as _cfg
    from indexing.embedder import ProtocolEmbedder

    chroma     = request.app.state.chroma
    embedder   = request.app.state.embedder
    embed_lock = request.app.state.embed_lock

    # Fetch all pass-2 chunks (with stored embeddings) for this meeting
    try:
        coll = chroma.get_collection(_cfg.PASS2_COLLECTION)
        rows = coll.get(
            where={"meeting_id": {"$eq": meeting_id}},
            include=["embeddings", "metadatas"],
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    if not rows["ids"]:
        return {"chunks": []}  # meeting not indexed in pass2 — grey heatmap is correct

    # Embed query (thread-safe)
    loop = asyncio.get_event_loop()
    def _embed():
        with embed_lock:
            return embedder.embed([req.query], ProtocolEmbedder.INSTR_QUERY)

    q_emb = await loop.run_in_executor(None, _embed)
    q_vec = q_emb[0]
    q_norm = q_vec / (_np.linalg.norm(q_vec) + 1e-9)

    # Cosine similarity per chunk
    chunks_out = []
    for emb, meta in zip(rows["embeddings"], rows["metadatas"]):
        e = _np.array(emb, dtype=_np.float32)
        e_norm = e / (_np.linalg.norm(e) + 1e-9)
        score = float(_np.dot(q_norm, e_norm))
        tvec  = _json.loads(meta.get("topic_scores_vec", "[]"))
        chunks_out.append({
            "start": meta.get("start_speech_idx", 0),
            "end":   meta.get("end_speech_idx",   0),
            "score": round(score, 4),
            "tvec":  tvec,
        })

    return {"chunks": chunks_out}


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

    register_meeting_paths((session.workspace_data or {}).get("meeting_paths", {}))
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
    workspace.setdefault("meeting_paths", {})
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
    register_meeting_paths(workspace.get("meeting_paths", {}))
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
        summary_path = get_summary_path_from_id(req.meeting_id)
        if summary_path and summary_path.exists():
            try:
                bullets = parse_summary_bullets(summary_path)
                summary_lines = [b["text"] for b in bullets[:30]]
                summary_block = "סיכום ישיבה:\n" + "\n".join(f"• {l}" for l in summary_lines)
                if used_chars + len(summary_block) <= MAX_CTX_CHARS:
                    context_parts.insert(0, summary_block)
            except Exception:
                pass

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
