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

OKNESSET_API = "https://backend.oknesset.org"
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


def _normalize_mk(mk: dict, knesset_num: int = 25) -> dict:
    """Convert a raw member record into a clean, consistent dict."""
    factions   = [f for f in (mk.get("factions") or []) if f]
    ministries = [m for m in (mk.get("govministries") or []) if m]
    committees = [c for c in (mk.get("committee_positions") or []) if c]
    faction    = _most_recent_faction(factions, knesset_num)

    return {
        "mk_id":      mk.get("mk_individual_id"),
        "person_id":  mk.get("PersonID"),
        "first_name": mk.get("mk_individual_first_name", ""),
        "last_name":  mk.get("mk_individual_name", ""),
        "full_name":  f"{mk.get('mk_individual_first_name', '')} {mk.get('mk_individual_name', '')}".strip(),
        "altnames":   mk.get("altnames") or [],
        "is_current": mk.get("IsCurrent", False),
        "email":      mk.get("mk_individual_email"),
        "party":      faction["faction_name"].strip() if faction else None,
        "factions":   [
            {
                "party":      f["faction_name"].strip(),
                "start_date": f.get("start_date"),
                "finish_date": f.get("finish_date"),
                "knesset":    f.get("knesset"),
            }
            for f in factions
        ],
        "ministries": [
            {
                "ministry":   m.get("govministry_name", ""),
                "position":   m.get("position_name", ""),
                "start_date": m.get("start_date"),
                "finish_date": m.get("finish_date"),
                "knesset":    m.get("knesset"),
            }
            for m in ministries
            if m.get("knesset") == knesset_num
        ],
        "committee_positions": committees,
    }


# ── Name Search ───────────────────────────────────────────────────────────────

def _name_matches(mk: dict, query: str) -> bool:
    """Check if a query string matches any known name or altname of an MK."""
    query = query.strip()
    full  = f"{mk.get('mk_individual_first_name', '')} {mk.get('mk_individual_name', '')}".strip()
    candidates = [full, mk.get("mk_individual_name", ""), mk.get("mk_individual_first_name", "")]
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
    result  = [_normalize_mk(mk, knesset_num) for mk in members]
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
    Returns a list of normalized MK dicts.
    """
    current = _fetch_members(True)
    former  = _fetch_members(False)
    matches = [
        _normalize_mk(mk, knesset_num)
        for mk in (current + former)
        if _name_matches(mk, name)
    ]
    return matches


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