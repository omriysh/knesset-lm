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
import sys
import threading
import traceback
import uuid
import ftfy
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
from agent.machine import StateMachine
from agent.runner import MachineRunner, build_tool_registry
from indexing.embedder import ProtocolEmbedder
from indexing.parse_summary import parse_summary_bullets
from retrieval.protocol_rag import query_retrieve
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

    # ── Summary tools (need meeting_paths injected per-request) ──────────────
    summaries_root = settings.SUMMARIES_ROOT

    def _summary_executor(name: str, args: dict, meeting_paths: dict) -> str:
        meeting_id = str(args.get("meeting_id", "")).strip()
        path_str   = meeting_paths.get(meeting_id)
        if not path_str:
            available = ", ".join(list(meeting_paths)[:6]) or "אין"
            return f"ישיבה '{meeting_id}' לא נמצאה.\nמזהים זמינים: {available}"
        path = Path(path_str)
        if not path.exists():
            return f"קובץ הסיכום לא נמצא: {path_str}"
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
_QUERY_LOG = Path(__file__).parent / "query_log.jsonl"

def _log_query(question: str, ip: str) -> None:
    entry = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ip": ip,
        "q":  question,
    }, ensure_ascii=False)
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
        return {"error": "שאלה ריקה"}, 400

    _log_query(question, request.client.host if request.client else "unknown")

    settings      = request.app.state.settings
    machine       = request.app.state.machine
    backend       = request.app.state.backend
    retriever     = request.app.state.retriever
    tool_registry = request.app.state.tool_registry

    top_k = req.top_k or settings.TOP_K_MEETINGS
    top_n = req.top_n or settings.TOP_N_DIALOGS

    async def generate():
        loop      = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def _run_sync():
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

    settings      = request.app.state.settings
    machine       = request.app.state.machine
    backend       = request.app.state.backend
    retriever     = request.app.state.retriever
    tool_registry = request.app.state.tool_registry
    sessions_dir  = request.app.state.sessions_dir

    top_k = req.top_k or settings.TOP_K_MEETINGS
    top_n = req.top_n or settings.TOP_N_DIALOGS

    session_id = str(uuid.uuid4())

    async def generate():
        from datetime import datetime, timezone

        def _now():
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

        # First event: session_id
        yield _sse("session_id", {"session_id": session_id})

        loop: asyncio.AbstractEventLoop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        def _run_sync():
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
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        thread = threading.Thread(target=_run_sync, daemon=True)
        thread.start()

        created_at  = _now()
        final_token = ""

        try:
            while True:
                item = await queue.get()
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

        elif session.status == "done":
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

        def _run_sync():
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
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as exc:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("error", str(exc) + "\n" + traceback.format_exc()),
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        thread = threading.Thread(target=_run_sync, daemon=True)
        thread.start()

        final_token = ""

        try:
            while True:
                item = await queue.get()
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

                elif ev_type == "user_input_required":
                    new_checkpoint = ev_data.get("checkpoint", {})

                    # Extract meeting_paths + heatmap scores from ctx_snapshot
                    ctx_snap2   = new_checkpoint.get("ctx_snapshot", {})
                    ctx_vars2   = ctx_snap2.get("vars", {})
                    mp2         = ctx_vars2.get("meeting_paths") or {}
                    rcbm2       = ctx_vars2.get("rag_chunks_by_meeting") or {}
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
    from web.session import delete_session

    sessions_dir = request.app.state.sessions_dir
    delete_session(session_id, sessions_dir)
    # Return 204 regardless of whether the file existed (idempotent delete)
    from fastapi.responses import Response
    return Response(status_code=204)


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
    from web.session import load_session, save_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    query = query.strip()
    if not query:
        return JSONResponse({"error": "שאלה ריקה"}, status_code=400)

    settings = request.app.state.settings
    retriever = request.app.state.retriever
    top_k = top_k if top_k > 0 else settings.TOP_K_MEETINGS
    top_n = settings.TOP_N_DIALOGS

    loop = asyncio.get_event_loop()
    context_str, debug = await loop.run_in_executor(
        None,
        lambda: retriever(question=query, top_k=top_k, top_n=top_n),
    )

    meeting_ids: list[str] = debug["meetings"]
    selected_pass1: list[dict] = debug["selected_pass1"]
    meeting_paths: dict[str, str] = debug["meeting_paths"]

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
            path_str = meeting_paths.get(mid, "")
            date, committee = _parse_filename(path_str) if path_str else ("", "")

        path_str = meeting_paths.get(mid, "")
        excerpt = _first_non_attendance_bullet(path_str) if path_str else ""

        # Score: rank-based, 1.0 at rank 0, decrement 0.05 per rank, floor 0.5
        score = max(0.5, 1.0 - rank * 0.05)

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
    from web.session import load_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    workspace = session.workspace_data or {}
    meeting_paths: dict[str, str] = workspace.get("meeting_paths", {})
    path_str = meeting_paths.get(meeting_id)
    if not path_str:
        return JSONResponse(
            {"error": f"Meeting '{meeting_id}' not in workspace. Run /rag first."},
            status_code=404,
        )

    summary_path = Path(path_str)
    if not summary_path.exists():
        return JSONResponse(
            {"error": f"Summary file not found: {path_str}"},
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
    from web.session import load_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    workspace = session.workspace_data or {}
    meeting_paths: dict[str, str] = workspace.get("meeting_paths", {})
    path_str = meeting_paths.get(meeting_id)
    if not path_str:
        return JSONResponse(
            {"error": f"Meeting '{meeting_id}' not in workspace. Run /rag first."},
            status_code=404,
        )

    # Derive transcription path from summary path
    transcript_path = Path(
        path_str
        .replace("summaries", "raw_transcriptions", 1)
        .replace(".txt", ".json")
    )
    if not transcript_path.exists():
        return JSONResponse(
            {"error": f"Transcript file not found: {transcript_path}"},
            status_code=404,
        )

    import json as _json
    meeting = _json.loads(transcript_path.read_text(encoding="utf-8"))

    # Derive date and committee from filename
    name = transcript_path.stem
    parts = name.split("_")
    date = f"{parts[0]}/{parts[1]}/{parts[2]}" if len(parts) >= 4 else ""
    committee = transcript_path.parent.name
    title = f"{committee} — {date}" if committee and date else meeting_id

    chunks = []
    if "speeches" in meeting:
        for idx, speech in enumerate(meeting["speeches"]):
            speaker = speech.get("speaker", "").strip()
            text    = speech.get("text_he", "").strip()
            if not text and not speaker:
                continue
            chunks.append({
                "chunk_id": str(idx),
                "speaker":  speaker,
                "text":     ftfy.fix_text(text),
            })
    else:
        from utils.meeting import parse_full_text_speeches
        full_text = meeting.get("full_text", "")
        parsed = parse_full_text_speeches(full_text)

        if parsed:
            for idx, speech in enumerate(parsed):
                speaker = speech.get("speaker", "").strip()
                text    = speech.get("text_he", "").strip()
                if text:
                    chunks.append({
                        "chunk_id": str(idx),
                        "speaker":  speaker,
                        "text":     text,
                    })
        else:
            paragraphs = [p.strip() for p in full_text.split("\n\n") if p.strip()]
            if not paragraphs:
                paragraphs = [p.strip() for p in full_text.split("\n") if p.strip()]
            for idx, para in enumerate(paragraphs):
                chunks.append({
                    "chunk_id": str(idx),
                    "speaker":  "",
                    "text":     para,
                })

    return {
        "meeting_id": meeting_id,
        "date":       date,
        "committee":  committee,
        "chunks":     chunks,
    }


@app.get("/api/research/{session_id}/meeting/{meeting_id}/pass2_chunks")
async def research_meeting_pass2_chunks(session_id: str, meeting_id: str, request: Request):
    """Return pass-2 chunk metadata for a meeting (no scoring — fast)."""
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
    from web.session import load_session

    sessions_dir = request.app.state.sessions_dir
    session = load_session(session_id, sessions_dir)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    workspace = session.workspace_data or {}
    meeting_paths: dict[str, str] = workspace.get("meeting_paths", {})
    path_str = meeting_paths.get(meeting_id)
    if not path_str:
        return JSONResponse({"participants": []})

    transcript_path = Path(
        path_str
        .replace("summaries", "raw_transcriptions", 1)
        .replace(".txt", ".json")
    )
    if not transcript_path.exists():
        return JSONResponse({"participants": []})

    import json as _json
    meeting = _json.loads(transcript_path.read_text(encoding="utf-8"))

    from utils.meeting import extract_attendance
    participants = extract_attendance(meeting)

    return {"meeting_id": meeting_id, "participants": participants}


@app.post("/api/research/{session_id}/workspace/select")
async def workspace_select(
    session_id: str,
    req: WorkspaceSelectRequest,
    request: Request,
):
    """Append a transcript chunk to the session workspace for later querying."""
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

    workspace       = session.workspace_data or {}
    selected_chunks: list[dict] = workspace.get("selected_chunks", [])
    meeting_paths: dict[str, str] = workspace.get("meeting_paths", {})

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
        summary_path_str = meeting_paths.get(req.meeting_id)
        if summary_path_str:
            summary_path = Path(summary_path_str)
            if summary_path.exists():
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
