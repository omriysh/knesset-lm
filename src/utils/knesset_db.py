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

import requests
from urllib.parse import quote
from functools import lru_cache
import io
import pdfplumber

OKNESSET_API = "https://backend.oknesset.org"
OFFICIAL_KNESSET_NEW_API = "https://knesset.gov.il/OdataV4/ParliamentInfo"
TIMEOUT = 30
_DOC_GROUP_PRIORITY = [
    "הצעת חוק לקריאה השנייה והשלישית",
    "הצעת חוק לקריאה הראשונה",
    "טקסט חוק מאוחד",
    "חוק - פרסום ברשומות",
]



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


# ── Name Search ───────────────────────────────────────────────────────────────

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
        result["other_matches"] = [m["full_name"] for m in matches[1:]]
    return result


def get_law_or_bill_by_name(
    name_part: str,
    knesset_num: int | None = None,
) -> dict | None:
    """
    Search for a bill/law by partial Hebrew name.
    Returns the single most recently updated match, or None.
    """
    base_url = f"{OFFICIAL_KNESSET_NEW_API}/KNS_Bill"
    filter_expr = f"contains(Name,'{name_part}')"
    if knesset_num:
        filter_expr += f" and KnessetNum eq {knesset_num}"

    params = {
        "$filter":  filter_expr,
        "$expand":  "KNS_Status,KNS_BillInitiator($expand=KNS_Person)",
        "$top":     1,                        # only the best match
        "$orderby": "LastUpdatedDate desc",
    }
    r = requests.get(base_url, params=params, timeout=TIMEOUT)
    r.raise_for_status()

    bills = r.json().get("value", [])
    if not bills:
        return None

    bill = bills[0]
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
        if doc["format"] not in ("PDF", "PPT"):  # skip non-PDF for now
            continue
        try:
            response = requests.get(doc["url"], timeout=TIMEOUT)
            response.raise_for_status()

            with pdfplumber.open(io.BytesIO(response.content)) as pdf:
                pages = []
                for page in pdf.pages:
                    text = page.extract_text(x_tolerance=2, y_tolerance=2)
                    if text:
                        pages.append(text)

            full_text = "\n".join(pages).strip()
            if not full_text:
                continue  # try next doc

            return {
                "bill_id": bill_id,
                "doc_id":  doc["doc_id"],
                "group":   doc["group"],
                "url":     doc["url"],
                "text":    full_text[:max_chars],
                "truncated": len(full_text) > max_chars,
            }

        except Exception:
            continue  # try next doc

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