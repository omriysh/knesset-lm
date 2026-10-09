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
CACHE_TTL_VOTES = 24 * 3600

# ── LLM ──────────────────────────────────────────────────────────────────────

LLAMA_SERVER        = "http://127.0.0.1:8080"
CTX_SIZE            = 40000
MAX_TOKENS          = 16384
MAX_THINKING_TOKENS = 6000
SYNTHESIZER_MAX_TOKENS = 2 ** 15   # the sourced answer + its citations' quotes, plus thinking (counts against it)
CHARS_PER_TOK       = 2      # rough estimate for Hebrew

API_RETRY_ATTEMPTS  = 5      # number of attempts for external API calls
API_RETRY_SLEEP     = 30     # seconds between retries
HTTP_TIMEOUT_SECONDS = 60

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


def summary_batch_state_path(knesset_num: int = 25) -> Path:
    """Resume state of scripts/summarize_knesset_batches.py."""
    return DATA_DIR / "summary_batches" / f"batch_state_k{knesset_num}.json"


def mk_themes_dir(knesset_num: int = 25) -> Path:
    """Per-MK theme files (<mk_id>.json) written by scripts/summarize_mk_themes_batches.py."""
    return DATA_DIR / "mk_themes" / str(knesset_num)


def candidate_lists_dir(election_knesset_num: int = 26) -> Path:
    """Candidate lists of an election (lists.json, ballots/, photos/), built by scripts/build_candidate_lists.py."""
    return DATA_DIR / "candidates" / str(election_knesset_num)


def mk_themes_batch_state_path(knesset_num: int = 25) -> Path:
    """Resume state of scripts/summarize_mk_themes_batches.py."""
    return DATA_DIR / "summary_batches" / f"mk_themes_state_k{knesset_num}.json"


# ── Plenum protocols ──────────────────────────────────────────────────────────
# Plenum sessions are stored as meetings of one pseudo-committee, next to the committees.

PLENUM_COMMITTEE_NAME    = "מליאת הכנסת"
PLENUM_COMMITTEE_ID      = "plenum"
PLENUM_MEETING_ID_PREFIX = "p"     # plenum meeting_id = "p" + KNS_PlenumSession.Id
PLENUM_PROTOCOL_DOCUMENT_GROUP_TYPE_ID = 28   # KNS_DocumentPlenumSession group "דברי הכנסת"
COMMITTEE_PROTOCOL_DOCUMENT_GROUP_TYPE_ID = 23   # KNS_DocumentCommitteeSession group "פרוטוקול ועדה"
WORD_EXTRACTION_TIMEOUT_SECONDS = 120   # per .doc; the worker and its own Word instance are killed after this

# ── Summarization (scripts/summarize_knesset_batches.py) ─────────────────────
# The opinions pass saturates the output cap (~150 opinions) on long transcripts, so a transcript
# longer than SUMMARY_OPINIONS_CHUNK_THRESHOLD_CHARS gets one opinions request per chunk of about
# SUMMARY_OPINIONS_CHUNK_TARGET_CHARS, cut at speaker turns. The topics pass sees it whole unless it is
# longer than the model context (scripts/summarize_knesset_batches.py MAX_TRANSCRIPT_CHARS).
SUMMARY_OPINIONS_CHUNK_THRESHOLD_CHARS = 150_000
SUMMARY_OPINIONS_CHUNK_TARGET_CHARS    = 120_000

# ── MK themes (scripts/summarize_mk_themes_batches.py) ───────────────────────
# One request per MK holding all their opinions; the largest MK (~4,800 opinions) is ~300K input tokens.
MK_THEMES_MODEL             = "gemini-3.8-flash"
MK_THEMES_THINKING_LEVEL    = "low"
MK_THEMES_MAX_OUTPUT_TOKENS = 100_000


# ── Plan-execute agent ───────────────────────────────────────────────────────

# Models (cloud)
GOOGLE_API_KEY_ENV   = "GOOGLE_API_KEY"
# Defaults for runs without a web visitor (agent.model_choice.ResearchModels.from_config); the web visitor picks
# each model in the settings (web/static/models.js holds the browser's defaults).
PLANNER_MODEL        = "gemini-flash-latest"
CRITIC_MODEL         = "gemini-2.5-flash-lite"
SYNTHESIZER_MODEL    = "gemini-flash-latest"
EXECUTOR_MODEL       = "gemini-2.5-flash-lite"
INTENT_MODEL         = "gemini-2.5-flash-lite"   # outer machine's intent classification, plan validator
ANSWER_EDITOR_MODEL  = "gemini-2.5-flash-lite"   # outer machine's final formatting of the answer

# Fallback
GOOGLE_API_FALLBACK_TO_LOCAL = False

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
FUZZY_BODY_SCORE_WEIGHT            = 0.7    # body (aliases) match weighted lower than label match
# find_mk / mk name resolution: a name sharing only one token with the query ("יאיר גולן" vs "יאיר
# לפיד") is capped at this score. Queries of up to 2 tokens need every token matched, longer ones all but one.
FUZZY_PARTIAL_NAME_MAX_SCORE       = 60.0
FUZZY_NAME_TOKEN_MATCH_MIN_RATIO   = 80.0   # "מרב" ~ "מירב" match, "יאיר" ~ "מאיר" does not
FIND_MK_CONFIDENT_SCORE            = 0.75   # below it find_mk adds a "no MK named X" hint
MK_NAME_FILTER_MIN_SCORE           = 0.85   # query_protocols mk_id given as a name
NAME_FILTER_UNAMBIGUOUS_GAP        = 0.1    # top fuzzy candidate must lead the runner-up by this much
PARTY_MATCH_MIN_SCORE              = 0.8    # find_party / party filter fuzzy cutoff: typos only; "דגל התורה" (0.7) must not become "יהדות התורה"
MAX_COMMITTEE_NAME_CHARS           = 250    # joint-committee names in meetings run to ~190 characters
FILTER_DIAGNOSTICS_ROW_COUNT_CAP   = 1000

# Party filter aliases → the exact mks.party string of Knesset 25: only abbreviations, spelling
# variants and transliterations of the faction's own name (no renames, mergers or component parties). Keys are compared after
# filter_resolution.normalized_party_key (quotes and dashes dropped, case folded), so 'רע"ם' covers 'רעם'.
PARTY_ALIASES = {
    'ש"ס':                  'התאחדות הספרדים שומרי תורה תנועתו של מרן הרב עובדיה יוסף זצ"ל',
    "shas":                 'התאחדות הספרדים שומרי תורה תנועתו של מרן הרב עובדיה יוסף זצ"ל',
    "ליכוד":                "הליכוד",
    "likud":                "הליכוד",
    "the likud":            "הליכוד",
    "יש עתיד":              "יש עתיד",
    "yesh atid":            "יש עתיד",
    "עוצמה":                "עוצמה יהודית בראשות איתמר בן גביר",
    "עוצמה יהודית":         "עוצמה יהודית בראשות איתמר בן גביר",
    "otzma yehudit":        "עוצמה יהודית בראשות איתמר בן גביר",
    "הציונות הדתית":        "הציונות הדתית בראשות בצלאל סמוטריץ'",
    "ציונות דתית":          "הציונות הדתית בראשות בצלאל סמוטריץ'",
    "religious zionism":    "הציונות הדתית בראשות בצלאל סמוטריץ'",
    "כחול לבן":             "כחול לבן - המחנה הממלכתי",
    "blue and white":       "כחול לבן - המחנה הממלכתי",
    "ישראל ביתנו":          "ישראל ביתנו",
    "yisrael beiteinu":     "ישראל ביתנו",
    "yisrael beytenu":      "ישראל ביתנו",
    "עבודה":                "העבודה",
    "מפלגת העבודה":         "העבודה",
    "labor":                "העבודה",
    "labour":               "העבודה",
    'רע"ם':                 'רע"ם',
    "raam":                 'רע"ם',
    "ra'am":                'רע"ם',
    'חד"ש':                 'חד"ש-תע"ל',
    "hadash":               'חד"ש-תע"ל',
    "hadash taal":          'חד"ש-תע"ל',
    "יהדות התורה":          "יהדות התורה",
    "יהדות התורה המאוחדת":  "יהדות התורה",
    "utj":                  "יהדות התורה",
    "united torah judaism": "יהדות התורה",
    "הימין הממלכתי":        "הימין הממלכתי",
    "נעם":                  "נעם - בראשות אבי מעוז",
    "נועם":                 "נעם - בראשות אבי מעוז",
    "noam":                 "נעם - בראשות אבי מעוז",
}
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

# Public API (src/api) — sized for a browsing agent's context, smaller than the research agent's
API_PROTOCOLS_DEFAULT_SCOPES  = ("topics", "opinions")
# Public API page sizes: top_k is not a public argument; callers page with offset / the response's `next`
# query_protocols pages whole rows by a character budget (utils.tool_helpers.char_paging): offset counts
# rows, and a page takes rows while their served JSON is under API_PROTOCOLS_PAGE_CHARS, plus the row
# that crosses it. Rows are never split or cut, so a page can run one row over the budget.
API_PROTOCOLS_PAGE_CHARS                 = 28_000  # per scope
API_PROTOCOLS_MAX_OFFSET                 = 20_000  # rows; the meeting with the most speeches has ~7.2k
API_FIND_PAGE_SIZE                       = 5    # find_mk, find_committee
API_FIND_PARTY_PAGE_SIZE                 = 3
API_FIND_LISTING_PAGE_SIZE               = 100  # find_party / find_committee with an empty query: all of them
API_LIST_PAGE_SIZE                       = 20   # query_bills, query_votes (rows)
PUBLIC_API_RETRY_ATTEMPTS        = 2    # api.app and web.app processes: fail fast when the Knesset API is down
PUBLIC_API_RETRY_SLEEP           = 1
PUBLIC_API_HTTP_TIMEOUT_SECONDS  = 10
API_RATE_LIMIT_ENABLED              = True
API_RATE_LIMIT_UPSTREAM_PER_MINUTE  = 10   # per client IP, routes that call the Knesset APIs
API_RATE_LIMIT_DB_PER_MINUTE        = 60   # per client IP, knesset.db-only routes
API_RATE_LIMIT_PROFILES_PER_MINUTE  = 60   # per client IP, candidate-profile routes that call the Knesset APIs (cached)
API_TRUST_CLOUDFLARE_IP_HEADER      = True  # True only when the server is reachable solely through Cloudflare
API_RATE_LIMIT_AGENT_PER_MINUTE     = 5    # per client IP, web routes that run an LLM
WEB_REQUIRE_USER_GEMINI_KEY         = True   # web agent tab bills the visitor's key (X-Gemini-Api-Key), never the server's
GEMINI_KEY_CHECK_TIMEOUT_SECONDS    = 10
GEMINI_KEY_CHECK_CACHE_SECONDS      = 600
GEMINI_KEY_CHECK_CACHE_MAX_ENTRIES  = 10_000
GEMINI_MODEL_LIST_EXCLUDED_NAME_PARTS = ("-tts", "-image", "-audio", "-live", "embedding", "robotics", "computer-use", "omni", "transcribe")   # not text-chat models
API_TRUSTED_PROXY_HOSTS             = ("127.0.0.1", "::1")  # cloudflared runs on this machine
DB_QUERY_TIMEOUT_SECONDS            = 20

# Protocol FTS5 queries: each query word is OR-ed with its indexed prefixed forms (ביוקר, המחיה)
FTS_HEBREW_PREFIXES = ("ה", "ב", "ו", "ל", "מ", "ש", "כ", "וה", "שה", "מה", "וב", "ול", "לה", "בה", "כש")
FTS_MAX_PREFIXED_VARIANTS_PER_WORD  = 24   # on top of the ktiv spelling variants
FTS_MIN_PREFIXED_WORD_CHARS         = 3    # shorter words (שר, כל) turn into other words behind a prefix
# A query word that starts with a prefix (המחיה) also matches its bare base (מחיה) when the base is a common
# indexed word, not a root that happens to start with a prefix letter (מדינה -> דינה, ממשלה -> משלה)
FTS_MIN_STRIPPED_BASE_DOCS          = 50
FTS_MIN_STRIPPED_BASE_DOC_RATIO     = 0.1  # of the query word's own doc count
API_MAX_QUERY_CHARS           = 200
API_MAX_QUERY_WORDS           = 12       # FTS5 AND-slots per protocol query
API_MAX_NAME_CHARS            = 100      # party / committee filter values
API_MAX_LIST_ITEMS            = 20       # repeated / comma-separated list params
API_MAX_ID_DIGITS             = 12
API_MAX_OFFSET                = 5000       # rows: query_bills, query_votes
BILL_TEXT_MAX_OFFSET          = 2_000_000  # characters into one bill document's text (get_bill)
API_KNESSET_NUM_RANGE         = (1, 25)  # live OData tools (votes, bills, find_mk, find_party, find_committee); omitted = every Knesset
PROTOCOL_KNESSET_NUMS         = (25,)    # Knessets preprocessed into knesset.db: query_protocols, committee listing with meeting counts
API_RATE_LIMIT_WEB_PER_MINUTE = 300      # per client IP, every other web route (reading tab fires one request per meeting/speaker)

# Research agent tool arguments: validated like the public API (api.tool_arguments) but with room for
# what the agent legitimately sends (whole-meeting transcripts paged by offset, many meeting_ids)
AGENT_MAX_QUERY_CHARS         = 500
AGENT_MAX_QUERY_WORDS         = 60
AGENT_MAX_NAME_CHARS          = 200
AGENT_MAX_LIST_ITEMS          = 500      # meeting_ids from earlier steps; SQLite caps bound variables at 32766
AGENT_MAX_OFFSET              = 100_000
AGENT_FIND_MAX_TOP_K          = 20
AGENT_LIST_MAX_TOP_K          = 100      # bills / votes; the OData service caps $top at 100

# Web page paths and the links the API/MCP rows carry to them (utils/source_links.py)
PUBLIC_SITE_URL               = "https://meorav.com"
CHAT_PAGE_PATH                = "/chat"
RESEARCH_PAGE_PATH            = "/research"
PROTOCOLS_PAGE_PATH           = "/protocols"
GAME_PAGE_PATH                = "/game"

# "היכרות" game (web/game.py): the browser sends the theme ids it has seen / voted on
GAME_MAX_IDS                  = 2500     # per list in a request; above the number of themes in the pool
GAME_MAX_CARDS_PER_FETCH      = 3

# MCP endpoint (src/api/mcp_server.py): POST /mcp on both servers, and the whole of MCP_SUBDOMAIN_HOSTS
MCP_ENABLED                   = True
MCP_PATH                      = "/mcp"
MCP_SUBDOMAIN_HOSTS           = ("mcp.meorav.com",)   # "/" and MCP_PATH serve MCP, every other path 404s
MCP_ALLOWED_HOSTS             = ("meorav.com", "mcp.meorav.com", "localhost:*", "127.0.0.1:*", "[::1]:*")
MCP_ALLOWED_ORIGINS           = ("https://meorav.com", "https://mcp.meorav.com",
                                 "http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*")
# Cloudflare Access in front of MCP: requests that came through Cloudflare must carry a valid
# Cf-Access-Jwt-Assertion; local requests (peer in MCP_LOCAL_PEER_HOSTS, no Cloudflare headers) skip it.
# Empty team domain or audiences → every Cloudflare request is refused (403).
MCP_REQUIRE_CLOUDFLARE_ACCESS = True
CF_ACCESS_TEAM_DOMAIN         = "wild-wildflower-e296.cloudflareaccess.com"
CF_ACCESS_AUDIENCES           = ("497cbb31a985d70c20aa792149f1b0920d813b9f4c6c190c2f251085debeabc6",)  # Access app AUD tags covering MCP
CF_ACCESS_JWKS_CACHE_SECONDS  = 3600
MCP_LOCAL_PEER_HOSTS          = ("127.0.0.1", "::1")

# Public web server (web/app.py, exposed through a Cloudflare tunnel)
WEB_MAX_REQUEST_BODY_BYTES           = 64 * 1024
WEB_MAX_MK_PHOTO_NAME_CHARS          = 100
WEB_MK_PHOTO_CACHE_MAX_ENTRIES       = 2000
WEB_MAX_WORKSPACE_CHUNK_CHARS        = 8000
WEB_MAX_WORKSPACE_SELECTED_CHUNKS    = 50
WEB_MAX_DEEP_DIVE_MEETINGS           = 20
WEB_MAX_OUTPUT_VAR_CHARS             = 100
WEB_MAX_HITS_QUERY_CHARS             = 500    # the heatmap sends a whole summary topic as the query
WEB_MAX_HITS_QUERY_WORDS             = 60
WEB_SESSION_MAX_AGE_HOURS            = 2.0
WEB_SESSION_CLEANUP_INTERVAL_SECONDS = 600
WEB_RESEARCH_MAX_CONCURRENT_RUNS     = 5
WEB_RESEARCH_MAX_QUEUED_RUNS         = 10
WEB_RESEARCH_SLOT_WAIT_SECONDS       = 300
WEB_WORKSPACE_MAX_CONCURRENT_ASKS    = 4      # workspace/ask runs in flight; more → 503 busy
WEB_WORKSPACE_ASK_MAX_TOKENS         = 1024
WEB_WORKSPACE_ASK_MAX_CONTEXT_CHARS  = 8000
WEB_SSE_KEEPALIVE_SECONDS            = 15.0
WEB_META_CACHE_SECONDS               = 3600
# script-src names the exact CDN files index.html loads. style-src 'unsafe-inline' is for the style=""
# attributes the page and its scripts set (Tailwind is prebuilt into static/tailwind.css, scripts/build_css.py).
WEB_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js "
    "https://cdn.jsdelivr.net/npm/dompurify@3.4.16/dist/purify.min.js; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
)
WEB_DOCS_CONTENT_SECURITY_POLICY = (   # /docs, /redoc: self-hosted bundles (they use new Function), inline bootstrap script
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "img-src 'self' data:; "
    "worker-src 'self' blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
)

# Bill text
BILL_TEXT_DEFAULT_MAX_CHARS  = 1000
BILL_TEXT_MIN_MAX_CHARS      = 200
BILL_TEXT_MAX_MAX_CHARS      = 8000
BILL_DOCUMENT_HOST_SUFFIX    = "knesset.gov.il"   # bill PDFs are only fetched over https from this domain
BILL_PDF_MAX_BYTES           = 15_000_000
BILL_PDF_MAX_PAGES           = 40

# Tool result truncation
# Max chars of `full` text sent to the executor LLM per tool result message.
EXECUTOR_TOOL_RESULT_CHARS   = 4000
# Max chars of `full` text included in the step_completed SSE event payload.
AGENT_STEP_FULL_CHARS        = 8000

# Sessions on disk (evidence overflow)
SESSIONS_DIR = DATA_DIR / "sessions"

# Server logs (src/api/request_log.py): requests.jsonl, questions.jsonl, errors.log, rotated at UTC midnight
LOG_DIR                          = DATA_DIR / "logs"
LOG_RETENTION_DAYS               = 14
REQUEST_LOG_MAX_QUERY_CHARS      = 300
REQUEST_LOG_MAX_USER_AGENT_CHARS = 200
WEB_GENERIC_ERROR_MESSAGE        = "אירעה שגיאה בשרת. נסו שוב מאוחר יותר."
