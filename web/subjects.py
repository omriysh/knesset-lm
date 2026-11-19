"""
web/subjects.py

The subjects tab ("נושאים"): the shared subjects MKs talked about in the latest processed Knesset, grouped
into umbrella areas; a subject's approaches; and the MK themes behind one approach, each linked to its
candidate profile when the MK is a 26th Knesset candidate.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import config
from api import validation as valid
from profiles import mk_activity, subjects
from retrieval import knesset_db_store as store
from subjects import prompts as subject_prompts
from summarization import prompts as summary_prompts
from web.profiles import ACTIVITY_KNESSET, OPINIONS_PAGE_SIZE, _offset, _photo_url, candidate_lists

router = APIRouter()

SUBJECT_PAGE_PATHS = ("/subjects", "/subjects/area/{area}", "/subjects/{subject_id}",
                      "/subjects/{subject_id}/approach/{approach_id}")
SUBJECT_NOT_FOUND = "הנושא לא נמצא"


def _not_found(message: str = SUBJECT_NOT_FOUND) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=404)


def _candidates_by_mk() -> dict[str, dict]:
    return {c["mk_id"]: {"profile_url": f"/profiles/party/{party['id']}/candidate/{c['position']}",
                         "photo_url": _photo_url(party["id"], c), "party_26": party["name"]}
            for party in candidate_lists()["parties"] for c in party["candidates"]
            if c["profile"] == "full" and c.get("mk_id")}


@router.get("/api/subjects")
def subject_areas():
    """Umbrella areas, most MKs first, each with its subjects."""
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        return {"knesset_num": ACTIVITY_KNESSET, "areas": subjects.subject_areas(conn, ACTIVITY_KNESSET)}
    finally:
        conn.close()


PROCESS_PROMPTS = {
    "opinions": [summary_prompts.SYSTEM_PROMPT_OPINIONS],
    "themes": [summary_prompts.SYSTEM_PROMPT_MK_THEMES],
    "subjects": [subject_prompts.SHARD_SYSTEM_PROMPT, subject_prompts.MERGE_SYSTEM_PROMPT,
                 subject_prompts.CONSOLIDATE_SYSTEM_PROMPT],
    "assign": [subject_prompts.ASSIGN_SYSTEM_PROMPT],
    "background": [subject_prompts.VERIFY_SYSTEM_PROMPT, subject_prompts.REPAIR_SYSTEM_PROMPT],
    "approaches": [subject_prompts.APPROACHES_SYSTEM_PROMPT],
}


@router.get("/api/subjects/process")
def process_counts():
    """What each AI step of making the subjects produced and the system prompts it ran with, for the
    "how was this made" dialog."""
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        return {"knesset_num": ACTIVITY_KNESSET, **subjects.process_counts(conn, ACTIVITY_KNESSET), "prompts": PROCESS_PROMPTS}
    finally:
        conn.close()


@router.get("/api/subjects/{subject_id}")
def subject(subject_id: int):
    """A subject with its background, party counts and approaches."""
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        result = subjects.subject(conn, subject_id)
    finally:
        conn.close()
    return result if result is not None else _not_found()


@router.get("/api/subjects/{subject_id}/approach/{approach_id}")
def approach(subject_id: int, approach_id: int):
    """The MK themes behind one approach of a subject, each with its MK and, for a candidate, their profile."""
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        result = subjects.approach_themes(conn, subject_id, approach_id)
    finally:
        conn.close()
    if result is None:
        return _not_found("הגישה לא נמצאה")
    candidates = _candidates_by_mk()
    for theme in result["themes"]:
        theme["mk"].update(candidates.get(theme["mk"]["mk_id"], {}))
    return result


@router.get("/api/subjects/theme/{theme_id}/opinions")
def theme_opinions(theme_id: int, date_from: str = "", date_to: str = "", offset: int = 0):
    """A page of the opinions of one theme in some subject (the theme sheet), newest first."""
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        owner = subjects.theme_owner(conn, theme_id)
        if owner is None:
            return _not_found()
        mk_id, knesset_num = owner
        return mk_activity.mk_opinions(
            conn, mk_id, knesset_num, theme=str(theme_id), date_from=valid.iso_date(date_from, "date_from"),
            date_to=valid.iso_date(date_to, "date_to"), offset=_offset(offset), limit=OPINIONS_PAGE_SIZE)
    finally:
        conn.close()
