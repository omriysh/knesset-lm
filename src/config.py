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

CACHE_DB  = DATA_DIR / "knesset_api_cache"
CACHE_TTL = 7 * 24 * 3600   # 1 week (seconds)

# ── LLM ──────────────────────────────────────────────────────────────────────

LLAMA_SERVER        = "http://127.0.0.1:8080"
CTX_SIZE            = 40000
MAX_TOKENS          = 16384
MAX_THINKING_TOKENS = 10000
CHARS_PER_TOK       = 2      # rough estimate for Hebrew

# Chunk sizing: reserve space for system prompt, partial summary, and response
_ESTIMATED_SUMMARY_TOKENS = 2048
_RESERVED_TOKENS = 2048 + MAX_TOKENS + _ESTIMATED_SUMMARY_TOKENS
MAX_CHUNK_CHARS  = (CTX_SIZE - _RESERVED_TOKENS) * CHARS_PER_TOK

# Meetings that would require more chunks than this are skipped (likely non-protocol documents)
MAX_SUMMARIZATION_CHUNKS = 10

# ── Data paths ────────────────────────────────────────────────────────────────

def transcriptions_dir(knesset_num: int = 25) -> Path:
    """Root directory for raw protocol files for a given Knesset."""
    return DATA_DIR / "raw_transcriptions" / str(knesset_num)


def summaries_dir(knesset_num: int = 25) -> Path:
    """Root directory for generated summary files for a given Knesset."""
    return DATA_DIR / "summaries" / str(knesset_num)
