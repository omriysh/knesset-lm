"""
config.py

All project-wide constants and path helpers.
"""

from pathlib import Path

_SRC_DIR = Path(__file__).parent
DATA_DIR  = _SRC_DIR.parent.parent / "Data"   # KnessetLM/Data/

# ── External APIs ─────────────────────────────────────────────────────────────

OKNESSET_API             = "https://backend.oknesset.org"
OFFICIAL_KNESSET_NEW_API = "https://knesset.gov.il/OdataV4/ParliamentInfo"
API_TIMEOUT = 30

# ── HTTP cache ────────────────────────────────────────────────────────────────

CACHE_DB      = DATA_DIR / "knesset_api_cache"
MK_PHOTOS_DIR = DATA_DIR / "mk_photos"
CACHE_TTL = 7 * 24 * 3600   # 1 week (seconds)

# ── LLM ──────────────────────────────────────────────────────────────────────

LLAMA_SERVER        = "http://127.0.0.1:8080"
CTX_SIZE            = 40000
MAX_TOKENS          = 16384
MAX_THINKING_TOKENS = 6000
CHARS_PER_TOK       = 2      # rough estimate for Hebrew

API_RETRY_ATTEMPTS  = 5      # number of attempts for external API calls
API_RETRY_SLEEP     = 30     # seconds between retries

NOT_PROTOCOL        = "לא פרוטוקול"   # sentinel the summarization prompts return for non-protocol documents

# ── Indexing ──────────────────────────────────────────────────────────────────

MIN_SPEECH_CHARS = 50    # speeches shorter than this are not stored in knesset.db

# ── Web reading tab ───────────────────────────────────────────────────────────

TOP_K_BROWSE = 50        # meetings per reading-tab search

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
RESEARCH_MAX_REPLANS            = 2
RESEARCH_MAX_PLAN_STEPS_V1      = 8
RESEARCH_MAX_DEEP_DIVES_PER_PLAN = 3       # validator caps plan deep-dives
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

# BM25 / morphology
KNESSET_DB           = DATA_DIR / "knesset.db"   # built by scripts/build_knesset_db.py
USE_DICTABERT_LEMMA  = False
DICTABERT_MODEL      = "dicta-il/dictabert-seg"
DICTABERT_DEVICE     = "cuda"   # used only when USE_DICTABERT_LEMMA=True

# Retrieval
QUERY_PROTOCOLS_DEFAULT_TOP_K      = 50     # rows per scope (topics / opinions / speeches)
QUERY_PROTOCOLS_MAX_TOP_K          = 200
NAME_RESOLUTION_AUTO_THRESHOLD     = 0.35
FUZZY_SEARCH_THRESHOLD             = 55.0   # minimum RapidFuzz score (0–100) to include a candidate
FUZZY_BODY_SCORE_WEIGHT            = 0.85   # body match weighted lower than label match
# Score given when query and label differ only by an interior middle name
# and agree on both first and last token ("אביחי בוארון" vs "אביחי אברהם
# בוארון"). WRatio puts those at 85, below PARTICIPANT_FUZZY_THRESHOLD.
FUZZY_TOKEN_CONTAINMENT_SCORE      = 95.0

# Stricter bar for meeting-participant/guest MK resolution (speaker/roster
# names -> mk_id, in web/app.py::browse_search).
# At the general-purpose FUZZY_SEARCH_THRESHOLD=55, real non-MK names that
# happen to share one name token with an MK false-positive up to ~85
# (e.g. "עודד ברוק" -> MK "עודד פורר", "שי טייב" -> MK "יוסף טייב") — while
# true matches (the actual MK's name, verbatim as transcribed) always score
# 100. 90 sits cleanly between the two with margin on both sides. A false
# negative here just falls through to the guest bucket (still filterable,
# just via guest_name instead of mk_id) — a much softer failure than
# attributing a meeting to the wrong MK, so trading recall for precision
# is the right call specifically for this use case.
PARTICIPANT_FUZZY_THRESHOLD        = 90.0

# Bill text
BILL_TEXT_DEFAULT_MAX_CHARS  = 1000
BILL_TEXT_MIN_MAX_CHARS      = 200
BILL_TEXT_MAX_MAX_CHARS      = 8000

# Tool result truncation
# Max chars of `full` text sent to the executor LLM per tool result message.
EXECUTOR_TOOL_RESULT_CHARS   = 4000
# Max chars of `full` text included in the step_completed SSE event payload.
AGENT_STEP_FULL_CHARS        = 8000

# Sessions on disk (evidence overflow)
SESSIONS_DIR = DATA_DIR / "sessions"
