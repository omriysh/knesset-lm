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
from functools import lru_cache
from datetime import datetime, timezone
from dateutil import parser

OKNESSET_API = "https://backend.oknesset.org"
OFFICIAL_KNESSET_NEW_API = "https://knesset.gov.il/OdataV4/ParliamentInfo"
TIMEOUT      = 15


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


def get_committee_members(
    committee_id: int,
    knesset_num: int = 25,
    date: datetime | None = None,
) -> list[dict]:

    if date is None:
        date = datetime.now(timezone.utc)

    url = f"{OFFICIAL_KNESSET_NEW_API}/KNS_PersonToPosition"

    params = {
        "$expand": "KNS_Position,KNS_Person",
        "$filter": f"KnessetNum eq {knesset_num} and CommitteeID eq {committee_id}",
        "$top": 1000
    }

    r = requests.get(url, params=params)
    r.raise_for_status()

    rows = r.json().get("value", [])

    active_members: dict[int, dict] = {}

    for row in rows:
        start = parser.isoparse(row.get("StartDate"))
        finish_raw = row.get("FinishDate")
        finish = parser.isoparse(finish_raw) if finish_raw else None

        # Filter by date
        if start > date:
            continue
        if finish and finish < date:
            continue

        person = row.get("KNS_Person", {})
        position = row.get("KNS_Position", {})

        mk_id = row.get("PersonID")
        full_name = f"{person.get('FirstName','')} {person.get('LastName','')}".strip()
        role = position.get("PositionName")

        # Deduplicate by mk_id
        if mk_id not in active_members:
            active_members[mk_id] = {
                "mk_id": mk_id,
                "full_name": full_name,
                "roles": set(),
            }

        if role:
            active_members[mk_id]["roles"].add(role)

    # Convert roles set → sorted list
    result = []
    for member in active_members.values():
        result.append({
            "mk_id": member["mk_id"],
            "full_name": member["full_name"],
            "roles": sorted(member["roles"]),
        })

    result.sort(key=lambda x: x["full_name"])
    return result


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