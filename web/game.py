"""
web/game.py

The "היכרות" game (the game tab, /game): the next cards to swipe, with the MK's names blacked out,
and the results by party. The browser keeps the votes and sends the theme ids it has seen; ids are checked
(strict ints, unique, in the pool's range) before anything else, and ids that are in range but no longer in
the pool (themes rebuilt since) come back as `dropped` so the browser can forget them.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field, StrictInt

import config
from profiles import game, mk_activity
from retrieval import knesset_db_store as store
from web.profiles import ACTIVITY_KNESSET, PROFILE_PAGE_PATHS, candidate_card, check_theme_ids, find_candidate, \
    find_party, party_card, theme_pool

router = APIRouter()

CARD_EVIDENCE = 2


class NextCardsRequest(BaseModel):
    seen: list[StrictInt] = Field(default_factory=list, max_length=config.GAME_MAX_IDS)
    count: StrictInt = Field(default=2, ge=1, le=config.GAME_MAX_CARDS_PER_FETCH)


class ResultsRequest(BaseModel):
    agreed: list[StrictInt] = Field(default_factory=list, max_length=config.GAME_MAX_IDS)
    disagreed: list[StrictInt] = Field(default_factory=list, max_length=config.GAME_MAX_IDS)


def _split_known(theme_ids: list[int], pool: dict) -> tuple[list[int], list[int]]:
    return [i for i in theme_ids if i in pool], [i for i in theme_ids if i not in pool]


def _card(conn, theme_id: int, names: list[str]) -> dict:
    theme = mk_activity.mk_theme(conn, theme_id, CARD_EVIDENCE)["theme"]
    return {"id": theme_id, "title": game.redact(theme["title"], names), "summary": game.redact(theme["summary"], names),
            "evidence": [{"quote": game.redact(e["quote"] or "", names), "opinion": game.redact(e["opinion"] or "", names),
                          "date": e["date"], "committee": e["committee"]} for e in theme["evidence"]]}


def _candidate_name(theme: game.PoolTheme) -> str:
    _, candidate = find_candidate(theme.party_id, theme.position)
    return candidate["name"] if candidate else ""


@router.post("/api/game/next")
def game_next(request: NextCardsRequest):
    """Up to `count` cards the visitor has not seen, balanced across parties; [] when none are left."""
    pool = theme_pool()
    check_theme_ids(request.seen, pool, "seen")
    seen, dropped = _split_known(request.seen, pool)
    picked = game.pick_cards(pool, seen, request.count)
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        names = mk_activity.mk_names(conn, sorted({pool[i].mk_id for i in picked}), ACTIVITY_KNESSET)
        cards = [_card(conn, theme_id, game.redaction_names(names.get(pool[theme_id].mk_id, []) +
                                                            [_candidate_name(pool[theme_id])]))
                 for theme_id in picked]
    finally:
        conn.close()
    return {"cards": cards, "dropped": dropped}


def _theme_ref(theme: game.PoolTheme) -> dict:
    _, candidate = find_candidate(theme.party_id, theme.position)
    return {"theme_id": theme.theme_id, "title": theme.title,
            "candidate": {**candidate_card(theme.party_id, candidate),
                          "profile_url": f"{PROFILE_PAGE_PATHS[0]}/party/{theme.party_id}/candidate/{theme.position}"}}


@router.post("/api/game/results")
def game_results(request: ResultsRequest):
    """Per party, the themes the visitor agreed and disagreed with and whose they are; best_party_id agreed most."""
    pool = theme_pool()
    check_theme_ids(request.agreed + request.disagreed, pool, "theme")
    agreed, dropped_agreed = _split_known(request.agreed, pool)
    disagreed, dropped_disagreed = _split_known(request.disagreed, pool)
    results = game.game_results(pool, agreed, disagreed)
    parties = []
    for party in results["parties"]:
        parties.append({"party": party_card(find_party(party["party_id"])),
                        "agreed": [_theme_ref(pool[i]) for i in party["agreed"]],
                        "disagreed": [_theme_ref(pool[i]) for i in party["disagreed"]]})
    return {"parties": parties, "best_party_id": results["best_party_id"],
            "dropped": dropped_agreed + dropped_disagreed}
