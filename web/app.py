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
from contextlib import asynccontextmanager
from pathlib import Path

# Bootstrap sys.path before importing knesset-lm modules
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import chromadb
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
from agent.llm.qwen3 import Qwen3LlamaBackend  # swap for GemmaLlamaBackend if using Gemma
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

    backend = Qwen3LlamaBackend(url=settings.LLAMA_SERVER)

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

    # ── Store all state on app ────────────────────────────────────────────────
    app.state.machine       = machine
    app.state.backend       = backend
    app.state.retriever     = _retriever
    app.state.tool_registry = tool_registry
    app.state.chroma        = chroma
    app.state.settings      = settings

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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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


# ── SSE helper ────────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
