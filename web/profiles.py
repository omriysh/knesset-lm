"""
web/profiles.py

MK profiles ("פרופילים"), in two views of the same profile routes:
- the sitting Knesset (config.CURRENT_KNESSET_NUM): its factions, a faction's MKs and an MK's profile, keyed by
  faction id and person id (Data/knesset_roster/<k>/roster.json, scripts/build_knesset_roster.py);
- the 26th Knesset elections (the site's election mode): the party grid, a party's candidates, and a candidate's
  profile. Party ids are the row order of the gov.il candidate-lists page and candidate ids their list position
  (Data/candidates/26/lists.json, scripts/build_candidate_lists.py), so candidates who were never MKs and new
  parties have stable URLs.

A candidate who served in a Knesset with processed protocols (config.PROTOCOL_KNESSET_NUMS) gets a full
profile (themes, opinions, votes, bills, roles, attendance); a former MK of an earlier Knesset gets roles,
votes and bills; anyone else has no profile. Committee activity comes from knesset.db, one Knesset at a
time (the latest with data unless asked); votes, bills and roles from the Knesset OData (cached).
"""

import json
from collections import Counter
from functools import lru_cache, wraps

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

import config
from api import validation as valid
from profiles import game, mk_activity, subjects
from retrieval import knesset_db_store as store
from utils import knesset_db as kdb
from utils.tools import _expand_match

router = APIRouter()

PROFILE_PAGE_PATHS = ("/profiles", "/profiles/party/{party_id}", "/profiles/party/{party_id}/candidate/{candidate_id}",
                      "/profiles/faction/{faction_id}", "/profiles/mk/{person_id}")
CANDIDATE_API = "/api/profiles/party/{party_id}/candidate/{candidate_id}"
MK_API = "/api/profiles/mk/{person_id}"
ACTIVITY_KNESSET = config.ROSTER_DEFAULT_KNESSET_NUM
OPINIONS_PAGE_SIZE = 30
VOTES_PAGE_SIZE = 50
BILLS_PAGE_SIZE = 20
COSPONSORS_SHOWN = 8
BILL_ROLES = ("initiator", "joined")
_MAX_OFFSET = 10_000


@lru_cache(maxsize=1)
def _lists_cached(mtime: float) -> dict:
    return json.loads((config.candidate_lists_dir(26) / "lists.json").read_text(encoding="utf-8"))


def candidate_lists() -> dict:
    path = config.candidate_lists_dir(26) / "lists.json"
    return _lists_cached(path.stat().st_mtime) if path.exists() else {"parties": []}


def find_party(party_id: int) -> dict | None:
    return next((party for party in candidate_lists()["parties"] if party["id"] == party_id), None)


def find_candidate(party_id: int, candidate_id: int) -> tuple[dict | None, dict | None]:
    party = find_party(party_id)
    if party is None:
        return None, None
    return party, next((c for c in party["candidates"] if c["position"] == candidate_id), None)


@lru_cache(maxsize=1)
def _roster_cached(mtime: float) -> dict:
    return json.loads(_roster_path().read_text(encoding="utf-8"))


def _roster_path():
    return config.knesset_roster_dir(config.CURRENT_KNESSET_NUM) / "roster.json"


def knesset_roster() -> dict:
    """The sitting Knesset's factions and MKs; empty until scripts/build_knesset_roster.py has run."""
    path = _roster_path()
    return _roster_cached(path.stat().st_mtime) if path.exists() else {"knesset_num": config.CURRENT_KNESSET_NUM, "factions": []}


def find_faction(faction_id: int) -> dict | None:
    return next((faction for faction in knesset_roster()["factions"] if faction["id"] == faction_id), None)


def find_member(person_id: int) -> tuple[dict | None, dict | None]:
    for faction in knesset_roster()["factions"]:
        member = next((m for m in faction["members"] if m["person_id"] == person_id), None)
        if member:
            return faction, member
    return None, None


def _not_found(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=404)


def _photo_url(party_id: int, candidate: dict) -> str | None:
    return f"/api/profiles/photo/{party_id}/{candidate['position']}" if candidate.get("photo") else None


def candidate_card(party_id: int, candidate: dict) -> dict:
    return {"position": candidate["position"], "name": candidate["name"], "profile": candidate["profile"],
            "photo_url": _photo_url(party_id, candidate), "photo_source": candidate.get("photo_source"),
            "knessets": candidate["knessets"], "from_party": candidate.get("from_party", "")}


def party_card(party: dict) -> dict:
    candidates = party["candidates"]
    return {"id": party["id"], "letters": party["letters"], "name": party["name"], "leader": party["leader"],
            "logo_url": f"/api/profiles/logo/{party['id']}" if party.get("logo") else None,
            "website": party.get("website"),
            "leader_photo_url": _photo_url(party["id"], candidates[0]) if candidates else None,
            "candidate_count": len(candidates),
            "full_profiles": sum(c["profile"] == "full" for c in candidates),
            "former_mks": sum(c["profile"] == "bills" for c in candidates)}


def _member_photo_url(member: dict) -> str | None:
    if not member.get("photo"):
        return None
    version = "".join(ch for ch in knesset_roster().get("built_at", "") if ch.isdigit())
    return f"/api/profiles/mk-photo/{member['person_id']}?v={version}"


def member_card(member: dict) -> dict:
    return {"person_id": member["person_id"], "name": member["name"], "profile": member["profile"],
            "photo_url": _member_photo_url(member), "photo_source": member.get("photo_source"),
            "knessets": member["knessets"], "from_party": member.get("from_party", ""),
            "list_position": member.get("list_position")}


def faction_card(faction: dict) -> dict:
    members = faction["members"]
    return {"id": faction["id"], "name": faction["name"], "start_date": faction.get("start_date"),
            "logo_url": f"/api/profiles/faction-logo/{faction['id']}" if faction.get("logo") else None,
            "leader": members[0]["name"] if members else "",
            "leader_photo_url": _member_photo_url(members[0]) if members else None,
            "seats": len(members),
            "full_profiles": sum(m["profile"] == "full" for m in members),
            "new_mks": sum(m["knessets"] == [config.CURRENT_KNESSET_NUM] for m in members)}


def _profile_mk(person_id: int, needs: str = "bills") -> tuple[dict | None, dict | None, JSONResponse | None]:
    """(faction, member, None), or a 404 when the MK is unknown or has no profile of that depth."""
    faction, member = find_member(person_id)
    if member is None:
        return None, None, _not_found("חבר/ת הכנסת לא נמצא/ה")
    if needs == "full" and member["profile"] != "full":
        return None, None, _not_found("אין פרופיל מלא לחבר/ת הכנסת")
    return faction, member, None


def _profile_candidate(party_id: int, candidate_id: int, needs: str = "bills") -> tuple[dict | None, dict | None, JSONResponse | None]:
    """(party, candidate, None), or a 404 when the candidate is unknown or has no profile of that depth."""
    party, candidate = find_candidate(party_id, candidate_id)
    if candidate is None:
        return None, None, _not_found("המועמד לא נמצא")
    if candidate["profile"] == "none" or (needs == "full" and candidate["profile"] != "full"):
        return None, None, _not_found("אין פרופיל למועמד הזה")
    return party, candidate, None


def upstream_route(route):
    """A route that calls the Knesset OData: a failed call is a 502 with a Hebrew message, not a 500."""
    @wraps(route)
    def guarded(*args, **kwargs):
        try:
            return route(*args, **kwargs)
        except valid.ApiInputError:
            raise
        except Exception as exc:
            print(f"[profiles] {route.__name__} failed: {exc}")
            return JSONResponse({"error": "שגיאה בקבלת הנתונים מאתר הכנסת, נסו שוב מאוחר יותר"}, status_code=502)
    return guarded


def _offset(offset: int) -> int:
    return valid.offset(offset, _MAX_OFFSET)


@lru_cache(maxsize=1)
def _theme_pool_cached(lists_path: str, lists_mtime: float, db_mtime: float) -> dict[int, game.PoolTheme]:
    candidates = [(party["id"], c["position"], c["mk_id"]) for party in candidate_lists()["parties"]
                  for c in party["candidates"]
                  if c["profile"] == "full" and c.get("mk_id") and ACTIVITY_KNESSET in c["knessets"]]
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        themes = mk_activity.themes_of_mks(conn, sorted({mk_id for *_, mk_id in candidates}), ACTIVITY_KNESSET)
        theme_subjects = subjects.game_subjects(conn, ACTIVITY_KNESSET)
    finally:
        conn.close()
    return game.build_pool(candidates, themes, theme_subjects)


def theme_pool() -> dict[int, game.PoolTheme]:
    """Every theme of a candidate with a full profile, by theme id (the game's cards)."""
    path = config.candidate_lists_dir(26) / "lists.json"
    return _theme_pool_cached(str(path), path.stat().st_mtime if path.exists() else 0.0, store.db_path().stat().st_mtime)


def check_theme_ids(theme_ids: list[int], pool: dict[int, game.PoolTheme], name: str) -> None:
    """400 unless every id is unique and between 1 and the largest theme id of the pool."""
    top = max(pool, default=0)
    if any(not 1 <= theme_id <= top for theme_id in theme_ids):
        raise valid.ApiInputError(f"invalid_{name}", f"{name} ids must be between 1 and {top}")
    if len(set(theme_ids)) != len(theme_ids):
        raise valid.ApiInputError(f"duplicate_{name}", f"{name} ids must be unique")


# ── lists ────────────────────────────────────────────────────────────────────

@router.get("/api/profiles/parties")
def profile_parties():
    """Lists with a current or former MK first, each group in alphabetical order."""
    lists = candidate_lists()
    return {"source": lists.get("source"), "built_at": lists.get("built_at"),
            "activity_knessets": list(config.PROTOCOL_KNESSET_NUMS),
            "parties": sorted(({**party_card(party), "candidates": [[c["position"], c["name"] or c["name_raw"]] for c in party["candidates"]]}
                               for party in lists["parties"]),
                              key=lambda card: (not (card["full_profiles"] or card["former_mks"]), card["name"]))}


@router.get("/api/profiles/party/{party_id}")
def profile_party(party_id: int):
    party = find_party(party_id)
    if party is None:
        return _not_found("הרשימה לא נמצאה")
    return {**party_card(party), "submitted_by": party.get("submitted_by", ""), "gov_url": party["gov_url"],
            "candidates": [candidate_card(party_id, c) for c in party["candidates"]]}


@router.get("/api/profiles/photo/{party_id}/{candidate_id}")
def profile_photo(party_id: int, candidate_id: int):
    _, candidate = find_candidate(party_id, candidate_id)
    if candidate is None or not candidate.get("photo"):
        return JSONResponse({}, status_code=404)
    path = config.candidate_lists_dir(26) / "photos" / f"{party_id}_{candidate_id}.jpg"
    if not path.exists():
        return JSONResponse({}, status_code=404)
    return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/profiles/logo/{party_id}")
def profile_logo(party_id: int):
    path = config.candidate_lists_dir(26) / "logos" / f"{party_id}.png"
    if find_party(party_id) is None or not path.exists():
        return JSONResponse({}, status_code=404)
    return FileResponse(str(path), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ── the sitting Knesset ──────────────────────────────────────────────────────

@router.get("/api/profiles/factions")
def profile_factions():
    """The factions of the sitting Knesset, largest first, with their MKs' names for the search box."""
    roster = knesset_roster()
    return {"knesset_num": roster["knesset_num"], "source": roster.get("source"), "built_at": roster.get("built_at"),
            "factions": [{**faction_card(faction), "members": [[m["person_id"], m["name"]] for m in faction["members"]]}
                         for faction in roster["factions"]]}


@router.get("/api/profiles/faction/{faction_id}")
def profile_faction(faction_id: int):
    faction = find_faction(faction_id)
    if faction is None:
        return _not_found("הסיעה לא נמצאה")
    return {**faction_card(faction), "knesset_num": knesset_roster()["knesset_num"],
            "members": [member_card(m) for m in faction["members"]]}


@router.get("/api/profiles/mk-photo/{person_id}")
def profile_member_photo(person_id: int):
    _, member = find_member(person_id)
    if member is None or not member.get("photo"):
        return JSONResponse({}, status_code=404)
    path = config.knesset_roster_dir(config.CURRENT_KNESSET_NUM) / member["photo"]
    if not path.exists():
        return JSONResponse({}, status_code=404)
    return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/api/profiles/faction-logo/{faction_id}")
def profile_faction_logo(faction_id: int):
    faction = find_faction(faction_id)
    if faction is None or not faction.get("logo"):
        return JSONResponse({}, status_code=404)
    path = config.knesset_roster_dir(config.CURRENT_KNESSET_NUM) / faction["logo"]
    if not path.exists():
        return JSONResponse({}, status_code=404)
    return FileResponse(str(path), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ── a profile: each route serves a candidate (CANDIDATE_API) and an MK of the sitting Knesset (MK_API) ──

@router.get(CANDIDATE_API)
def profile_candidate(party_id: int, candidate_id: int):
    """The header card: who, which list, Knesset-site details and committee activity counts. Database
    only, so it loads when the Knesset OData is down; roles and attendance come from their own routes."""
    party, candidate = find_candidate(party_id, candidate_id)
    if candidate is None:
        return _not_found("המועמד לא נמצא")
    body = {"kind": "candidate", "party": party_card(party),
            "candidate": {**candidate_card(party_id, candidate), "details": candidate.get("details") or {},
                          "wikipedia": candidate.get("wikipedia"), "site_id": candidate.get("site_id")}}
    return _with_activity(body, candidate)


@router.get(MK_API)
def profile_member(person_id: int):
    faction, member = find_member(person_id)
    if member is None:
        return _not_found("חבר/ת הכנסת לא נמצא/ה")
    body = {"kind": "mk", "knesset_num": knesset_roster()["knesset_num"], "party": faction_card(faction),
            "candidate": {**member_card(member), "details": member.get("details") or {},
                          "wikipedia": member.get("wikipedia"), "site_id": member.get("site_id")}}
    return _with_activity(body, member)


def _with_activity(body: dict, person: dict) -> dict:
    if person["profile"] == "full":
        conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
        try:
            body["activity"] = _candidate_activity(conn, person)
        finally:
            conn.close()
    return body


def _candidate_activity(conn, candidate: dict) -> dict:
    """Opinion counts of the latest Knesset with activity (the header stats) and the activity Knessets."""
    knessets = activity_knessets(conn, candidate)
    return {**mk_activity.opinion_counts(conn, candidate["mk_id"], knessets[0]),
            "knesset_num": knessets[0], "knessets": knessets, "simple_knesset": ACTIVITY_KNESSET,
            "has_subjects": mk_activity.has_subjects(conn, candidate["mk_id"], knessets[0])}


def _committee_positions(person_id: int, knesset_num: int) -> list[dict] | None:
    try:
        return kdb.get_mk_positions(person_id, knesset_num)["committee_positions"]
    except Exception as exc:
        print(f"[profiles] committee positions of {person_id} in Knesset {knesset_num} failed: {exc}")
        return None


@router.get(CANDIDATE_API + "/attendance")
def profile_attendance(party_id: int, candidate_id: int):
    _, candidate, error = _profile_candidate(party_id, candidate_id, needs="full")
    return error or _attendance(candidate)


@router.get(MK_API + "/attendance")
def member_attendance(person_id: int):
    _, member, error = _profile_mk(person_id, needs="full")
    return error or _attendance(member)


def _attendance(candidate: dict) -> dict:
    """Committee attendance in each activity Knesset, latest first. Memberships come from the Knesset
    OData, fetched before the database opens so a slow API doesn't use up the query deadline; a Knesset
    whose memberships failed has an error instead."""
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        knessets = activity_knessets(conn, candidate)
    finally:
        conn.close()
    positions_by_knesset = {knesset_num: _committee_positions(candidate["person_id"], knesset_num) for knesset_num in knessets}
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        return {"knessets": [
            {"knesset_num": knesset_num, "error": "שגיאה בטעינת הוועדות מאתר הכנסת"} if positions is None
            else {"knesset_num": knesset_num, **mk_activity.committee_attendance(conn, candidate["mk_id"], knesset_num, positions)}
            for knesset_num, positions in positions_by_knesset.items()]}
    finally:
        conn.close()


def activity_knessets(conn, candidate: dict) -> list[int]:
    """The processed Knessets the candidate served in and was active in committees, latest first (a
    non-member's guest appearances don't count); when there is none, the latest processed Knesset they
    served in, so the page still has one Knesset to show."""
    served = [k for k in config.PROTOCOL_KNESSET_NUMS if k in candidate["knessets"]]
    knessets = mk_activity.activity_knessets(conn, candidate["mk_id"], served)
    return knessets or [max(served or config.PROTOCOL_KNESSET_NUMS)]


def _chosen_activity_knesset(conn, candidate: dict, knesset: int | None, theme: str = "") -> tuple[int, list[int]]:
    """(the Knesset on display, the candidate's activity Knessets): the requested one, else the Knesset of
    the requested theme, else the latest. 400 for a Knesset without the candidate's activity."""
    knessets = activity_knessets(conn, candidate)
    if knesset is None and theme.isdigit():
        knesset = mk_activity.theme_knesset(conn, int(theme))
    if knesset is None:
        return knessets[0], knessets
    if knesset not in knessets:
        raise valid.ApiInputError("invalid_knesset", "no committee activity of this candidate in that Knesset")
    return knesset, knessets


def _served_knesset(person: dict, knesset: int | None) -> int | None:
    """None for every Knesset; 400 for a Knesset the person did not serve in."""
    if knesset is not None and knesset not in person["knessets"]:
        raise valid.ApiInputError("invalid_knesset", "the MK did not serve in that Knesset")
    return knesset


@router.get(CANDIDATE_API + "/vote-summary")
@upstream_route
def profile_vote_summary(party_id: int, candidate_id: int, knesset: int | None = None):
    _, candidate, error = _profile_candidate(party_id, candidate_id)
    return error or _vote_summary(candidate, knesset)


@router.get(MK_API + "/vote-summary")
@upstream_route
def member_vote_summary(person_id: int, knesset: int | None = None):
    _, member, error = _profile_mk(person_id)
    return error or _vote_summary(member, knesset)


def _vote_summary(person: dict, knesset: int | None) -> dict:
    """Plenum vote counts of one Knesset, or of the person's latest two."""
    knessets = [_served_knesset(person, knesset)] if knesset is not None else sorted(person["knessets"], reverse=True)[:2]
    return {"knessets": [kdb.get_person_vote_summary(person["person_id"], knesset_num) for knesset_num in knessets]}


@router.get(CANDIDATE_API + "/themes")
def profile_themes(party_id: int, candidate_id: int, knesset: int | None = None):
    _, candidate, error = _profile_candidate(party_id, candidate_id, needs="full")
    return error or _themes(candidate, knesset)


@router.get(MK_API + "/themes")
def member_themes(person_id: int, knesset: int | None = None):
    _, member, error = _profile_mk(person_id, needs="full")
    return error or _themes(member, knesset)


def _themes(candidate: dict, knesset: int | None) -> dict:
    """The themes of one Knesset (the latest with activity unless knesset is given), with the Knessets to
    choose from."""
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        knesset_num, knessets = _chosen_activity_knesset(conn, candidate, knesset)
        return {**mk_activity.add_subjects(conn, mk_activity.mk_themes(conn, candidate["mk_id"], knesset_num)),
                "knesset_num": knesset_num, "knessets": knessets}
    finally:
        conn.close()


@router.get("/api/profiles/theme/{theme_id}")
def profile_theme(theme_id: int):
    """One theme as it appears on its candidate's profile, with who it belongs to (for the game's results)."""
    pool = theme_pool()
    check_theme_ids([theme_id], pool, "theme")
    owner = pool.get(theme_id)
    if owner is None:
        return _not_found("הנושא לא נמצא")
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        data = mk_activity.mk_theme(conn, theme_id)
        if data is not None:
            mk_activity.add_subjects(conn, {"themes": [data["theme"]], "quarters": data["quarters"]})
    finally:
        conn.close()
    if data is None:
        return _not_found("הנושא לא נמצא")
    return {**data, "party_id": owner.party_id, "candidate_id": owner.position}


@router.get(CANDIDATE_API + "/opinions")
def profile_opinions(party_id: int, candidate_id: int, q: str = "", theme: str = "", committee: str = "",
                     date_from: str = "", date_to: str = "", offset: int = 0, knesset: int | None = None):
    _, candidate, error = _profile_candidate(party_id, candidate_id, needs="full")
    return error or _opinions(candidate, q, theme, committee, date_from, date_to, offset, knesset)


@router.get(MK_API + "/opinions")
def member_opinions(person_id: int, q: str = "", theme: str = "", committee: str = "",
                    date_from: str = "", date_to: str = "", offset: int = 0, knesset: int | None = None):
    _, member, error = _profile_mk(person_id, needs="full")
    return error or _opinions(member, q, theme, committee, date_from, date_to, offset, knesset)


def _opinions(candidate: dict, q: str, theme: str, committee: str, date_from: str, date_to: str,
              offset: int, knesset: int | None) -> dict:
    """A page of the candidate's opinions in one Knesset: the requested one, else the Knesset of the
    requested theme, else the latest with activity."""
    query = valid.keyword_query(q)
    if theme and theme != mk_activity.OTHER_THEME:
        valid.numeric_id(theme, "theme")
    committee_name = valid.name_filter(committee, "committee", config.MAX_COMMITTEE_NAME_CHARS)
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        knesset_num, _ = _chosen_activity_knesset(conn, candidate, knesset, theme)
        page = mk_activity.mk_opinions(
            conn, candidate["mk_id"], knesset_num, fts_match=_expand_match(query, "opinions_fts") if query else None,
            theme=theme or None, committee=committee_name, date_from=valid.iso_date(date_from, "date_from"),
            date_to=valid.iso_date(date_to, "date_to"), offset=_offset(offset), limit=OPINIONS_PAGE_SIZE)
        return {**page, "knesset_num": knesset_num}
    finally:
        conn.close()


@router.get(CANDIDATE_API + "/votes")
@upstream_route
def profile_votes(party_id: int, candidate_id: int, q: str = "", offset: int = 0, knesset: int | None = None):
    _, candidate, error = _profile_candidate(party_id, candidate_id)
    return error or _votes(candidate, q, offset, knesset)


@router.get(MK_API + "/votes")
@upstream_route
def member_votes(person_id: int, q: str = "", offset: int = 0, knesset: int | None = None):
    _, member, error = _profile_mk(person_id)
    return error or _votes(member, q, offset, knesset)


def _votes(person: dict, q: str, offset: int, knesset: int | None) -> dict:
    """The person's plenum votes (in one Knesset, or all), newest first; with q, the votes matching it and how
    they voted (kdb.VOTE_RESULT_ABSENT when they did not)."""
    query = valid.search_text(q)
    offset = _offset(offset)
    knesset_num = _served_knesset(person, knesset)
    if query:
        votes, total = kdb.get_person_votes_on_topic(person["person_id"], query, knesset_num, offset, VOTES_PAGE_SIZE)
    else:
        votes, total = kdb.get_person_votes(person["person_id"], knesset_num, offset, VOTES_PAGE_SIZE)
    return {"votes": votes, "total": total, "offset": offset, "page_size": VOTES_PAGE_SIZE}


@router.get(CANDIDATE_API + "/bills")
@upstream_route
def profile_bills(party_id: int, candidate_id: int, q: str = "", role: str = "", stage: str = "", offset: int = 0,
                  knesset: int | None = None):
    _, candidate, error = _profile_candidate(party_id, candidate_id)
    return error or _bills(candidate, q, role, stage, offset, knesset)


@router.get(MK_API + "/bills")
@upstream_route
def member_bills(person_id: int, q: str = "", role: str = "", stage: str = "", offset: int = 0,
                 knesset: int | None = None):
    _, member, error = _profile_mk(person_id)
    return error or _bills(member, q, role, stage, offset, knesset)


def _bills(person: dict, q: str, role: str, stage: str, offset: int, knesset: int | None) -> dict:
    query = kdb._sanitize_odata_search(valid.search_text(q))
    initiator_role = valid.one_of(role, BILL_ROLES, "role") if role else ""
    bill_stage = valid.one_of(stage, tuple(kdb.BILL_STATUS_IDS_BY_STAGE), "stage") if stage else ""
    offset = _offset(offset)
    bills, total = kdb.person_bills_page(person["person_id"], query, _served_knesset(person, knesset), initiator_role,
                                         offset, BILLS_PAGE_SIZE, bill_stage)
    return {"bills": bills, "total": total, "offset": offset, "page_size": BILLS_PAGE_SIZE}


@router.get(CANDIDATE_API + "/cosponsors")
@upstream_route
def profile_cosponsors(party_id: int, candidate_id: int, knesset: int | None = None):
    _, candidate, error = _profile_candidate(party_id, candidate_id)
    if error:
        return error
    listed = {c["person_id"]: (f"/profiles/party/{party['id']}/candidate/{c['position']}" if c["profile"] != "none" else None,
                               party["name"])
              for party in candidate_lists()["parties"] for c in party["candidates"] if c.get("person_id")}
    return _cosponsors(candidate, knesset, listed)


@router.get(MK_API + "/cosponsors")
@upstream_route
def member_cosponsors(person_id: int, knesset: int | None = None):
    _, member, error = _profile_mk(person_id)
    if error:
        return error
    sitting = {m["person_id"]: (f"/profiles/mk/{m['person_id']}", faction["name"])
               for faction in knesset_roster()["factions"] for m in faction["members"]}
    return _cosponsors(member, knesset, sitting)


def _cosponsors(person: dict, knesset: int | None, profiles_by_person: dict[int, tuple[str | None, str]]) -> dict:
    """The MKs who most often signed the person's bills with them (in one Knesset, or over every bill they
    signed); profiles_by_person: person id → (profile url, party name) of the people the site has a page for."""
    person_id = person["person_id"]
    knesset_num = _served_knesset(person, knesset)
    bills, offset, total = [], 0, None
    while total is None or offset < total:
        page, total = kdb.person_bills_page(person_id, "", knesset_num, "", offset, kdb.ODATA_PAGE_SIZE)
        if not page:
            break
        bills.extend(page)
        offset += len(page)
    counts, names = Counter(), {}
    for bill in bills:
        for initiator in bill["initiators"]:
            if initiator["person_id"] != person_id:
                counts[initiator["person_id"]] += 1
                names[initiator["person_id"]] = initiator["full_name"]
    cosponsors = []
    for cosponsor_id, shared in counts.most_common(COSPONSORS_SHOWN):
        profile_url, party_name = profiles_by_person.get(cosponsor_id, (None, None))
        cosponsors.append({"person_id": cosponsor_id, "name": names[cosponsor_id], "shared_bills": shared,
                           "profile_url": profile_url, "party_name": party_name})
    return {"bills": len(bills), "passed": sum(b["status"] == "התקבלה בקריאה שלישית" for b in bills),
            "initiated": sum(b["mk_is_initiator"] for b in bills), "cosponsors": cosponsors}


@router.get(CANDIDATE_API + "/roles")
@upstream_route
def profile_roles(party_id: int, candidate_id: int):
    _, candidate, error = _profile_candidate(party_id, candidate_id)
    return error or _roles(candidate)


@router.get(MK_API + "/roles")
@upstream_route
def member_roles(person_id: int):
    _, member, error = _profile_mk(person_id)
    return error or _roles(member)


def _roles(person: dict) -> dict:
    """Positions in every Knesset the person served in, newest first."""
    knessets = []
    for knesset_num in sorted(person["knessets"], reverse=True):
        try:
            knessets.append({"knesset_num": knesset_num, **kdb.get_mk_positions(person["person_id"], knesset_num)})
        except Exception as exc:
            print(f"[profiles] positions of {person['person_id']} in Knesset {knesset_num} failed: {exc}")
            knessets.append({"knesset_num": knesset_num, "error": "שגיאה בטעינת התפקידים"})
    return {"knessets": knessets}


@router.get("/api/profiles/bill/{bill_id}/text")
@upstream_route
def profile_bill_text(bill_id: int, offset: int = 0):
    """A bill's extracted text, for the profile's bill preview dialog."""
    if bill_id <= 0:
        return _not_found("הצעת החוק לא נמצאה")
    record = kdb._get_bill_text_by_id(bill_id, max_chars=config.BILL_TEXT_MAX_MAX_CHARS,
                                      text_offset=valid.offset(offset, config.BILL_TEXT_MAX_OFFSET))
    if record is None:
        return _not_found("לא נמצא טקסט להצעת החוק")
    return record
