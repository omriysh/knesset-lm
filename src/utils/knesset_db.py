"""
knesset_db.py

Data access layer for Knesset member data.
Uses the backend.oknesset.org REST API for member identity (ids, altnames) and
the official Knesset OData v4 service for Knesset membership and factions —
oknesset currently returns ``factions: [null]`` for every member, and only the
OData positions table covers ministers and Norwegian-law replacements.

API endpoints:
    https://backend.oknesset.org/members?is_current=true   — current MKs
    https://backend.oknesset.org/members?is_current=false  — former MKs
    https://knesset.gov.il/OdataV4/ParliamentInfo/KNS_PersonToPosition — roster

Each member record contains:
    mk_individual_id, mk_individual_first_name, mk_individual_name,
    PersonID, IsCurrent, altnames,
    factions        — list of {faction_id, faction_name, start_date, finish_date, knesset}
    committee_positions — list of committee roles
    faction_chairpersons — list of faction chair periods
    govministries   — list of {govministry_name, position_name, start_date, finish_date, knesset}

Usage:
    from utils.knesset_db import get_all_mks, get_all_parties
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import requests
from urllib.parse import quote, urlsplit
from functools import lru_cache
import io
import pdfplumber
import fitz  # pymupdf
import re
import docx          # python-docx
from bs4 import BeautifulSoup

try:
    import win32com.client
    _WORD_COM_AVAILABLE = True
except ImportError as exc:
    print(f"[knesset_db] pywin32 is not installed; legacy .doc files will not be extracted ({exc})")
    _WORD_COM_AVAILABLE = False

import config as _config
from utils.cache import SESSION as HTTP_SESSION
from utils.tool_helpers.filter_resolution import matching_party_names

OKNESSET_API = "https://backend.oknesset.org"
OFFICIAL_KNESSET_NEW_API = "https://knesset.gov.il/OdataV4/ParliamentInfo"
ODATA_PAGE_SIZE = 100  # the OData service silently caps $top at 100

SESSION_TYPE_CLASSIFIED  = 160  # חסויה — classified session; no public transcript
_PROTOCOL_NAME_SUBSTRINGS = ("פרוטוקול", "protocol")

_DOC_GROUP_PRIORITY = [
    "הצעת חוק לקריאה השנייה והשלישית",
    "הצעת חוק לקריאה הראשונה",
    "טקסט חוק מאוחד",
    "חוק - פרסום ברשומות",
]


# ── Retry helper ─────────────────────────────────────────────────────────────

def _retry_get(url: str, *, cached: bool = True, **kwargs) -> requests.Response:
    """
    GET through the disk-cached HTTP_SESSION (plain requests when cached=False, for large
    documents) with retry on transient network errors.
    Retries on ConnectionError, Timeout, and HTTP 5xx.
    4xx responses are returned as-is for the caller to handle.
    """
    http = HTTP_SESSION if cached else requests
    attempts = _config.API_RETRY_ATTEMPTS
    sleep    = _config.API_RETRY_SLEEP
    last_exc: BaseException | None = None

    for i in range(attempts):
        try:
            r = http.get(url, **kwargs)
            if r.status_code < 500:
                return r
            last_exc = requests.exceptions.HTTPError(
                f"HTTP {r.status_code}", response=r
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_exc = e

        if i < attempts - 1:
            print(
                f"[knesset_db] request failed (url: {url}, error: {last_exc}), "
                f"retrying in {sleep}s … ({i + 1}/{attempts})",
                flush=True,
            )
            time.sleep(sleep)

    raise last_exc  # type: ignore[misc]


# ── Helpers ───────────────────────────────────────────────────────────────────

@lru_cache(maxsize=2)
def _fetch_members(is_current: bool) -> list[dict]:
    """
    Fetch all members from the oknesset API.
    Cached per session — current and former are cached separately.
    """
    url    = f"{OKNESSET_API}/members"
    params = {"is_current": "true" if is_current else "false"}
    response = _retry_get(url, params=params, timeout=_config.HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


@lru_cache(maxsize=4)
def _fetch_person_positions(knesset_num: int) -> tuple[dict, ...]:
    """
    Every KNS_PersonToPosition row of a given Knesset, with KNS_Person expanded.
    This is the authoritative roster source: it covers plain MKs, ministers,
    deputy ministers and Norwegian-law replacements, which the oknesset
    /members payload does not reliably expose.
    """
    url  = f"{OFFICIAL_KNESSET_NEW_API}/KNS_PersonToPosition"
    rows: list[dict] = []
    skip = 0
    while True:
        params = {
            "$filter":  f"KnessetNum eq {knesset_num}",
            "$expand":  "KNS_Person",
            "$orderby": "Id asc",
            "$top":     ODATA_PAGE_SIZE,
            "$skip":    skip,
        }
        response = _retry_get(url, params=params, timeout=_config.HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        page = response.json().get("value", [])
        if not page:
            break
        rows.extend(page)
        skip += len(page)
    return tuple(rows)


@lru_cache(maxsize=1)
def _position_names() -> dict[int, str]:
    """KNS_Position Id -> Hebrew description (חבר ועדה, יו"ר סיעה, שר …)."""
    response = _retry_get(f"{OFFICIAL_KNESSET_NEW_API}/KNS_Position", params={"$top": ODATA_PAGE_SIZE}, timeout=_config.HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return {row["Id"]: (row.get("Description") or "").strip() for row in response.json().get("value", [])}


_FACTION_CHAIR_POSITION_ID = 48
_PLAIN_MEMBERSHIP_POSITION_IDS = {43, 61}  # חבר / חברת הכנסת


def get_mk_positions(person_id, knesset_num: int) -> dict:
    """
    An MK's positions in one Knesset from OData KNS_PersonToPosition, grouped as
    factions / committee_positions / govministries / faction_chairpersons /
    knesset_roles (PM, speaker, coalition/opposition head …). oknesset ships
    these lists as ``[null]``, so OData is the only source.
    """
    try:
        position_names = _position_names()
    except Exception as exc:
        print(f"[knesset_db] KNS_Position fetch failed ({exc}); position names will be missing", flush=True)
        position_names = {}
    rows = sorted((row for row in _fetch_person_positions(knesset_num) if str(row.get("PersonID")) == str(person_id)),
                  key=lambda row: row.get("StartDate") or "")
    grouped: dict[str, list[dict]] = {"factions": [], "committee_positions": [], "govministries": [],
                                      "faction_chairpersons": [], "knesset_roles": []}
    for row in rows:
        position = (row.get("DutyDesc") or "").strip() or position_names.get(row.get("PositionID"), "")
        period = {"start_date": row.get("StartDate"), "finish_date": row.get("FinishDate"),
                  "is_current": row.get("IsCurrent"), "knesset": knesset_num}
        if row.get("CommitteeName"):
            grouped["committee_positions"].append({"committee_id": row.get("CommitteeID"),
                                                   "committee_name": row["CommitteeName"].strip(),
                                                   "position": position, **period})
        elif row.get("GovMinistryName"):
            grouped["govministries"].append({"govministry_name": row["GovMinistryName"].strip(),
                                             "position_name": position, **period})
        elif row.get("PositionID") == _FACTION_CHAIR_POSITION_ID:
            grouped["faction_chairpersons"].append({"faction_name": (row.get("FactionName") or "").strip(), **period})
        elif row.get("FactionName"):
            grouped["factions"].append({"faction_id": row.get("FactionID"),
                                        "faction_name": row["FactionName"].strip(), **period})
        elif row.get("PositionID") not in _PLAIN_MEMBERSHIP_POSITION_IDS:
            grouped["knesset_roles"].append({"position": position, **period})
    grouped["factions"] = _dedupe_factions(grouped["factions"])
    return {group: list({tuple(sorted(entry.items())): entry for entry in entries}.values())
            for group, entries in grouped.items()}


def mk_full_name(record: dict) -> str:
    first = (record.get("mk_individual_first_name") or "").strip()
    last  = (record.get("mk_individual_name") or "").strip()
    return record.get("full_name") or f"{first} {last}".strip()


def _dedupe_factions(factions: list[dict]) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for faction in factions:
        key = (faction.get("faction_id"), faction.get("faction_name"), faction.get("start_date"))
        seen.setdefault(key, faction)
    return list(seen.values())


def _odata_roster(knesset_num: int) -> dict[int, dict]:
    """
    Build ``PersonID -> partial member record`` from the official OData positions
    table. Faction records are shaped like the oknesset ones so that
    ``_most_recent_faction`` and every downstream consumer keep working.
    """
    try:
        rows = _fetch_person_positions(knesset_num)
    except Exception as exc:
        print(
            f"[knesset_db] OData roster fetch failed for knesset {knesset_num} ({exc}); "
            f"falling back to the oknesset roster only",
            flush=True,
        )
        return {}

    roster: dict[int, dict] = {}
    for row in rows:
        person_id = row.get("PersonID")
        if person_id is None:
            continue
        person = row.get("KNS_Person") or {}
        entry  = roster.setdefault(person_id, {
            "PersonID":                 person_id,
            "mk_individual_first_name": (person.get("FirstName") or "").strip(),
            "mk_individual_name":       (person.get("LastName") or "").strip(),
            "mk_individual_email":      person.get("Email"),
            "GenderID":                 person.get("GenderID"),
            "GenderDesc":               person.get("GenderDesc"),
            "IsCurrent":                bool(person.get("IsCurrent")),
            "factions":                 [],
        })
        faction_name = (row.get("FactionName") or "").strip()
        if faction_name:
            entry["factions"].append({
                "faction_id":   row.get("FactionID"),
                "faction_name": faction_name,
                "start_date":   row.get("StartDate"),
                "finish_date":  row.get("FinishDate"),
                "knesset":      knesset_num,
            })

    for entry in roster.values():
        entry["factions"] = _dedupe_factions(entry["factions"])

    if not roster:
        print(
            f"[knesset_db] OData KNS_PersonToPosition returned no person for knesset "
            f"{knesset_num} — the roster will be incomplete",
            flush=True,
        )
    return roster


def _get_all_members_raw(knesset_num: int = 25) -> list[dict]:
    """
    Return all members (current + former) who served in a given Knesset.

    The oknesset ``/members`` payload currently ships ``factions: [null]`` for
    every member, so filtering on it alone yields an empty roster. The official
    OData positions table is merged in as the authoritative membership source,
    while the oknesset records still supply ``mk_individual_id`` and altnames.
    """
    current = _fetch_members(True)
    former  = _fetch_members(False)
    all_members = current + former

    if not all_members:
        print(
            f"[knesset_db] oknesset /members returned no member at all "
            f"(current={len(current)}, former={len(former)})",
            flush=True,
        )

    if knesset_num is None:
        return all_members

    oknesset_by_person_id: dict[int, dict] = {}
    for mk in all_members:
        person_id = mk.get("PersonID")
        if person_id is not None:
            oknesset_by_person_id.setdefault(person_id, mk)

    result: list[dict] = []
    seen_person_ids: set[int] = set()
    for mk in all_members:
        factions = [f for f in (mk.get("factions") or []) if f and f.get("knesset") == knesset_num]
        if factions:
            result.append(mk)
            if mk.get("PersonID") is not None:
                seen_person_ids.add(mk["PersonID"])

    if not result:
        print(
            f"[knesset_db] no oknesset member carries a faction record for knesset "
            f"{knesset_num} — relying entirely on the official OData roster",
            flush=True,
        )

    for person_id, odata_entry in _odata_roster(knesset_num).items():
        if person_id in seen_person_ids:
            continue
        oknesset_record = oknesset_by_person_id.get(person_id)
        if oknesset_record is None:
            member = dict(odata_entry)
            member["mk_individual_id"]     = person_id
            member["altnames"]             = []
            member["committee_positions"]  = []
            member["faction_chairpersons"] = []
            member["govministries"]        = []
        else:
            member = dict(oknesset_record)
            member["factions"] = odata_entry["factions"]
            for field in ("mk_individual_first_name", "mk_individual_name"):
                if not (member.get(field) or "").strip():
                    member[field] = odata_entry[field]
        result.append(member)
        seen_person_ids.add(person_id)

    if not result:
        print(
            f"[knesset_db] EMPTY roster for knesset {knesset_num} — both the oknesset "
            f"REST API and the official OData endpoint yielded nothing. Any index "
            f"rebuilt from this roster would be empty; aborting is strongly advised.",
            flush=True,
        )
    return result


def mk_name_variants(first_name: str, last_name: str) -> list[str]:
    """
    Plausible written forms of an MK name, for alias-style retrieval.
    Rosters store the full legal name ("אביחי אברהם בוארון", "ששון ששי גואטה")
    while protocols usually print a shorter everyday form.
    """
    first  = " ".join((first_name or "").split())
    last   = " ".join((last_name or "").split())
    if not first and not last:
        return []

    variants = [f"{first} {last}".strip()]
    first_tokens = first.split()
    if len(first_tokens) > 1 and last:
        variants.append(f"{first_tokens[0]} {last}")
        variants.append(f"{first_tokens[-1]} {last}")
    return list(dict.fromkeys(v for v in variants if v))


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
    for page_number in range(min(len(doc), _config.BILL_PDF_MAX_PAGES)):
        text = doc[page_number].get_text("text")
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
        for page in pdf.pages[:_config.BILL_PDF_MAX_PAGES]:
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
    except Exception as exc:
        print(f"[knesset_db] PyMuPDF extraction failed ({exc}); trying pdfplumber", flush=True)

    # Fall back to pdfplumber
    try:
        text = _extract_pdf_text_pdfplumber(pdf_bytes)
        if text and not _is_garbage(text):
            return text
    except Exception as exc:
        print(f"[knesset_db] pdfplumber extraction failed ({exc}); returning empty", flush=True)

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
        "name":             bill.get("Name"),
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


def search_bills_page(
    search_term: str,
    knesset_num: int | None,
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[dict], int | None]:
    """
    Raw KNS_Bill dicts whose name contains `search_term` (already OData-escaped), most
    recently updated first, in one Knesset or all when knesset_num is None.
    Returns (rows [offset, offset+limit), total match count).
    """
    filter_expr = f"contains(Name,'{search_term}')"
    if knesset_num is not None:
        filter_expr += f" and KnessetNum eq {int(knesset_num)}"
    params = {
        "$filter":  filter_expr,
        "$expand":  "KNS_Status,KNS_BillInitiator($expand=KNS_Person)",
        "$orderby": "LastUpdatedDate desc,Id desc",
    }
    return odata_page("KNS_Bill", params, offset, limit)


# ── Public API ────────────────────────────────────────────────────────────────

def get_all_mks(knesset_num: int = 25) -> list[dict]:
    """
    Return all MKs who served in a given Knesset, sorted by last name.
    Each entry contains: mk_id, full_name, party, is_current, email.
    """
    members = _get_all_members_raw(knesset_num)
    result  = list(members)
    result.sort(key=lambda x: x.get("mk_individual_name") or "")
    return result


def mk_roster_rows(knesset_num: int) -> list[dict]:
    """
    One row per MK of a Knesset, shaped like the knesset.db ``mks`` table:
    mk_id (oknesset mk_individual_id, else PersonID), knesset_num, first_name, last_name,
    full_name, party (latest faction), aliases (" | "-joined name variants and altnames).
    """
    rows = []
    for mk in get_all_mks(knesset_num):
        mk_id = str(mk.get("mk_individual_id") or mk.get("PersonID") or "")
        first = (mk.get("mk_individual_first_name") or "").strip()
        last  = (mk.get("mk_individual_name") or "").strip()
        full  = f"{first} {last}".strip()
        if not mk_id or not full:
            continue
        faction = _most_recent_faction([f for f in (mk.get("factions") or []) if f], knesset_num)
        aliases = mk_name_variants(first, last) + [a for a in (mk.get("altnames") or []) if a]
        rows.append({
            "mk_id": mk_id, "knesset_num": knesset_num, "first_name": first, "last_name": last,
            "full_name": full, "party": (faction or {}).get("faction_name", "").strip() or None,
            "aliases": " | ".join(dict.fromkeys(a for a in aliases if a and a != full)),
        })
    return rows


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


def get_party_members(party_query: str, knesset_num: int = 25, top_k: int = 3) -> list[dict]:
    """
    The party party_query names in a given Knesset (filter_resolution.matching_party_names: exact /
    config.PARTY_ALIASES / unambiguous fuzzy, else every party holding all query words; never a
    near-miss). Returns up to top_k matches, each with:
      {party, score, mk_count, members: [{mk_id, full_name, is_current}]}
    """
    parties = get_all_parties(knesset_num)
    if not parties:
        return []

    top_matches = matching_party_names(party_query, [p["party"] for p in parties])[:top_k]

    members_raw = _get_all_members_raw(knesset_num)
    results: list[dict] = []
    for party_name, score in top_matches:
        mk_count = next((p["mk_count"] for p in parties if p["party"] == party_name), 0)
        members: list[dict] = []
        for mk in members_raw:
            faction = _most_recent_faction(
                [f for f in (mk.get("factions") or []) if f],
                knesset_num,
            )
            if faction and faction["faction_name"].strip() == party_name:
                members.append({
                    "mk_id":      str(mk.get("mk_individual_id") or ""),
                    "full_name":  f"{mk.get('mk_individual_first_name', '')} {mk.get('mk_individual_name', '')}".strip() or mk.get("mk_individual_name") or "",
                    "is_current": bool(mk.get("IsCurrent", False)),
                })
        results.append({
            "party":    party_name,
            "score":    round(score, 2),
            "mk_count": mk_count,
            "members":  sorted(members, key=lambda m: m.get("full_name", "")),
        })
    return results


def get_all_committees(knesset_num: int = 25) -> list[dict]:
    """
    Return all committees for a given Knesset number, sorted by name.
    Each entry: {CommitteeID, Name, KnessetNum, IsCurrent}.
    """
    url      = f"{OKNESSET_API}/committees_kns_committee/list"
    response = _retry_get(url, params={"KnessetNum": knesset_num, "limit": 1000}, timeout=_config.HTTP_TIMEOUT_SECONDS)
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


def _get_active_committee_members_by_id(
    committee_id: int,
    knesset_num: int = 25,
    current_only: bool = True,
) -> list[dict]:
    """
    Search for MKs the are a part of a certain committee in a given time.
    Returns a list of (partial) MK dicts.
    """
    filter_expr = f"CommitteeID eq {committee_id} and KnessetNum eq {knesset_num}"
    if current_only:
        filter_expr += " and IsCurrent eq true"

    roster_rows = odata_all_rows("KNS_PersonToPosition", {
        "$expand":  "KNS_Person,KNS_Position",
        "$filter":  filter_expr,
        "$orderby": "Id",
    })

    seen: dict[int, dict] = {}
    for row in roster_rows:
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


def _get_bill_documents(bill_id: int) -> list[dict]:
    """
    Return document metadata for a bill: title, type, and corrected URL.
    Sorted by usefulness (latest reading first).
    """
    docs = odata_all_rows("KNS_DocumentBill", {"$filter": f"BillID eq {int(bill_id)}", "$orderby": "Id"})

    def _priority(doc):
        desc = doc.get("GroupTypeDesc", "")
        return _DOC_GROUP_PRIORITY.index(desc) if desc in _DOC_GROUP_PRIORITY else len(_DOC_GROUP_PRIORITY)

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


def _is_knesset_document_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    suffix = _config.BILL_DOCUMENT_HOST_SUFFIX
    return (parts.scheme == "https" and not parts.username and not parts.port
            and (host == suffix or host.endswith("." + suffix)))


def _download_bill_document(url: str) -> bytes | None:
    """Bill document bytes, or None when the URL is not an https Knesset URL or the file exceeds
    BILL_PDF_MAX_BYTES (checked on Content-Length and again while streaming)."""
    if not _is_knesset_document_url(url):
        print(f"[knesset_db] refusing bill document outside {_config.BILL_DOCUMENT_HOST_SUFFIX}: {url!r}", flush=True)
        return None
    max_bytes = _config.BILL_PDF_MAX_BYTES
    response = _retry_get(url, cached=False, timeout=_config.HTTP_TIMEOUT_SECONDS, stream=True)
    try:
        response.raise_for_status()
        final_url = getattr(response, "url", None) or url
        if not _is_knesset_document_url(final_url):
            print(f"[knesset_db] bill document {url!r} redirected outside the Knesset domain: {final_url!r}", flush=True)
            return None
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > max_bytes:
            print(f"[knesset_db] bill document {url!r} too large ({declared} bytes)", flush=True)
            return None
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                print(f"[knesset_db] bill document {url!r} exceeds {max_bytes} bytes", flush=True)
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        response.close()


def _get_bill_text_by_id(bill_id: int, max_chars: int = 8000, text_offset: int = 0) -> dict | None:
    """
    Fetch the most relevant document for a bill and extract its text.
    Tries documents in priority order until one succeeds.
    Returns {bill_id, doc_id, group, url, text, text_chars, truncated} or None if all fail:
    text is characters [text_offset, text_offset + max_chars) of the document text, text_chars is
    [start, end, total] and truncated says the text continues after end.
    """
    docs = _get_bill_documents(bill_id)
    if not docs:
        return None

    for doc in docs:
        if doc["format"] not in ("PDF",):
            continue
        try:
            pdf_bytes = _download_bill_document(doc["url"])
            if not pdf_bytes:
                continue
            full_text = _extract_pdf_text(pdf_bytes)
            if not full_text:
                continue  # try next doc

            text_start = min(text_offset, len(full_text))
            text_end = min(text_start + max_chars, len(full_text))
            return {
                "bill_id":    bill_id,
                "doc_id":     doc["doc_id"],
                "group":      doc["group"],
                "url":        doc["url"],
                "text":       full_text[text_start:text_end],
                "text_chars": [text_start, text_end, len(full_text)],
                "truncated":  text_end < len(full_text),
            }

        except Exception as exc:
            print(f"[knesset_db] _get_bill_text_by_id: doc {doc.get('url')!r} failed ({exc}); trying next", flush=True)
            continue

    return None


def _get_bill_details_by_id(bill_id: int) -> dict | None:
    # Request 1: bill + status only (no nested expand)
    url = (
        f"{OFFICIAL_KNESSET_NEW_API}/KNS_Bill({bill_id})"
        f"?$expand=KNS_Status"
    )
    r = _retry_get(url, timeout=_config.HTTP_TIMEOUT_SECONDS)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    bill = r.json()

    # Request 2: initiators separately
    initiator_rows = odata_all_rows("KNS_BillInitiator", {
        "$filter": f"BillID eq {int(bill_id)}", "$expand": "KNS_Person", "$orderby": "Ordinal,Id"})
    initiators = [
        {
            "person_id": bi["KNS_Person"]["Id"],
            "full_name": f"{bi['KNS_Person'].get('FirstName', '')} {bi['KNS_Person'].get('LastName', '')}".strip(),
        }
        for bi in initiator_rows
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
        "documents":        _get_bill_documents(bill_id),
    }


# ── Committee sessions ────────────────────────────────────────────────────────

def get_committee_sessions(committee_id: int, knesset_num: int = 25) -> list[dict]:
    """
    List ALL sessions for a committee from OData KNS_CommitteeSession, newest first.
    Uses $count=true on first request to determine total, then paginates exactly
    as many times as needed (avoids blind range-based pagination up to 100K).
    Returns list of {session_id, date, committee_id, knesset_num, type_id, status_id, note}.
    Check type_id against SESSION_TYPE_CLASSIFIED to skip classified sessions.
    """
    url       = f"{OFFICIAL_KNESSET_NEW_API}/KNS_CommitteeSession"
    page_size = 100
    base_params = {
        "$filter":  f"CommitteeID eq {committee_id} and KnessetNum eq {knesset_num}",
        "$select":  "Id,CommitteeID,KnessetNum,StartDate,Note,TypeID,StatusID",
        "$orderby": "StartDate desc",
        "$top":     page_size,
    }

    # First request: get count + first page in one shot
    r = _retry_get(url, params={**base_params, "$count": "true"}, timeout=_config.HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()
    data  = r.json()
    total = data.get("@odata.count", 0)
    all_sessions: list[dict] = list(data.get("value", []))

    # Fetch remaining pages (exactly ceil(total/page_size) - 1 more requests)
    for offset in range(page_size, total, page_size):
        r = _retry_get(url, params={**base_params, "$skip": offset}, timeout=_config.HTTP_TIMEOUT_SECONDS)
        r.raise_for_status()
        all_sessions.extend(r.json().get("value", []))

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


# ── Session documents & transcripts ──────────────────────────────────────────

def _extract_docx_text(docx_bytes: bytes) -> str:
    """Extract text from a .docx file using python-docx."""
    try:
        doc = docx.Document(io.BytesIO(docx_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs).strip()
    except Exception as exc:
        print(f"[knesset_db] _extract_docx_text failed ({exc})", flush=True)
        return ""


class WordExtractionError(RuntimeError):
    """A .doc could not be read through Word (timeout or Word error); the download should retry it."""


_WORD_WORKER_SCRIPT = Path(__file__).with_name("word_doc_worker.py")
_WORD_IMAGE_NAME = "winword.exe"


def _word_worker_command(doc_path: str, text_path: str, word_pid_path: str) -> list[str]:
    return [sys.executable, str(_WORD_WORKER_SCRIPT), doc_path, text_path, word_pid_path]


def _kill_word_instance(word_pid: int) -> None:
    """Terminate the process `word_pid` only if it is WINWORD.EXE."""
    import win32api
    import win32con
    import win32process
    try:
        process_handle = win32api.OpenProcess(
            win32con.PROCESS_TERMINATE | win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
            False, word_pid)
    except Exception as exc:
        print(f"[knesset_db] Word PID {word_pid} is already gone ({exc})", flush=True)
        return
    try:
        image_path = win32process.GetModuleFileNameEx(process_handle, 0)
        if os.path.basename(image_path).lower() != _WORD_IMAGE_NAME:
            print(f"[knesset_db] PID {word_pid} is {image_path}, not Word; not killing it", flush=True)
            return
        win32api.TerminateProcess(process_handle, 1)
        print(f"[knesset_db] killed the extraction's Word instance (PID {word_pid})", flush=True)
    except Exception as exc:
        print(f"[knesset_db] killing Word PID {word_pid} failed ({exc})", flush=True)
    finally:
        win32api.CloseHandle(process_handle)


def _remove_file_if_exists(path: str) -> None:
    try:
        if os.path.exists(path):
            os.unlink(path)
    except Exception as exc:
        print(f"[knesset_db] deleting temp file {path} failed ({exc})", flush=True)


def _extract_doc_text(doc_bytes: bytes, document_url: str) -> str:
    """
    Extract text from a binary .doc (OLE) file through Word COM, in a worker process with a
    fresh Word instance, killed after config.WORD_EXTRACTION_TIMEOUT_SECONDS.
    Windows only; returns "" without pywin32; raises WordExtractionError when Word fails.
    Writes to ~/.knesset_doc_tmp/ — NOT %TEMP%, which triggers Protected View.
    """
    if not _WORD_COM_AVAILABLE:
        return ""
    tmp_dir = os.path.join(os.path.expanduser("~"), ".knesset_doc_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    doc_file_descriptor, doc_path = tempfile.mkstemp(suffix=".doc", dir=tmp_dir)
    text_path = doc_path + ".txt"
    word_pid_path = doc_path + ".wordpid"
    timeout_seconds = _config.WORD_EXTRACTION_TIMEOUT_SECONDS
    try:
        with os.fdopen(doc_file_descriptor, "wb") as doc_file:
            doc_file.write(doc_bytes)
        worker = subprocess.Popen(_word_worker_command(doc_path, text_path, word_pid_path),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            worker_output, _ = worker.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            worker.kill()
            worker.communicate()
            if os.path.exists(word_pid_path):
                with open(word_pid_path, encoding="ascii") as pid_file:
                    _kill_word_instance(int(pid_file.read().strip()))
            else:
                print(f"[knesset_db] no Word PID recorded for {document_url}; its Word may still be running", flush=True)
            message = f"Word extraction of {document_url} timed out after {timeout_seconds}s ({exc})"
            print(f"[knesset_db] {message}", flush=True)
            raise WordExtractionError(message) from exc
        worker_log = worker_output.decode(errors="replace").strip()
        if worker.returncode != 0:
            message = f"Word extraction of {document_url} failed (exit {worker.returncode}): {worker_log[-2000:]}"
            print(f"[knesset_db] {message}", flush=True)
            raise WordExtractionError(message)
        if worker_log:
            print(f"[knesset_db] Word worker for {document_url}: {worker_log}", flush=True)
        with open(text_path, encoding="utf-8") as text_file:
            return text_file.read().strip()
    finally:
        for temp_path in (doc_path, text_path, word_pid_path):
            _remove_file_if_exists(temp_path)


def _get_session_documents(session_id: int, group_type_id: int | None = None) -> list[dict]:
    """Document metadata of a session from KNS_DocumentCommitteeSession, optionally of one GroupTypeID."""
    odata_filter = f"CommitteeSessionID eq {int(session_id)}"
    if group_type_id is not None:
        odata_filter += f" and GroupTypeID eq {int(group_type_id)}"
    rows = odata_all_rows("KNS_DocumentCommitteeSession", {"$filter": odata_filter, "$orderby": "Id"})
    return [
        {
            "doc_id":     doc["Id"],
            "session_id": session_id,
            "name":       doc.get("DocumentName"),
            "format":     doc.get("ApplicationDesc"),
            "url":        _fix_file_path(doc["FilePath"]),
        }
        for doc in rows
        if doc.get("FilePath")
    ]


def _get_session_protocol_documents(session_id: int) -> list[dict]:
    """The session's committee-protocol documents by GroupTypeID; when none are typed so, the
    documents whose name contains 'פרוטוקול' or 'protocol' (background attachments excluded)."""
    typed_protocols = _get_session_documents(session_id, _config.COMMITTEE_PROTOCOL_DOCUMENT_GROUP_TYPE_ID)
    if typed_protocols:
        return typed_protocols
    return [
        d for d in _get_session_documents(session_id)
        if d["name"] and any(p in d["name"] for p in _PROTOCOL_NAME_SUBSTRINGS)
    ]


def _download_document_text(url: str, application_format: str) -> str:
    """Download a Knesset document and extract its text; application_format is OData's ApplicationDesc."""
    response = _retry_get(url, cached=False, timeout=_config.HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    if application_format.lower() == "pdf":
        return _extract_pdf_text(response.content)
    if url.lower().endswith(".doc"):
        return _extract_doc_text(response.content, url)
    return _extract_docx_text(response.content)


def _get_session_protocol_text(session_id: int, max_chars: int | None = None) -> dict | None:
    """
    Download and extract the protocol document for a session (see _get_session_protocol_documents).
    Returns {session_id, doc_id, name, url, text} or None.
    """
    candidates = _get_session_protocol_documents(session_id)
    if not candidates:
        return None

    word_extraction_error: WordExtractionError | None = None
    for doc in candidates:
        fmt = (doc["format"] or "").lower()
        if fmt not in ("pdf", "word", "doc", "docx"):
            continue
        try:
            text = _download_document_text(doc["url"], fmt)
            if not text:
                continue
            if max_chars:
                text = text[:max_chars]
            return {"session_id": session_id, "doc_id": doc["doc_id"],
                    "name": doc["name"], "url": doc["url"], "text": text}
        except WordExtractionError as exc:
            print(f"[knesset_db] _get_session_protocol_text: doc {doc.get('url')!r} failed in Word ({exc}); trying next", flush=True)
            word_extraction_error = exc
        except Exception as exc:
            print(f"[knesset_db] _get_session_protocol_text: doc {doc.get('url')!r} failed ({exc}); trying next", flush=True)
            continue
    if word_extraction_error is not None:
        raise word_extraction_error
    return None


def get_session_transcript(session_id: int) -> dict | None:
    """
    Download a session transcript, trying oknesset.org first, then OData PDF/Word.

    Returns:
      {"speeches": [...]}                        — structured, from oknesset.org
      {"full_text": "...", "source_url": "..."}  — raw text, from OData document
      None                                       — no transcript available
    """
    speeches = _scrape_oknesset_transcript(session_id)
    if speeches:
        return {"speeches": speeches}

    result = _get_session_protocol_text(session_id)
    if result and result.get("text"):
        return {"full_text": result["text"], "source_url": result["url"]}

    return None


def get_plenum_protocol_documents(knesset_num: int = 25) -> list[dict]:
    """
    The "דברי הכנסת" protocol document of every plenum session of a Knesset, oldest session first:
    [{session_id, session_number, date (YYYY-MM-DD), document_id, format, url, updated}].
    A session with several such documents keeps the most recently updated one.
    """
    rows = odata_all_rows("KNS_DocumentPlenumSession", {
        "$filter":  (f"GroupTypeID eq {_config.PLENUM_PROTOCOL_DOCUMENT_GROUP_TYPE_ID} "
                     f"and KNS_PlenumSession/KnessetNum eq {int(knesset_num)}"),
        "$select":  "Id,PlenumSessionID,ApplicationDesc,FilePath,LastUpdatedDate",
        "$expand":  "KNS_PlenumSession($select=StartDate,Number)",
        "$orderby": "Id",
    })
    by_session: dict[int, dict] = {}
    for row in rows:
        if not row.get("FilePath"):
            continue
        session = row.get("KNS_PlenumSession") or {}
        document = {
            "session_id":     row["PlenumSessionID"],
            "session_number": session.get("Number"),
            "date":           (session.get("StartDate") or "")[:10],
            "document_id":    row["Id"],
            "format":         row.get("ApplicationDesc") or "",
            "url":            _fix_file_path(row["FilePath"]),
            "updated":        row.get("LastUpdatedDate") or "",
        }
        previous = by_session.get(document["session_id"])
        if previous is None or document["updated"] > previous["updated"]:
            by_session[document["session_id"]] = document
    return sorted(by_session.values(), key=lambda d: (d["date"], d["session_id"]))


def get_plenum_session_transcript(document: dict) -> dict | None:
    """{"full_text", "source_url"} of one get_plenum_protocol_documents() entry; None when no text."""
    text = _download_document_text(document["url"], document["format"])
    if not text:
        return None
    return {"full_text": text, "source_url": document["url"]}


def _scrape_oknesset_transcript(session_id: int) -> list[dict] | None:
    """
    Scrape the speech-by-speech transcript from oknesset.org.
    URL: https://oknesset.org/meetings/{s[0]}/{s[1]}/{session_id}.html

    Returns list of {speaker, text_he}, or None if unavailable / no protocol marker.
    """
    s   = str(session_id)
    url = f"https://oknesset.org/meetings/{s[0]}/{s[1]}/{session_id}.html"
    try:
        r = _retry_get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
    except Exception as exc:
        print(f"[knesset_db] oknesset transcript fetch failed for session {session_id} ({exc})", flush=True)
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

_VOTE_EXPAND_KNESSET = "KNS_PlenumSession($select=KnessetNum)"
_VOTE_RESULT_FILTER_BATCH = 10  # the service rejects filters over MaxNodeCount=100
VOTE_RESULT_ABSENT = "לא הצביע"


def odata_page(entity: str, params: dict, offset: int, limit: int) -> tuple[list[dict], int | None]:
    """
    Rows [offset, offset+limit) of an OData query plus the service's total match count
    (None when it sends none). $orderby in `params` must end with a unique key so pages
    stay stable. Pages over the service's silent $top cap of ODATA_PAGE_SIZE.
    """
    url = f"{OFFICIAL_KNESSET_NEW_API}/{entity}"
    rows: list[dict] = []
    total: int | None = None
    while len(rows) < limit:
        chunk_size = min(ODATA_PAGE_SIZE, limit - len(rows))
        page_params = {**params, "$top": chunk_size, "$count": "true"}
        if offset + len(rows):
            page_params["$skip"] = offset + len(rows)
        response = _retry_get(url, params=page_params, timeout=_config.HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
        total = body.get("@odata.count", total)
        page = body.get("value", [])
        rows.extend(page[:chunk_size])
        if len(page) < chunk_size:
            break
    return rows, total


def odata_all_rows(entity: str, params: dict) -> list[dict]:
    """Every row of an OData query, paged; $orderby in `params` must end with a unique key."""
    rows, _ = odata_page(entity, params, 0, sys.maxsize)
    return rows


def _plenum_vote_row(vote: dict) -> dict:
    return {
        "vote_id":          vote.get("Id"),
        "vote_title":       vote.get("VoteTitle"),
        "vote_subject":     vote.get("VoteSubject"),
        "vote_datetime":    vote.get("VoteDateTime"),
        "vote_method":      vote.get("VoteMethodDesc"),
        "is_no_confidence": vote.get("IsNoConfidenceInGov"),
        "knesset_num":      (vote.get("KNS_PlenumSession") or {}).get("KnessetNum"),
    }


def _odata_text(text: str) -> str:
    return text.replace("'", "''")


def search_plenum_votes(
    topic: str | None,
    knesset_num: int | None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[dict], int | None]:
    """
    Plenum votes whose title or subject contains `topic` (every vote when empty), newest
    first, in one Knesset or in all of them when knesset_num is None.
    Returns (rows, total match count).
    """
    clauses = []
    if topic:
        safe_topic = _odata_text(topic)
        clauses.append(f"(contains(VoteTitle,'{safe_topic}') or contains(VoteSubject,'{safe_topic}'))")
    if knesset_num is not None:
        clauses.append(f"KNS_PlenumSession/KnessetNum eq {int(knesset_num)}")
    params = {"$expand": _VOTE_EXPAND_KNESSET, "$orderby": "VoteDateTime desc,Id desc"}
    if clauses:
        params["$filter"] = " and ".join(clauses)
    votes, total = odata_page("KNS_PlenumVote", params, offset, limit)
    return [_plenum_vote_row(v) for v in votes], total


def get_person_votes(
    person_id: int,
    knesset_num: int | None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[dict], int | None]:
    """
    Votes cast by one person (KNS_Person id = KNS_PlenumVoteResult.MkId), newest first, in
    one Knesset or in every Knesset they served in. Each row is a vote row plus `result`.
    """
    filter_expr = f"MkId eq {int(person_id)}"
    if knesset_num is not None:
        filter_expr += f" and Vote/KNS_PlenumSession/KnessetNum eq {int(knesset_num)}"
    params = {
        "$filter":  filter_expr,
        "$expand":  f"Vote($expand={_VOTE_EXPAND_KNESSET})",
        "$orderby": "VoteDate desc,Id desc",
    }
    results, total = odata_page("KNS_PlenumVoteResult", params, offset, limit)
    return [{**_plenum_vote_row(row.get("Vote") or {"Id": row.get("VoteID")}),
             "result": row.get("ResultDesc") or ""}
            for row in results], total


def get_person_votes_on_topic(
    person_id: int,
    topic: str | None,
    knesset_num: int | None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[dict], int | None]:
    """
    search_plenum_votes rows, each with how one person voted (`result`, or
    VOTE_RESULT_ABSENT when they cast no vote). Paging and total follow the vote search.
    """
    votes, total = search_plenum_votes(topic, knesset_num, offset, limit)
    vote_ids = [v["vote_id"] for v in votes]
    result_by_vote_id: dict[int, str] = {}
    for start in range(0, len(vote_ids), _VOTE_RESULT_FILTER_BATCH):
        batch = vote_ids[start:start + _VOTE_RESULT_FILTER_BATCH]
        id_filter = " or ".join(f"VoteID eq {int(vote_id)}" for vote_id in batch)
        response = _retry_get(
            f"{OFFICIAL_KNESSET_NEW_API}/KNS_PlenumVoteResult",
            params={"$filter": f"MkId eq {int(person_id)} and ({id_filter})", "$top": ODATA_PAGE_SIZE},
            timeout=_config.HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        for row in response.json().get("value", []):
            if row.get("VoteID"):
                result_by_vote_id[row["VoteID"]] = row.get("ResultDesc") or ""
    return [{**v, "result": result_by_vote_id.get(v["vote_id"], VOTE_RESULT_ABSENT)} for v in votes], total


def get_person_by_id(person_id: int) -> dict | None:
    """KNS_Person record, or None when the id is unknown."""
    response = _retry_get(f"{OFFICIAL_KNESSET_NEW_API}/KNS_Person({int(person_id)})",
                          timeout=_config.HTTP_TIMEOUT_SECONDS)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()
