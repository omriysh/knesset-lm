"""
knesset_db.py

Data access layer for Knesset member data.
Uses the backend.oknesset.org REST API as primary source.

API endpoints:
    https://backend.oknesset.org/members?is_current=true   — current MKs
    https://backend.oknesset.org/members?is_current=false  — former MKs

Each member record contains:
    mk_individual_id, mk_individual_first_name, mk_individual_name,
    PersonID, IsCurrent, altnames,
    factions        — list of {faction_id, faction_name, start_date, finish_date, knesset}
    committee_positions — list of committee roles
    faction_chairpersons — list of faction chair periods
    govministries   — list of {govministry_name, position_name, start_date, finish_date, knesset}

Usage:
    from utils.knesset_db import get_mk_profile, get_all_mks, get_all_parties
"""

import os
import requests
from urllib.parse import quote
from functools import lru_cache
import io
import pdfplumber
import fitz  # pymupdf
import re
import docx          # python-docx
from bs4 import BeautifulSoup

try:
    import win32com.client as _win32com
    _WORD_COM_AVAILABLE = True
except ImportError:
    _WORD_COM_AVAILABLE = False

OKNESSET_API = "https://backend.oknesset.org"
OFFICIAL_KNESSET_NEW_API = "https://knesset.gov.il/OdataV4/ParliamentInfo"
TIMEOUT = 30

SESSION_TYPE_CLASSIFIED  = 160  # חסויה — classified session; no public transcript
_PROTOCOL_NAME_SUBSTRINGS = ("פרוטוקול", "protocol")

_DOC_GROUP_PRIORITY = [
    "הצעת חוק לקריאה השנייה והשלישית",
    "הצעת חוק לקריאה הראשונה",
    "טקסט חוק מאוחד",
    "חוק - פרסום ברשומות",
]
_HEBREW_DATE_RE = re.compile(r',?\s*ה?תש[\u05d0-\u05ea]{1,3}["\u05f3][\u05d0-\u05ea](?:[–\-]\d{4})?')
_BILL_TYPE_PREFIXES = ('הצעת חוק', 'חוק', 'תיקון לחוק')




# ── Helpers ───────────────────────────────────────────────────────────────────

@lru_cache(maxsize=2)
def _fetch_members(is_current: bool) -> list[dict]:
    """
    Fetch all members from the oknesset API.
    Cached per session — current and former are cached separately.
    """
    url    = f"{OKNESSET_API}/members"
    params = {"is_current": "true" if is_current else "false"}
    response = requests.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _get_all_members_raw(knesset_num: int = 25) -> list[dict]:
    """Return all members (current + former) filtered to a given Knesset."""
    current = _fetch_members(True)
    former  = _fetch_members(False)
    all_members = current + former

    if knesset_num is None:
        return all_members

    # Keep only members who had a faction in the requested Knesset
    result = []
    for mk in all_members:
        factions = [f for f in (mk.get("factions") or []) if f and f.get("knesset") == knesset_num]
        if factions:
            result.append(mk)
    return result


def _most_recent_faction(factions: list[dict], knesset_num: int) -> dict | None:
    """From a list of faction records, return the most recent one for a given Knesset."""
    relevant = [f for f in factions if f and f.get("knesset") == knesset_num]
    if not relevant:
        return None
    return max(relevant, key=lambda f: f.get("start_date") or "")


def _fix_file_path(path: str) -> str:
    """Normalize Knesset document URLs (backslashes → forward slashes)."""
    return path.replace("\\", "/")


def _is_garbage(text: str, threshold: float = 0.3) -> bool:
    """
    Return True if more than `threshold` fraction of the text characters
    are Unicode replacement characters (U+FFFD), indicating a failed decode.
    """
    if not text:
        return True
    garbage_count = text.count('\ufffd')
    return (garbage_count / len(text)) > threshold


def _extract_pdf_text_pymupdf(pdf_bytes: bytes) -> str:
    """
    Extract text from a PDF using PyMuPDF (fitz).
    Handles more Hebrew font encodings than pdfplumber.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []
    for page in doc:
        text = page.get_text("text")
        if text:
            pages.append(text)
    doc.close()
    return "\n".join(pages).strip()


def _extract_pdf_text_pdfplumber(pdf_bytes: bytes) -> str:
    """
    Extract text from a PDF using pdfplumber (pdfminer backend).
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = []
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=2, y_tolerance=2)
            if text:
                pages.append(text)
    return "\n".join(pages).strip()


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """
    Extract Hebrew text from a PDF, trying multiple engines.
    PyMuPDF first (faster, handles more font encodings).
    Falls back to pdfplumber if the result is garbage.
    Returns empty string if all methods fail.
    """
    # Try PyMuPDF first
    try:
        text = _extract_pdf_text_pymupdf(pdf_bytes)
        if text and not _is_garbage(text):
            return text
    except Exception:
        pass

    # Fall back to pdfplumber
    try:
        text = _extract_pdf_text_pdfplumber(pdf_bytes)
        if text and not _is_garbage(text):
            return text
    except Exception:
        pass

    return ""


def _sanitize_odata_search(name: str) -> str:
    """
    Prepare a bill name for use inside an OData contains() filter.
    - Strips parenthetical clauses (which break OData parser)
    - Escapes remaining single quotes as ''
    - Collapses extra whitespace
    """
    # Remove anything inside parentheses (including nested)
    sanitized = re.sub(r'\(.*?\)', '', name)
    # Collapse whitespace and strip
    sanitized = re.sub(r'\s+', ' ', sanitized).strip()
    # Remove trailing comma/dash left after stripping parens
    sanitized = sanitized.strip(',-– ').strip()
    # Escape single quotes for OData
    sanitized = sanitized.replace("'", "''")
    return sanitized


def _bill_search_terms(name: str) -> list[str]:
    """
    Generate a ranked list of OData search terms to try for a bill name.
    Each term is progressively shorter/more lenient, maximising recall.

    Strategy:
      1. Full name, parentheses stripped  (most specific)
      2. Hebrew date suffix also stripped  (removes התשפ"ד–2024 etc.)
      3. Core subject noun phrase only     (strips leading type prefix too)
      4. First 4 significant words of the subject  (widest net)
    """
    terms: list[str] = []

    # Step 1 – strip parentheses
    step1 = _sanitize_odata_search(name)
    if step1:
        terms.append(step1)

    # Step 2 – also strip Hebrew date suffix
    step2 = _HEBREW_DATE_RE.sub('', step1).strip().strip(',-– ').strip()
    if step2 and step2 != step1:
        terms.append(step2)

    # Step 3 – strip leading type prefix ("הצעת חוק", "חוק" …)
    step3 = step2
    for prefix in _BILL_TYPE_PREFIXES:
        if step3.startswith(prefix):
            step3 = step3[len(prefix):].strip()
            break
    if step3 and step3 != step2:
        terms.append(step3)

    # Step 4 – first 4 words of the subject (skip very short words like ה/ו/ב)
    words = [w for w in step3.split() if len(w) > 1]
    short = ' '.join(words[:4])
    if short and short != step3:
        terms.append(short)

    # Escape single-quotes for OData in every term
    return [t.replace("'", "''") for t in terms if t]


def _bill_record_to_dict(bill: dict) -> dict:
    """Normalise a raw KNS_Bill OData record into our standard shape."""
    initiators = [
        {
            "person_id": bi["KNS_Person"]["Id"],
            "full_name": f"{bi['KNS_Person'].get('FirstName', '')} {bi['KNS_Person'].get('LastName', '')}".strip(),
        }
        for bi in (bill.get("KNS_BillInitiator") or [])
        if bi.get("KNS_Person")
    ]
    return {
        "bill_id":          bill.get("Id"),
        "bill_name":        bill.get("Name"),
        "bill_number":      bill.get("Number"),
        "knesset_num":      bill.get("KnessetNum"),
        "type":             bill.get("TypeDesc"),
        "sub_type":         bill.get("SubTypeDesc"),
        "status":           (bill.get("KNS_Status") or {}).get("Desc"),
        "committee_id":     bill.get("CommitteeID"),
        "publication_date": bill.get("PublicationDate"),
        "last_updated":     bill.get("LastUpdatedDate"),
        "initiators":       initiators,
    }


def _search_bills_by_term(
    search_term: str,
    knesset_num: int | None,
    top: int = 3,
) -> list[dict]:
    """Run a single OData contains() search and return up to `top` raw bill dicts."""
    base_url = f"{OFFICIAL_KNESSET_NEW_API}/KNS_Bill"
    filter_expr = f"contains(Name,'{search_term}')"
    if knesset_num:
        filter_expr += f" and KnessetNum eq {knesset_num}"
    params = {
        "$filter":  filter_expr,
        "$expand":  "KNS_Status,KNS_BillInitiator($expand=KNS_Person)",
        "$top":     top,
        "$orderby": "LastUpdatedDate desc",
    }
    r = requests.get(base_url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("value", [])


def _name_matches(mk: dict, query: str) -> bool:
    """Check if a query string matches any known name or altname of an MK."""
    query = query.strip()
    full  = f"{mk.get('mk_individual_first_name', '')} {mk.get('mk_individual_name', '')}".strip()
    candidates = [full, f"{mk.get('mk_individual_name', '')} {mk.get('mk_individual_first_name', '')}".strip()]
    candidates += (mk.get("altnames") or [])

    query_lower = query.lower()
    for name in candidates:
        if not name:
            continue
        # Exact match
        if name.strip() == query:
            return True
        # Query is a substring of name or vice versa (handles partial names)
        if query_lower in name.lower() or name.lower() in query_lower:
            return True
    return False


# ── Public API ────────────────────────────────────────────────────────────────

def get_all_mks(knesset_num: int = 25) -> list[dict]:
    """
    Return all MKs who served in a given Knesset, sorted by last name.
    Each entry contains: mk_id, full_name, party, is_current, email.
    """
    members = _get_all_members_raw(knesset_num)
    result  = [mk for mk in members]
    result.sort(key=lambda x: x.get("last_name", ""))
    return result


def get_all_parties(knesset_num: int = 25) -> list[dict]:
    """
    Return all parties/factions that had seats in a given Knesset,
    sorted by MK count descending.
    Each entry contains: party, mk_count.
    """
    members = _get_all_members_raw(knesset_num)
    counts: dict[str, int] = {}
    for mk in members:
        faction = _most_recent_faction(
            [f for f in (mk.get("factions") or []) if f],
            knesset_num
        )
        if faction:
            name = faction["faction_name"].strip()
            counts[name] = counts.get(name, 0) + 1

    result = [{"party": name, "mk_count": count} for name, count in counts.items()]
    result.sort(key=lambda x: x["mk_count"], reverse=True)
    return result


def get_mk_by_name(name: str, knesset_num: int = 25) -> list[dict]:
    """
    Search for MKs by name (Hebrew, partial, or altname).
    Returns a list of MK dicts.
    """
    current = _fetch_members(True)
    former  = _fetch_members(False)
    matches = [
        mk
        for mk in (current + former)
        if _name_matches(mk, name)
    ]
    return matches


def get_all_committees(knesset_num: int = 25) -> list[dict]:
    """
    Return all committees for a given Knesset number, sorted by name.
    Each entry: {CommitteeID, Name, KnessetNum, IsCurrent}.
    """
    url      = f"{OKNESSET_API}/committees_kns_committee/list"
    response = requests.get(url, params={"KnessetNum": knesset_num, "limit": 1000}, timeout=TIMEOUT)
    response.raise_for_status()
    committees = [
        {
            "CommitteeID": c["CommitteeID"],
            "Name":        c.get("Name", ""),
            "KnessetNum":  c.get("KnessetNum"),
            "IsCurrent":   c.get("IsCurrent"),
        }
        for c in response.json()
        if c.get("Name")
    ]
    committees.sort(key=lambda c: c["Name"])
    return committees


def get_committee_by_name(name: str, knesset_num: int = 25) -> list[dict]:
    """
    Search for Knesset committees by name (Hebrew, partial match) and Knesset number.
    Uses the /committees_kns_committee/list endpoint.
    Returns a list of dicts: {CommitteeID, Name, KnessetNum, IsCurrent}.
    """
    url = f"{OKNESSET_API}/committees_kns_committee/list"
    params = {"Name": name, "KnessetNum": knesset_num, "limit": 100}
    response = requests.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    results = response.json()
    return [
        {
            "CommitteeID": c["CommitteeID"],
            "Name":        c.get("Name", ""),
            "KnessetNum":  c.get("KnessetNum"),
            "IsCurrent":   c.get("IsCurrent"),
        }
        for c in results
    ]


def get_active_committee_members(
    committee_id: int,
    knesset_num: int = 25,
    current_only: bool = True,
) -> list[dict]:
    """
    Search for MKs the are a part of a certain committee in a given time.
    Returns a list of (partial) MK dicts.
    """
    url = f"{OFFICIAL_KNESSET_NEW_API}/KNS_PersonToPosition"
    filter_expr = f"CommitteeID eq {committee_id} and KnessetNum eq {knesset_num}"
    if current_only:
        filter_expr += " and IsCurrent eq true"

    params = {
        "$expand": "KNS_Person,KNS_Position",
        "$filter": filter_expr,
        "$top":    500,
    }
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()

    seen: dict[int, dict] = {}
    for row in r.json().get("value", []):
        person   = row.get("KNS_Person") or {}
        position = row.get("KNS_Position") or {}
        mk_id    = row.get("PersonID")
        full_name = f"{person.get('FirstName', '')} {person.get('LastName', '')}".strip()
        # DutyDesc is the gendered, committee-specific role string; fall back to Position.Description
        duty = row.get("DutyDesc") or position.get("Description") or ""
        
        if 'מ"מ' in duty:
            continue

        if mk_id not in seen:
            seen[mk_id] = {"mk_id": mk_id, "full_name": full_name, "duty_desc": duty}
        else:
            # Prefer chair role over plain member if we see both
            if 'יו"ר' in duty and 'יו"ר' not in seen[mk_id]["duty_desc"]:
                seen[mk_id]["duty_desc"] = duty

    return sorted(seen.values(), key=lambda x: x["full_name"])


def get_mk_profile(name: str, knesset_num: int = 25) -> dict | None:
    """
    Look up an MK by name and return their full profile.
    Returns None if not found. If multiple match, returns the first with a flag.
    """
    matches = get_mk_by_name(name, knesset_num)
    if not matches:
        return None
    result = matches[0]
    result["multiple_matches"] = len(matches) > 1
    if len(matches) > 1:
        result["other_matches"] = [m["full_name"] for m in matches[1:] if "full_name" in m]
    return result


def get_law_or_bill_by_name(
    name_part: str,
    knesset_num: int | None = None,
) -> dict | None:
    """
    Search for a bill/law by partial Hebrew name.

    Tries progressively shorter/simpler search terms so that names the model
    writes (which include amendment suffixes, Hebrew dates, etc.) still match
    the more concise names stored in the Knesset API.

    Returns the single best match (most recently updated), or None.
    """
    for term in _bill_search_terms(name_part):
        bills = _search_bills_by_term(term, knesset_num, top=3)
        if bills:
            # Prefer the bill whose stored name best overlaps with the original query
            def _score(b: dict) -> int:
                stored = b.get("Name") or ""
                return sum(1 for ch in name_part if ch in stored)
            bills.sort(key=_score, reverse=True)
            return _bill_record_to_dict(bills[0])

    return None
    

def get_bill_documents(bill_id: int) -> list[dict]:
    """
    Return document metadata for a bill: title, type, and corrected URL.
    Sorted by usefulness (latest reading first).
    """
    r = requests.get(
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_DocumentBill",
        params={"$filter": f"BillID eq {bill_id}", "$top": 20},
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    docs = r.json().get("value", [])

    def _priority(doc):
        desc = doc.get("GroupTypeDesc", "")
        try:
            return _DOC_GROUP_PRIORITY.index(desc)
        except ValueError:
            return len(_DOC_GROUP_PRIORITY)

    docs.sort(key=_priority)
    return [
        {
            "doc_id":    doc["Id"],
            "bill_id":   bill_id,
            "group":     doc.get("GroupTypeDesc"),
            "format":    doc.get("ApplicationDesc"),
            "url":       _fix_file_path(doc["FilePath"]),
        }
        for doc in docs
        if doc.get("FilePath")
    ]


def get_bill_text(bill_id: int, max_chars: int = 8000) -> dict | None:
    """
    Fetch the most relevant document for a bill and extract its text.
    Tries documents in priority order until one succeeds.
    Returns {bill_id, doc_id, group, url, text} or None if all fail.
    max_chars limits the returned text to avoid overwhelming the context window.
    """
    docs = get_bill_documents(bill_id)
    if not docs:
        return None

    for doc in docs:
        if doc["format"] not in ("PDF",):
            continue
        try:
            response = requests.get(doc["url"], timeout=TIMEOUT)
            response.raise_for_status()

            full_text = _extract_pdf_text(response.content)
            if not full_text:
                continue  # try next doc

            return {
                "bill_id":   bill_id,
                "doc_id":    doc["doc_id"],
                "group":     doc["group"],
                "url":       doc["url"],
                "text":      full_text[:max_chars],
                "truncated": len(full_text) > max_chars,
            }

        except Exception:
            continue

    return None


def get_bill_details(bill_id: int) -> dict | None:
    # Request 1: bill + status only (no nested expand)
    url = (
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_Bill({bill_id})"
        f"?$expand=KNS_Status"
    )
    r = requests.get(url, timeout=TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    bill = r.json()

    # Request 2: initiators separately
    r2 = requests.get(
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_BillInitiator",
        params={"$filter": f"BillID eq {bill_id}", "$expand": "KNS_Person", "$top": 20},
        timeout=TIMEOUT,
    )
    r2.raise_for_status()
    initiators = [
        {
            "person_id": bi["KNS_Person"]["Id"],
            "full_name": f"{bi['KNS_Person'].get('FirstName', '')} {bi['KNS_Person'].get('LastName', '')}".strip(),
        }
        for bi in r2.json().get("value", [])
        if bi.get("KNS_Person")
    ]

    return {
        "bill_id":          bill.get("Id"),
        "bill_name":        bill.get("Name"),
        "bill_number":      bill.get("Number"),
        "knesset_num":      bill.get("KnessetNum"),
        "type":             bill.get("TypeDesc"),
        "sub_type":         bill.get("SubTypeDesc"),
        "status":           (bill.get("KNS_Status") or {}).get("Desc"),
        "committee_id":     bill.get("CommitteeID"),
        "publication_date": bill.get("PublicationDate"),
        "last_updated":     bill.get("LastUpdatedDate"),
        "initiators":       initiators,
        "documents":        get_bill_documents(bill_id),
    }


def get_bill_details_by_name(bill_name: str) -> dict | None:
    bill = get_law_or_bill_by_name(bill_name)
    if not bill:
        return None
    return get_bill_details(bill["bill_id"])


def get_bill_text_by_name(bill_name: str, knesset_num: int = 25, max_chars: int = 8000) -> dict | None:
    bill = get_law_or_bill_by_name(bill_name, knesset_num)
    if not bill:
        return None
    return get_bill_text(bill["bill_id"], max_chars)


def get_active_committee_members_by_name(name: str, knesset_num: int = 25) -> list[dict]:
    committees = get_committee_by_name(name, knesset_num)
    if not committees:
        return []
    committee = committees[0]  # take the best match
    return get_active_committee_members(committee["CommitteeID"], knesset_num)


# ── Committee sessions ────────────────────────────────────────────────────────

def get_committee_sessions(committee_id: int, knesset_num: int = 25) -> list[dict]:
    """
    List ALL sessions for a committee from OData KNS_CommitteeSession, newest first.
    Uses explicit $skip pagination (API caps at 100 per page).
    Returns list of {session_id, date, committee_id, knesset_num, type_id, status_id, note}.
    Check type_id against SESSION_TYPE_CLASSIFIED to skip classified sessions.
    """
    url       = f"{OFFICIAL_KNESSET_NEW_API}/KNS_CommitteeSession"
    page_size = 100
    all_sessions: list[dict] = []

    for offset in range(0, 100_000, page_size):
        r = requests.get(url, params={
            "$filter":  f"CommitteeID eq {committee_id} and KnessetNum eq {knesset_num}",
            "$select":  "Id,CommitteeID,KnessetNum,StartDate,Note,TypeID,StatusID",
            "$orderby": "StartDate desc",
            "$top":     page_size,
            "$skip":    offset,
        }, timeout=TIMEOUT)
        r.raise_for_status()
        page = r.json().get("value", [])
        all_sessions.extend(page)
        if len(page) < page_size:
            break

    return [
        {
            "session_id":   s["Id"],
            "date":         (s.get("StartDate") or "")[:10],  # YYYY-MM-DD
            "committee_id": s.get("CommitteeID"),
            "knesset_num":  s.get("KnessetNum"),
            "type_id":      s.get("TypeID"),
            "status_id":    s.get("StatusID"),
            "note":         s.get("Note"),
        }
        for s in all_sessions
    ]


def get_committee_sessions_by_name(name: str, knesset_num: int = 25) -> list[dict]:
    """Resolve committee by name, then return its sessions."""
    committees = get_committee_by_name(name, knesset_num)
    if not committees:
        return []
    return get_committee_sessions(committees[0]["CommitteeID"], knesset_num)


# ── Session documents & transcripts ──────────────────────────────────────────

def _extract_docx_text(docx_bytes: bytes) -> str:
    """Extract text from a .docx file using python-docx."""
    try:
        doc = docx.Document(io.BytesIO(docx_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs).strip()
    except Exception:
        return ""


def _extract_doc_text(doc_bytes: bytes) -> str:
    """
    Extract text from a binary .doc (OLE) file using Word COM automation.
    Windows only; requires Microsoft Word installed.
    Note: writes to ~/.knesset_doc_tmp/ — NOT %TEMP%, which triggers Protected View.
    """
    if not _WORD_COM_AVAILABLE:
        return ""
    tmp_dir  = os.path.join(os.path.expanduser("~"), ".knesset_doc_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    doc_path = os.path.join(tmp_dir, "document.doc")
    word = None
    com_doc = None
    try:
        with open(doc_path, "wb") as f:
            f.write(doc_bytes)
        word    = _win32com.Dispatch("Word.Application")
        word.Visible = False
        com_doc = word.Documents.Open(doc_path, ReadOnly=True)
        text    = com_doc.Content.Text
        return text.strip()
    except Exception:
        return ""
    finally:
        if com_doc:
            try: com_doc.Close(False)
            except Exception: pass
        if word:
            try: word.Quit()
            except Exception: pass
        try: os.unlink(doc_path)
        except Exception: pass


def get_session_documents(session_id: int) -> list[dict]:
    """Return document metadata for a session from KNS_DocumentCommitteeSession."""
    r = requests.get(
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_DocumentCommitteeSession",
        params={"$filter": f"CommitteeSessionID eq {session_id}", "$top": 20},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [
        {
            "doc_id":     doc["Id"],
            "session_id": session_id,
            "name":       doc.get("DocumentName"),
            "format":     doc.get("ApplicationDesc"),
            "url":        _fix_file_path(doc["FilePath"]),
        }
        for doc in r.json().get("value", [])
        if doc.get("FilePath")
    ]


def get_session_protocol_text(session_id: int, max_chars: int | None = None) -> dict | None:
    """
    Download and extract the protocol document for a session.
    Only considers documents whose name contains 'פרוטוקול' or 'protocol' —
    background documents (bills, appendices) are excluded.
    Returns {session_id, doc_id, name, url, text} or None.
    """
    docs = get_session_documents(session_id)
    candidates = [
        d for d in docs
        if d["name"] and any(p in d["name"] for p in _PROTOCOL_NAME_SUBSTRINGS)
    ]
    if not candidates:
        return None

    for doc in candidates:
        fmt = (doc["format"] or "").lower()
        if fmt not in ("pdf", "word", "doc", "docx"):
            continue
        try:
            response = requests.get(doc["url"], timeout=TIMEOUT)
            response.raise_for_status()
            if fmt == "pdf":
                text = _extract_pdf_text(response.content)
            elif doc["url"].lower().endswith(".doc"):
                text = _extract_doc_text(response.content)
            else:
                text = _extract_docx_text(response.content)
            if not text:
                continue
            if max_chars:
                text = text[:max_chars]
            return {"session_id": session_id, "doc_id": doc["doc_id"],
                    "name": doc["name"], "url": doc["url"], "text": text}
        except Exception:
            continue
    return None


def get_session_transcript(session_id: int) -> dict | None:
    """
    Download a session transcript, trying oknesset.org first, then OData PDF/Word.

    Returns:
      {"speeches": [...]}                        — structured, from oknesset.org
      {"full_text": "...", "source_url": "..."}  — raw text, from OData document
      None                                       — no transcript available
    """
    speeches = scrape_oknesset_transcript(session_id)
    if speeches:
        return {"speeches": speeches}

    result = get_session_protocol_text(session_id)
    if result and result.get("text"):
        return {"full_text": result["text"], "source_url": result["url"]}

    return None


def scrape_oknesset_transcript(session_id: int) -> list[dict] | None:
    """
    Scrape the speech-by-speech transcript from oknesset.org.
    URL: https://oknesset.org/meetings/{s[0]}/{s[1]}/{session_id}.html

    Returns list of {speaker, text_he}, or None if unavailable / no protocol marker.
    """
    s   = str(session_id)
    url = f"https://oknesset.org/meetings/{s[0]}/{s[1]}/{session_id}.html"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(r.content, "html.parser")

    # Explicit "no protocol" marker
    if soup.select_one("ul[data-noprotocol]"):
        return None

    speeches = []
    for div in soup.select("div.speech-container"):
        speaker_el = div.select_one("div.text-speaker")
        content_el = div.select_one("blockquote.entry-content")
        if speaker_el and content_el:
            speaker = speaker_el.get_text().replace("¶", "").strip()
            text_he = content_el.get_text().replace("¶", "").strip()
            if speaker or text_he:
                speeches.append({"speaker": speaker, "text_he": text_he})

    return speeches if speeches else None


# ── Voting data ───────────────────────────────────────────────────────────────

def _fetch_votes_metadata(vote_ids: list[int]) -> dict[int, dict]:
    """Batch-fetch KNS_PlenumVote records. Returns dict keyed by vote Id."""
    if not vote_ids:
        return {}
    filter_expr = " or ".join(f"Id eq {vid}" for vid in vote_ids)
    r = requests.get(
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_PlenumVote",
        params={"$filter": filter_expr},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return {v["Id"]: v for v in r.json().get("value", [])}


def _format_vote_with_result(vote_meta: dict, result_desc: str) -> dict:
    return {
        "vote_id":          vote_meta.get("Id"),
        "vote_title":       vote_meta.get("VoteTitle"),
        "vote_subject":     vote_meta.get("VoteSubject"),
        "vote_datetime":    vote_meta.get("VoteDateTime"),
        "vote_method":      vote_meta.get("VoteMethodDesc"),
        "is_no_confidence": vote_meta.get("IsNoConfidenceInGov"),
        "result":           result_desc,
    }


def _mk_name_filter(mk: dict) -> str:
    """
    Build an OData filter string to match KNS_PlenumVoteResult rows for an MK.
    Uses LastName from voting results (= mk_individual_name from oknesset).
    Adds FirstName clause when available for disambiguation.
    """
    last  = (mk.get("mk_individual_name") or "").strip().replace("'", "''")
    first = (mk.get("mk_individual_first_name") or "").strip().replace("'", "''")
    if first:
        return f"LastName eq '{last}' and FirstName eq '{first}'"
    return f"LastName eq '{last}'"


def get_mk_votes(mk_name: str, knesset_num: int = 25, top_n: int = 20) -> list[dict]:
    """
    Return the most recent `top_n` plenum votes for a named MK with their result.
    Each entry: vote_id, vote_title, vote_subject, vote_datetime, vote_method, result.
    Returns [] if the MK is not found or has no voting record.
    """
    mk = get_mk_profile(mk_name, knesset_num)
    if not mk:
        return []

    r = requests.get(
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_PlenumVoteResult",
        params={
            "$filter":  _mk_name_filter(mk),
            "$top":     top_n,
            "$orderby": "Id desc",
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    results = r.json().get("value", [])

    vote_ids = list({row["VoteID"] for row in results if row.get("VoteID")})
    votes_by_id = _fetch_votes_metadata(vote_ids)

    return [
        _format_vote_with_result(votes_by_id[row["VoteID"]], row.get("ResultDesc", ""))
        for row in results
        if row.get("VoteID") in votes_by_id
    ]


def get_votes_on_topic(topic: str, top_n: int = 20) -> list[dict]:
    """
    Search plenum votes whose title or subject contains `topic`.
    Returns up to `top_n` most recent matches.
    Each entry: vote_id, vote_title, vote_subject, vote_datetime, vote_method, is_no_confidence.
    """
    safe = topic.replace("'", "''")
    filter_expr = f"contains(VoteTitle,'{safe}') or contains(VoteSubject,'{safe}')"
    r = requests.get(
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_PlenumVote",
        params={"$filter": filter_expr, "$top": top_n, "$orderby": "Id desc"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [
        {
            "vote_id":          v["Id"],
            "vote_title":       v.get("VoteTitle"),
            "vote_subject":     v.get("VoteSubject"),
            "vote_datetime":    v.get("VoteDateTime"),
            "vote_method":      v.get("VoteMethodDesc"),
            "is_no_confidence": v.get("IsNoConfidenceInGov"),
        }
        for v in r.json().get("value", [])
    ]


def get_votes_on_topic_by_mk(
    topic: str,
    mk_name: str,
    knesset_num: int = 25,
    top_n: int = 20,
) -> list[dict]:
    """
    Search plenum votes on a topic and show how a specific MK voted on each.
    Combines get_votes_on_topic + per-MK result lookup.
    Each entry: vote_id, vote_title, vote_subject, vote_datetime, vote_method, result.
    """
    votes = get_votes_on_topic(topic, top_n=top_n)
    if not votes:
        return []

    mk = get_mk_profile(mk_name, knesset_num)
    if not mk:
        return []

    vote_ids = [v["vote_id"] for v in votes]
    votes_by_id = {v["vote_id"]: v for v in votes}

    id_filter = " or ".join(f"VoteID eq {vid}" for vid in vote_ids)
    r = requests.get(
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_PlenumVoteResult",
        params={"$filter": f"{_mk_name_filter(mk)} and ({id_filter})"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()

    result_by_vote: dict[int, str] = {
        row["VoteID"]: row.get("ResultDesc", "")
        for row in r.json().get("value", [])
        if row.get("VoteID")
    }

    return [
        {**v, "result": result_by_vote.get(v["vote_id"], "לא הצביע")}
        for v in votes
    ]


def get_recent_votes(top_n: int = 10) -> list[dict]:
    """
    Return the `top_n` most recent plenum votes (no MK filter).
    Each entry: vote_id, vote_title, vote_subject, vote_datetime, vote_method, is_no_confidence.
    """
    r = requests.get(
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_PlenumVote",
        params={"$orderby": "Id desc", "$top": top_n},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return [
        {
            "vote_id":          v["Id"],
            "vote_title":       v.get("VoteTitle"),
            "vote_subject":     v.get("VoteSubject"),
            "vote_datetime":    v.get("VoteDateTime"),
            "vote_method":      v.get("VoteMethodDesc"),
            "is_no_confidence": v.get("IsNoConfidenceInGov"),
        }
        for v in r.json().get("value", [])
    ]