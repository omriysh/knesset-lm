"""
config.py

All project-wide constants and path helpers.
"""

import os
from pathlib import Path

_SRC_DIR = Path(__file__).parent
DATA_DIR  = _SRC_DIR.parent.parent / "Data"   # KnessetLM/Data/

# ── External APIs ─────────────────────────────────────────────────────────────

OKNESSET_API             = "https://backend.oknesset.org"
OFFICIAL_KNESSET_NEW_API = "https://knesset.gov.il/OdataV4/ParliamentInfo"
API_TIMEOUT = 30

# ── HTTP cache ────────────────────────────────────────────────────────────────

CACHE_DB  = DATA_DIR / "knesset_api_cache"
CACHE_TTL = 7 * 24 * 3600   # 1 week (seconds)

# ── LLM ──────────────────────────────────────────────────────────────────────

LLAMA_SERVER        = "http://127.0.0.1:8080"
CTX_SIZE            = 40000
MAX_TOKENS          = 16384
MAX_THINKING_TOKENS = 6000
CHARS_PER_TOK       = 2      # rough estimate for Hebrew

API_RETRY_ATTEMPTS  = 5      # number of attempts for external API calls
API_RETRY_SLEEP     = 30     # seconds between retries

NOT_PROTOCOL        = "לא פרוטוקול"   # sentinel returned by summarize_meeting when LLM detects non-protocol

# Chunk sizing: reserve space for system prompt, partial summary, and response
_ESTIMATED_SUMMARY_TOKENS = 2048
_RESERVED_TOKENS = 2048 + MAX_TOKENS + _ESTIMATED_SUMMARY_TOKENS
MAX_CHUNK_CHARS  = (CTX_SIZE - _RESERVED_TOKENS) * CHARS_PER_TOK

# Meetings that would require more chunks than this are skipped (likely non-protocol documents)
MAX_SUMMARIZATION_CHUNKS = 10

# ── Embedding model ───────────────────────────────────────────────────────────
# Override with environment variables for non-standard installs.

EMBED_MODEL_PATH = os.environ.get(
    "KNESSET_EMBED_MODEL",
    str(Path.home() / "Downloads/llama.cpp/unsloth/Qwen3-VL-Embedding-8B"),
)

# Short slug used to namespace the ChromaDB directory so embeddings from
# different models are never mixed.  Derived from the model directory name
# by default; override with KNESSET_EMBED_MODEL_NAME if needed.
EMBED_MODEL_NAME = os.environ.get(
    "KNESSET_EMBED_MODEL_NAME",
    Path(EMBED_MODEL_PATH).name.lower().replace("_", "-"),
)

# ── Indexing parameters ───────────────────────────────────────────────────────

EMBED_BATCH_SIZE          = 4     # reduce to 1–2 if OOM during indexing
MIN_SPEECH_CHARS          = 50    # speeches shorter than this are skipped
COHERENCE_WINDOW          = 3     # speeches on each side for block similarity
COHERENCE_DEPTH_THRESHOLD = 0.02  # min valley depth to count as a topic boundary
COHERENCE_PEAK_WINDOW     = 8     # look-ahead/behind window for reference peak
MIN_DIALOG_SPEECHES       = 2     # groups smaller than this are merged into a neighbour
MAX_DIALOG_CHARS          = 3000  # oversized chunks are split at the deepest valley

# ── ChromaDB ─────────────────────────────────────────────────────────────────
# Embeddings from different models are stored in separate subdirectories under
# CHROMA_ROOT so they are never mixed.  The active model's directory is CHROMA_DIR.
# Use --db <path> at the CLI to override the full path (e.g. for an experimental
# store that pre-dates the per-model layout).

CHROMA_ROOT = DATA_DIR / "chroma"
CHROMA_DIR  = CHROMA_ROOT / EMBED_MODEL_NAME

SPEECHES_COLLECTION = "knesset_speeches"
BULLETS_COLLECTION  = "knesset_bullets"
PASS1_COLLECTION    = "knesset_dialogs_pass1"
PASS2_COLLECTION    = "knesset_dialogs_pass2"

# ── RAG retrieval parameters ──────────────────────────────────────────────────

TOP_K_MEETINGS    = 5      # meetings to surface via L1 bullet search
TOP_N_DIALOGS     = 15     # pass-2 chunks to rank per query
MAX_CONTEXT_CHARS = 50_000 # ~25k tokens; leaves headroom for LLM output

# ── Data paths ────────────────────────────────────────────────────────────────

def transcriptions_dir(knesset_num: int = 25) -> Path:
    """Root directory for raw protocol files for a given Knesset."""
    return DATA_DIR / "raw_transcriptions" / str(knesset_num)


def summaries_dir(knesset_num: int = 25) -> Path:
    """Root directory for generated summary files for a given Knesset."""
    return DATA_DIR / "summaries" / str(knesset_num)


# ── Plan-execute agent ───────────────────────────────────────────────────────

# Models (cloud)
GOOGLE_API_KEY_ENV   = "GOOGLE_API_KEY"
PLANNER_MODEL        = "gemini-flash-latest"
CRITIC_PRE_MODEL     = "gemini-2.5-flash-lite"
CRITIC_POST_MODEL    = "gemini-2.5-flash-lite"
SYNTHESIZER_MODEL    = "gemini-flash-latest"
EXECUTOR_MODEL_LIGHT = "gemini-2.5-flash-lite"
EXECUTOR_MODEL_HEAVY = "gemini-2.5-flash-lite"
INTENT_MODEL         = "local"            # always llama-server

# Fallback
GOOGLE_API_FALLBACK_TO_LOCAL = True

# Caps (hit-cap = abort)
RESEARCH_MAX_LLM_TOKENS         = 1_000_000
RESEARCH_MAX_TOOL_CALLS         = 50
RESEARCH_MAX_REPLANS            = 3
RESEARCH_MAX_PLAN_STEPS_V1      = 8
RESEARCH_MAX_DEEP_DIVES_PER_PLAN = 3       # validator caps plan deep-dives
DEEP_DIVE_CALLS_PER_STEP        = 2        # kept for backward compat
DEEP_DIVE_FULL_MODEL            = "gemini-2.5-flash-lite"
DEEP_DIVE_FULL_BATCH_HEADROOM   = 0.60    # fraction of ctx used for input; rest = output budget
MAX_TOOL_CALLS_PER_STEP         = 20       # max tool calls per executor step
EVIDENCE_MAX_ENTRIES            = 200
EVIDENCE_MAX_BYTES_PER_STEP     = 500 * 1024
EVIDENCE_MAX_BYTES_TOTAL        = 8 * 1024 * 1024

# Cost heuristic (Python, not LLM — see §4.1.1)
COST_HINT_SECONDS = {"cheap": 5, "medium": 30, "expensive": 120}

# Timing
RESEARCH_LONG_LATENCY_THRESHOLD_SECONDS = 600   # cost-gate trigger
RESEARCH_PER_STEP_TIMEOUT_SECONDS       = 300
RESEARCH_PER_TOOL_TIMEOUT_SECONDS       = 90

# Concurrency
RESEARCH_DAG_MAX_WORKERS         = 4
RESEARCH_DEEP_DIVE_MAX_PARALLEL  = 1

# BM25 / morphology
BM25_DIR             = DATA_DIR / "bm25"
USE_DICTABERT_LEMMA  = False
DICTABERT_MODEL      = "dicta-il/dictabert-seg"
DICTABERT_DEVICE     = "cuda"   # used only when USE_DICTABERT_LEMMA=True

# Retrieval
RRF_K                              = 60
SEARCH_TOPICS_DEFAULT_TOP_K        = 500
SEARCH_TOPICS_MAX_TOP_K            = 2000
SEARCH_PROTOCOLS_DEFAULT_TOP_K     = 50
SEARCH_PROTOCOLS_MAX_TOP_K         = 200
HYBRID_FIRST_STAGE_TOP_K           = 1000   # per-signal cap before RRF
KEYWORD_RERANK_TOP_K               = 200    # cosine rerank window when sort=relevance
NAME_RESOLUTION_AUTO_THRESHOLD     = 0.35
FUZZY_SEARCH_THRESHOLD             = 55.0   # minimum RapidFuzz score (0–100) to include a candidate
FUZZY_BODY_SCORE_WEIGHT            = 0.85   # body match weighted lower than label match

# Bill text
BILL_TEXT_DEFAULT_MAX_CHARS  = 1000
BILL_TEXT_MIN_MAX_CHARS      = 200
BILL_TEXT_MAX_MAX_CHARS      = 8000

# Tool result truncation
# Max chars of `full` text sent to the executor LLM per tool result message.
EXECUTOR_TOOL_RESULT_CHARS   = 4000
# Max chars of `full` text included in the step_completed SSE event payload.
AGENT_STEP_FULL_CHARS        = 8000

# Embedding device for query path. Flip to "cpu" when the local model
# running on llama-server is large enough to leave no VRAM headroom.
EMBED_DEVICE_FOR_QUERY = "cuda"

# Sessions on disk (evidence overflow)
SESSIONS_DIR = DATA_DIR / "sessions"
