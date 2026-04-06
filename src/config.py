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
MAX_THINKING_TOKENS = 10000
CHARS_PER_TOK       = 2      # rough estimate for Hebrew

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

# ── Data paths ────────────────────────────────────────────────────────────────

def transcriptions_dir(knesset_num: int = 25) -> Path:
    """Root directory for raw protocol files for a given Knesset."""
    return DATA_DIR / "raw_transcriptions" / str(knesset_num)


def summaries_dir(knesset_num: int = 25) -> Path:
    """Root directory for generated summary files for a given Knesset."""
    return DATA_DIR / "summaries" / str(knesset_num)
