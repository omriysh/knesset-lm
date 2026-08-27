"""
meeting.py

Helpers for loading and preparing meeting protocol data.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from config import MAX_CHUNK_CHARS

# ── extract_attendance() full_text parsing ───────────────────────────────────
#
# The נכחו: (attendance) header of OData PDF/Word-extracted protocols is a
# labeled roster list, NOT a "ח"כ <name>" title-prefixed list (that older
# assumption matched 0% of real files; see extract_attendance() docstring).
# Confirmed format via direct byte inspection of real files under
# Data/raw_transcriptions/25/*/*.json:
#
#   נכחו:
#   חברי הוועדה:              <- roster: one name per line, optional
#   <name> – <role>               inline "– <role>" suffix (e.g. "– היו"ר")
#   <name>
#
#   חברי הכנסת:               <- roster, same shape
#   <name>
#
#   מוזמנים:                  <- guest block: name / lone "–" / role,
#   <name>                        each guest separated by a blank line
#   –
#   <role, org>
#
#   ייעוץ משפטי: / מנהל(ת) הוועדה: / רישום פרלמנטרי: / etc.  <- roster
#
#   רשימת הנוכחים על תואריהם מבוססת על המידע שהוזן במערכת    <- end-of-
#   המוזמנים הממוחשבת. ייתכנו אי-דיוקים והשמטות.                בלוק אנכר
#
# Two PDF-extraction artifacts must be normalized before any of this parses:
#   1. Lines are sometimes CR-only (old Mac line endings) — re.MULTILINE
#      only treats \n as a line boundary, so this must be converted first.
#   2. A stray \x07 (BEL) control char prefixes many lines in the guests
#      section (bullet-point remnant from the source doc) — harmless noise,
#      stripped.
#
# CRITICAL: the role separator is the EN DASH "–", never a plain
# hyphen "-" (U+002D) — some real names contain a plain hyphen as part of
# the surname itself (e.g. "רום בר-אב", "מירי פרנקל-שור"). Splitting on
# plain "-" would mangle these into truncated fragments.
_EN_DASH = "–"

# A plain hyphen IS a role separator when whitespace touches it on either side
# ('אליהו רביבו- היו"ר', 'אריאל צרפתי - מתמחה'). Hyphenated surnames never have
# that whitespace ('רום בר-אב', 'מירי פרנקל-שור'), so they survive intact.
_SPACED_HYPHEN_RE = re.compile(r"\s-|-\s")

# End-of-attendance-block anchor. Verified present (with only cosmetic
# whitespace/typo drift observed in real data — missing/truncated "רשימת"
# prefix, "המידע" mistyped "המיודע", "המוזמנים" occasionally split as
# "המו זמנים") across ~1650 real full_text meetings spanning many
# committees and date ranges (~99.7% match rate). Deliberately does NOT
# require the "רשימת" prefix and tolerates one arbitrary token between
# "על" and "שהוזן" (covers the "המידע"/"המיודע" typo). Files where it
# doesn't match (very old / malformed protocols) fall back to a fixed
# char cap (_ATTENDANCE_FALLBACK_CAP below) rather than failing.
_ATTENDANCE_END_RE = re.compile(
    r"(?:ר?שימת\s+)?הנוכחים\s+"
    r"על\s+תואריהם\s+"
    r"מבוססת\s+על\s+\S{1,12}\s+"
    r"שהוזן\s+במערכת"
)
_ATTENDANCE_ANCHOR_WINDOW = 5000  # נכחו is always found well within this in real data (max observed: 2482)
_ATTENDANCE_FALLBACK_CAP  = 12000  # safety net when the end anchor isn't found

# Section labels that introduce a "guest block" (name / lone en-dash / role,
# each guest separated by a blank line) rather than a plain one-name-per-line
# roster. Sanity-checked across ~10 real files spanning 6+ committees;
# variants found (all folded in here): plain מוזמנים, a "(באמצעים מקוונים)"
# parenthesized suffix, an unparenthesized "באמצעים מקוונים/דיגיטליים"
# suffix, and the משתתפים family (some committees use this instead of
# מוזמנים) in the same variant shapes including gendered singular forms.
_GUEST_LABELS = {
    "מוזמנים",
    "מוזמנים (באמצעים מקוונים)",
    "מוזמנים באמצעים מקוונים",
    "מוזמנים באמצעים דיגיטליים",
    "משתתפים",
    "משתתפים (באמצעים מקוונים)",
    "משתתפים באמצעים מקוונים",
    "משתתפים באמצעים דיגיטליים",
    "משתתפים באופן מקוון",
    "משתתף באמצעים מקוונים",
    "משתתפת באמצעים מקוונים",
}
# Any other short colon-terminated line (נכחו, חברי הוועדה, חברי/חברת/חבר
# הכנסת, ייעוץ משפטי, יועץ/יועצת משפטי(ת), מנהל/מנהלת/מנהלות/מ"מ/סגן(ית)
# מנהל(ת) הוועדה, רישום פרלמנטרי, רשמת פרלמנטרית, and assorted
# committee-staff role labels like "ראש תחום..." / "רכז/ת פרלמנטרי/ת
# בוועדה" / "מרכז המחקר והמידע") falls through to the generic "roster"
# parser instead of needing an exhaustive enum — this is low-value data
# (legal counsel / stenographer / staff names), fine to fold into the flat
# returned name list without special categorization. The MK-vs-guest split
# needed for filtering happens downstream via mk_id fuzzy-resolution
# (build_meeting_index.py), not from which section a name came from.
_HEADER_LINE_MAX_LEN = 40

# Longest real name candidate accepted by _parse_attendance_section's add()
# (see its docstring re: the fallback-cap-swallows-dialogue edge case).
_MAX_NAME_LEN = 40

# Non-name transcript header/artifact tokens that surface as bogus "speaker
# turns" — both when parse_full_text_speeches() splits full_text on
# colon-terminated lines and when a converted full_text document arrives in
# "speeches" shape with its נכחו: header turned into pseudo-speeches (see
# _structured_header_text). Left in, they cause false-positive fuzzy matches
# downstream (e.g. "קריאה" resolving to an unrelated MK by partial-ratio).
_SPEAKER_STOPLIST = {
    "קריאה", "קריאות", "קריאת ביניים", "סדר היום", "חברי הוועדה",
    "חברי הועדה", "חברי הכנסת", "חברי כנסת", "מוזמנים",
    "מוזמנים באמצעים מקוונים", "מוזמנים באמצעים דיגיטליים",
    "משתתפים", "משתתפים באמצעים מקוונים", "משתתפים באמצעים דיגיטליים",
    "נכחו", "נוכחים", "השתתפו", "השתתפו באמצעים מקוונים",
    "ייעוץ משפטי", "יועץ משפטי", "יועצת משפטית",
    "מנהל הוועדה", "מנהלת הוועדה", "מנהל/ת הוועדה", "מזכירת הוועדה",
    "רישום פרלמנטרי", "רשמת פרלמנטרית", "קצרנית", "קצרן",
}

# Stage directions and editorial notes that the converted-protocol pipeline
# emits as "speakers" — e.g. "(מוקרן סרטון, להלן התמלול)",
# "(תרגום חופשי מהשפה האנגלית)". A label wrapped entirely in parentheses is
# never a person. A party/role suffix on a real name is NOT wrapped
# ("מיכל מרים וולדיגר (הציונות הדתית)"), so those survive.
_PARENTHESIZED_ONLY_RE = re.compile(r"^\(.*\)$", re.S)
# Latin letters are allowed: foreign guests appear under their English name
# (real example: "Dr. Gautam nand Allahbadia").
_NAME_WORD_RE = re.compile(r"[א-תA-Za-z]{2,}")

# Committee-staff section labels are open-ended ("ראש תחום ...", "רכזת
# פרלמנטרית בוועדה", ...), so header detection matches on suffix rather than
# an exhaustive enum.
_HEADER_LABEL_SUFFIXES = ("הוועדה", "הועדה", "משפטי", "משפטית",
                          "פרלמנטרי", "פרלמנטרית")

# The reconstructed header of a "speeches"-shape protocol never runs past the
# first handful of pseudo-speeches; hard cap so a malformed file can't drag
# real dialogue into the attendance section.
_MAX_HEADER_SPEECHES = 20

# "<section label>: <body>" collapsed onto one line inside a pseudo-speech
# body (real example: the "נכחו" pseudo-speech carries "חברי הוועדה: <roster>").
_INLINE_LABEL_RE = re.compile(r"^([^:\n]{1,40}):[ \t]*")


def _is_person_name(name: str) -> bool:
    """True if a speaker/roster label plausibly names a person."""
    text = name.strip()
    if not text or text in _SPEAKER_STOPLIST or _PARENTHESIZED_ONLY_RE.match(text):
        return False
    without_suffix = re.sub(r"\([^)]*\)", " ", text).strip()
    if not without_suffix or without_suffix in _SPEAKER_STOPLIST:
        return False
    return bool(_NAME_WORD_RE.search(without_suffix))


def _is_attendance_header_label(label: str) -> bool:
    """True if a pseudo-speech `speaker` is an attendance-header section label."""
    text = label.strip().rstrip(":").strip()
    if not text or len(text) > _HEADER_LINE_MAX_LEN:
        return False
    return (text in _SPEAKER_STOPLIST
            or text in _GUEST_LABELS
            or text.endswith(_HEADER_LABEL_SUFFIXES))


@lru_cache(maxsize=4)
def _mk_name_lexicon(knesset_num: int = 25) -> tuple[str, ...]:
    """Canonical multi-token MK names for a Knesset, read from the mks BM25 db.

    Needed only by the "speeches"-shape attendance parser: those protocols are
    converted full_text documents whose roster lost every line break, so names
    are glued with no separator at all ("טלי גוטליבשלום דנינו") and can only be
    segmented against a known-name lexicon. Returns () when mks.db hasn't been
    built yet — the parser then degrades to speaker-only attendance, i.e. the
    behaviour that predated this lexicon.
    """
    import config
    path = config.BM25_DIR / str(knesset_num) / "mks.db"
    if not path.exists():
        print(f"[meeting] mks.db not found ({path}); glued attendance rosters in "
              f"'speeches'-shape protocols cannot be split")
        return ()
    try:
        from retrieval.bm25_index import BM25Index
        index = BM25Index(path)
        try:
            rows = index._connect().execute("SELECT label FROM entries").fetchall()
        finally:
            index.close()
    except Exception as exc:
        print(f"[meeting] MK name lexicon load failed from {path}: {exc}")
        return ()
    labels = {str(row[0]).strip() for row in rows if row[0]}
    return tuple(sorted(label for label in labels if len(label.split()) >= 2))


def _split_glued_roster_line(line: str, name_lexicon: tuple[str, ...]) -> list[str]:
    """Segment a roster line whose names lost their separators.

    Picks leftmost-longest non-overlapping lexicon matches, so role markers and
    glue characters between names ('– היו"ר', '– מ"מ היו"ר', '- היו"ר', a bare
    space, a tab, or nothing at all — all observed in real files) are simply
    skipped as unmatched filler. Returns [] when nothing matches, which is the
    caller's signal to fall back to the one-name-per-line rule.
    """
    hits: list[tuple[int, int, str]] = []
    for name in name_lexicon:
        start = line.find(name)
        while start != -1:
            hits.append((start, start + len(name), name))
            start = line.find(name, start + 1)
    hits.sort(key=lambda hit: (hit[0], -hit[1]))

    found: list[str] = []
    cursor = 0
    for start, end, name in hits:
        if start >= cursor:
            found.append(name)
            cursor = end
    return found


def _structured_header_text(meeting: dict) -> str:
    '''Rebuild the נכחו: header region of a "speeches"-shape protocol.

    These files are not speech-by-speech scrapes — they are converted full_text
    documents whose header became a run of leading pseudo-speeches: `speaker` is
    the section label ("נכחו", "חברי הכנסת", "ייעוץ משפטי", ...) and `text_he`
    is that section's body. Re-emitting them as "label:\\nbody" lines produces
    exactly the shape _find_attendance_section/_parse_attendance_section already
    parse.

    Guest sections are dropped: unlike the full_text form (name / lone en-dash /
    role on separate lines) their bodies are "name - role, org" entries glued
    end-to-end with no separator, so the guest name can't be told apart from the
    previous entry's organisation. Guests who spoke still come from the speaker
    list.
    '''
    lines: list[str] = []
    for speech in meeting.get("speeches", [])[:_MAX_HEADER_SPEECHES]:
        label = (speech.get("speaker") or "").strip()
        body = (speech.get("text_he") or "").strip()
        if not label:
            continue
        if not _is_attendance_header_label(label):
            break
        label = label.rstrip(":").strip()
        if label not in _GUEST_LABELS:
            lines.append(f"{label}:")
            lines.append(_INLINE_LABEL_RE.sub(r"\1:\n", body, count=1))
        if _ATTENDANCE_END_RE.search(body):
            break
    return "\n".join(lines)

# Matches speaker-turn headers in OData full_text protocols.
# Handles:  "היו"ר שם:"  "ח"כ שם:"  "שם (מפלגה):"  "שם:"
# Requires colon at end of line (no body text after it on same line).
# Uses [ \t]+ (not \s+) between name tokens to avoid crossing line boundaries.
# NOTE: full_text must be LF-normalised before use — CR-only PDFs fool re.MULTILINE.
_SPEAKER_TURN_RE = re.compile(
    r'^('
    r'(?:היו["\u05f3\u05f4\u2019\u201d]ר[ \t]+|ח["\u05f3\u05f4\u2019\u201d]כ[ \t]+|'
    r'(?:סגן[ \t]+)?שר(?:ת)?[ \t]+|ממלא[ \t]+מקום[ \t]+)?'   # optional title prefix
    r'[\u05d0-\u05ea][\u05d0-\u05ea\-\u05f3\u05f4"\']{0,20}'  # first name token
    r'(?:[ \t]+[\u05d0-\u05ea][\u05d0-\u05ea\-\u05f3\u05f4"\']{0,20}){0,3}'  # up to 3 more
    r')'
    r'(?:[ \t]*\([^)\n]{1,40}\))?'   # optional (party / role)
    r'[ \t]*:[ \t]*$',               # colon at end of line only
    re.MULTILINE,
)


_meeting_registry: dict[str, str] = {}  # meeting_id → summary .txt path


def register_meeting_paths(paths: dict[str, str]) -> None:
    """Register a batch of meeting_id → summary-path mappings into the global registry."""
    _meeting_registry.update(paths)


def _find_summary_on_disk(meeting_id: str) -> Path | None:
    """Locate a meeting's summary .txt by its id suffix under Data/summaries.

    Summary files are named ``DD_MM_YYYY_<session_id>.txt`` and the meeting_id
    IS that trailing session_id, so a ``*_<meeting_id>.txt`` glob resolves it
    regardless of committee-folder or knesset-number nesting.
    """
    if not meeting_id.isdigit():
        return None
    import config
    root = config.DATA_DIR / "summaries"
    try:
        return next(root.glob(f"**/*_{meeting_id}.txt"), None)
    except Exception as exc:
        print(f"[meeting] summary glob failed for {meeting_id!r}: {exc}")
        return None


def get_summary_path_from_id(meeting_id: str) -> Path | None:
    """Return the summary .txt Path for a meeting_id, or None if not found.

    Fast path: the in-memory registry populated by ``register_meeting_paths``
    during a RAG run. Fallback: glob the summaries tree so meetings that were
    never registered (e.g. opened from an agent citation) still resolve; hits
    are cached back into the registry.
    """
    mid = str(meeting_id)
    p = _meeting_registry.get(mid)
    if p:
        return Path(p)
    found = _find_summary_on_disk(mid)
    if found is not None:
        _meeting_registry[mid] = str(found)
    return found


def get_transcript_path_from_id(meeting_id: str) -> Path | None:
    """Return the raw transcript JSON Path for a meeting_id, or None if not registered."""
    summary = get_summary_path_from_id(meeting_id)
    return transcript_path_from_summary(summary) if summary else None


def load_meeting(filepath: str | Path) -> dict:
    """Load a meeting JSON file and return its contents."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def build_transcript_text(meeting: dict) -> str:
    """
    Format a meeting into a single transcript string for the LLM.

    Supports two source formats:
    - 'speeches': list of {speaker, text_he} dicts (oknesset.org scraper format)
    - 'full_text': raw protocol string (OData PDF extraction format)
    """
    if "full_text" in meeting:
        return meeting["full_text"]

    lines = []
    for speech in meeting.get("speeches", []):
        speaker = speech.get("speaker", "").strip()
        text    = speech.get("text_he", "").strip()
        if speaker or text:
            lines.append(f"{speaker}: {text}")
    return "\n\n".join(lines)


def _normalize_full_text_for_attendance(full_text: str) -> str:
    """Strip BEL artifacts and normalize CR-only/CRLF line endings to LF."""
    text = full_text.replace("\x07", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _find_attendance_section(full_text: str) -> str:
    """
    Return the raw text slice spanning the נכחו: attendance header, from the
    "נכחו" anchor up to (not including) the standard end-of-block boilerplate
    sentence — or up to _ATTENDANCE_FALLBACK_CAP chars if that boilerplate
    isn't found (older/malformed protocols). Returns "" if no נכחו anchor is
    found at all (e.g. background/bill documents miscategorized as meetings).
    """
    text = _normalize_full_text_for_attendance(full_text)
    anchor = re.search(r"נכחו", text[:_ATTENDANCE_ANCHOR_WINDOW])
    if not anchor:
        return ""
    start = anchor.start()
    end_match = _ATTENDANCE_END_RE.search(text, start)
    end = end_match.start() if end_match else start + _ATTENDANCE_FALLBACK_CAP
    return text[start:end]


def _parse_attendance_section(section_text: str, name_lexicon: tuple[str, ...] = ()) -> list[str]:
    """
    Section-aware line-by-line parser for a נכחו: attendance block (already
    isolated by _find_attendance_section).

    ``name_lexicon`` (see _mk_name_lexicon) is only passed for "speeches"-shape
    protocols, whose roster lines hold several separator-less names at once; it
    is left empty for full_text, where one line really is one name.

    Recognizes short colon-terminated lines as sub-section headers, switching
    parse mode:
      - a label in _GUEST_LABELS (מוזמנים / משתתפים and variants) -> "guest"
        mode: each blank-line-separated block is name / lone en-dash / role;
        only the first line (the name) is kept, with any inline "– role"
        suffix stripped (en-dash only — never a plain hyphen, so names like
        "רום בר-אב" survive intact).
      - anything else (חברי הוועדה, חברי הכנסת, ייעוץ משפטי, מנהל(ת) הוועדה,
        רישום פרלמנטרי, misc staff labels, ...) -> "roster" mode: one name
        per line, with anything from the first en-dash or comma onward
        stripped (chair-role suffix / trailing role after a comma).
    """
    names: list[str] = []
    seen: set[str] = set()

    def add(raw_name: str) -> None:
        name = raw_name.strip()
        # Guard against the rare case (~5/1658 real files) where the
        # end-of-block boilerplate anchor isn't found and the fallback char
        # cap (_ATTENDANCE_FALLBACK_CAP) swallows real dialogue — free-text
        # sentences (interjections, Q&A fragments) would otherwise get taken
        # whole as "names" by roster mode. Real Hebrew names, even long
        # multi-part ones ("יולי יואל אדלשטיין"), stay well under this.
        if name and len(name) <= _MAX_NAME_LEN and name not in seen:
            seen.add(name)
            names.append(name)

    mode = "roster"  # default before any header line is seen
    guest_buffer: list[str] = []

    def flush_guest() -> None:
        if guest_buffer:
            first = guest_buffer[0]
            if _EN_DASH in first:
                add(first.split(_EN_DASH, 1)[0])
            else:
                add(first)
        guest_buffer.clear()

    for raw_line in section_text.split("\n"):
        line = raw_line.strip()
        if not line:
            if mode == "guest":
                flush_guest()
            continue

        if line.endswith(":") and len(line) <= _HEADER_LINE_MAX_LEN:
            if mode == "guest":
                flush_guest()
            label = line.rstrip(":").strip()
            mode = "guest" if label in _GUEST_LABELS else "roster"
            continue

        if mode == "guest":
            guest_buffer.append(line)
        else:
            glued = _split_glued_roster_line(line, name_lexicon) if name_lexicon else []
            if glued:
                for name in glued:
                    add(name)
                continue
            hyphen = _SPACED_HYPHEN_RE.search(line)
            idx_dash = line.find(_EN_DASH)
            idx_comma = line.find(",")
            idx_hyphen = hyphen.start() if hyphen else -1
            candidates = [i for i in (idx_dash, idx_comma, idx_hyphen) if i >= 0]
            add(line[:min(candidates)] if candidates else line)

    if mode == "guest":
        flush_guest()

    return names


def extract_attendance(meeting: dict) -> list[str]:
    """
    Extract attendee names from a meeting protocol.

    For structured (speeches) format: returns the נכחו: roster names first,
    then unique speaker names, both in order of first appearance. These files
    are converted full_text documents whose header survives as leading
    pseudo-speeches (see _structured_header_text), so the roster is recovered
    by rebuilding that header and running the same section parser used for
    full_text — with an MK name lexicon, because the conversion glued the
    roster names together with no separator. Speakers alone (the previous
    behaviour) silently dropped every attendee who never took the floor.

    For full_text (raw OData PDF/Word extraction) format: parses the נכחו:
    attendance header — a labeled roster (חברי הוועדה: / חברי הכנסת: / ייעוץ
    משפטי: / מנהל(ת) הוועדה: / רישום פרלמנטרי: / etc., one name per line) plus
    a מוזמנים: (guests) block (name / lone en-dash / role, blank-line
    separated) — bounded from the נכחו anchor to the standard
    "רשימת הנוכחים... " boilerplate sentence that terminates the block (or a
    generous char cap if that sentence isn't found). See the module-level
    comment above _EN_DASH for the full format writeup and the real-file
    verification behind it. Returns deduplicated names in order of
    appearance; MK-vs-guest categorization is NOT done here (that happens
    downstream via mk_id fuzzy-resolution against mks.db, in
    scripts/build_meeting_index.py) — this stays a flat list.

    Returns an empty list if no names are found.
    """
    if "speeches" in meeting:
        names: list[str] = []
        seen_set: set[str] = set()

        def collect(candidate: str) -> None:
            if candidate and candidate not in seen_set and _is_person_name(candidate):
                seen_set.add(candidate)
                names.append(candidate)

        section = _find_attendance_section(_structured_header_text(meeting))
        if section:
            try:
                knesset_num = int(meeting.get("knesset_num") or 25)
            except (TypeError, ValueError) as exc:
                print(f"[meeting] bad knesset_num {meeting.get('knesset_num')!r}: {exc}")
                knesset_num = 25
            for name in _parse_attendance_section(section, _mk_name_lexicon(knesset_num)):
                collect(name)

        for speech in meeting["speeches"]:
            collect((speech.get("speaker") or "").strip())
        return names

    if "full_text" in meeting:
        section = _find_attendance_section(meeting["full_text"])
        if not section:
            return []
        return _parse_attendance_section(section)

    return []


def get_meeting_speakers(meeting: dict) -> list[str]:
    """
    Return deduplicated speaker names who actually spoke in this meeting,
    in order of first appearance.

    Deliberately derived from who *spoke*, not who's listed as attending:
    silently-present attendees come from extract_attendance() instead, and
    build_meeting_index.py unions the two.

    Supports both meeting JSON formats:
    - structured ('speeches' field): speaker names taken directly.
    - full_text (OData PDF/Word extraction): speaker turns parsed via
      parse_full_text_speeches(), which — unlike extract_attendance's
      header regex — operates on the dialogue body, where PDF extraction
      preserves real line breaks (only the נכחו: header section glues
      names together with no whitespace).

    _is_person_name() filters out non-name transcript artifacts (section
    headers, interjection markers, parenthesized stage directions) that get
    picked up as "speakers" because they're colon-terminated lines too.
    """
    if "speeches" in meeting:
        speeches = meeting["speeches"]
    else:
        full_text = meeting.get("full_text", "")
        speeches = parse_full_text_speeches(full_text) or []

    seen: list[str] = []
    seen_set: set[str] = set()
    for speech in speeches:
        speaker = (speech.get("speaker") or "").strip()
        if not speaker or speaker in seen_set or not _is_person_name(speaker):
            continue
        seen.append(speaker)
        seen_set.add(speaker)
    return seen


def parse_full_text_speeches(full_text: str) -> list[dict] | None:
    """
    Parse a raw OData full_text protocol into [{speaker, text_he}] entries.

    Splits on speaker-turn headers (e.g. 'היו"ר שם:' / 'שם (מפלגה):').
    Returns None if fewer than 2 speaker turns are found (triggers fallback).
    """
    # PDF extraction often produces CR-only line endings; re.MULTILINE ^ only
    # matches after \n, so normalise before applying the regex.
    full_text = full_text.replace('\r\n', '\n').replace('\r', '\n')

    matches = list(_SPEAKER_TURN_RE.finditer(full_text))
    if len(matches) < 2:
        return None

    speeches: list[dict] = []
    for i, m in enumerate(matches):
        speaker = m.group(1).strip()
        text_start = m.end()
        text_end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        text = full_text[text_start:text_end].strip()
        if text:
            speeches.append({"speaker": speaker, "text_he": text})

    return speeches if speeches else None


def transcript_path_from_summary(summary_path: Path) -> Path:
    """Derive the raw transcript JSON path from a summary .txt path."""
    return Path(
        str(summary_path)
        .replace("summaries", "raw_transcriptions", 1)
        .replace(".txt", ".json")
    )


def format_meeting_chunks(meeting: dict) -> list[dict]:
    """
    Format a meeting into display chunks for the web UI.

    Returns list of {chunk_id: str, speaker: str, text: str}.

    Three formats handled:
    - structured speeches  → ftfy-cleaned text per speech
    - full_text (parsable) → speaker-turn split speeches
    - full_text (fallback) → paragraph split, empty speaker
    """
    import ftfy

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
    return chunks


def chunk_transcript(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """
    Split a transcript into chunks that each fit within max_chars.
    Splits on speech boundaries (double newline). Returns a list of chunk strings.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        cutoff = remaining.rfind("\n\n", 0, max_chars)
        if cutoff == -1:
            cutoff = max_chars
        chunks.append(remaining[:cutoff])
        remaining = remaining[cutoff:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks
