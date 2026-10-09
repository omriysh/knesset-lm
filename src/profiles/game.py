"""
profiles/game.py

The "היכרות" game: the visitor swipes MK themes with the names blacked out, agreeing or disagreeing,
then sees which parties they agreed with. The pool is every theme of a candidate with a full profile,
keyed by theme id, with the candidate's 26th-list party. Cards are picked party first, then candidate,
then theme, so parties with many former MKs are not shown more.
"""

import random
import re
from dataclasses import dataclass

REDACTED = "⟦█⟧"
MIN_THEME_OPINIONS = 3
_HEBREW = "א-ת"
_PREFIXES = "ובלמשהכ"


@dataclass(frozen=True)
class PoolTheme:
    theme_id: int
    party_id: int
    position: int
    mk_id: str
    title: str
    opinion_count: int


def build_pool(candidates: list[tuple[int, int, str]], themes: list[tuple[int, str, str, int]]) -> dict[int, PoolTheme]:
    """candidates: (party_id, position, mk_id); themes: (theme id, mk_id, title, opinion_count)."""
    owner_of = {mk_id: (party_id, position) for party_id, position, mk_id in candidates}
    return {theme_id: PoolTheme(theme_id, *owner_of[mk_id], mk_id, title, opinion_count)
            for theme_id, mk_id, title, opinion_count in themes if mk_id in owner_of}


def pick_cards(pool: dict[int, PoolTheme], seen: list[int], count: int, rng: random.Random | None = None) -> list[int]:
    """
    Up to count unseen playable theme ids: a party uniformly among those with unseen themes (not the party of
    the last card when another has some), a candidate uniformly in it, then a uniformly random theme of theirs.
    """
    rng = rng or random.Random()
    seen_ids = set(seen)
    unseen: dict[int, dict[int, list[int]]] = {}
    for theme in pool.values():
        if theme.theme_id not in seen_ids and theme.opinion_count >= MIN_THEME_OPINIONS:
            unseen.setdefault(theme.party_id, {}).setdefault(theme.position, []).append(theme.theme_id)
    last_party = pool[seen[-1]].party_id if seen and seen[-1] in pool else None
    picked = []
    for _ in range(count):
        if not unseen:
            break
        parties = sorted(unseen)
        party_id = rng.choice([p for p in parties if p != last_party] or parties)
        position = rng.choice(sorted(unseen[party_id]))
        theme_ids = unseen[party_id][position]
        theme_id = theme_ids.pop(rng.randrange(len(theme_ids)))
        if not theme_ids:
            del unseen[party_id][position]
            if not unseen[party_id]:
                del unseen[party_id]
        picked.append(theme_id)
        last_party = party_id
    return picked


def redaction_names(names: list[str]) -> list[str]:
    """The MK's names to black out, longest first, so a full name is hidden whole before its parts."""
    return sorted({name.strip() for name in names if len(name.strip()) > 1}, key=len, reverse=True)


def redact(text: str, names: list[str]) -> str:
    """Each whole-word name, after up to two Hebrew prefix letters, becomes REDACTED (the prefix is kept)."""
    if not text or not names:
        return text
    pattern = re.compile(f"(?<![{_HEBREW}])([{_PREFIXES}]{{0,2}})(?:{'|'.join(map(re.escape, names))})(?![{_HEBREW}])")
    return pattern.sub(lambda match: match.group(1) + REDACTED, text)


def game_results(pool: dict[int, PoolTheme], agreed: list[int], disagreed: list[int]) -> dict:
    """
    Per party: the agreed and disagreed theme ids. Parties are ordered by agreed count, then by fewer
    disagreed; best_party_id is the first one with any agreed theme (None when nothing was agreed).
    """
    parties: dict[int, dict] = {}
    for key, ids in (("agreed", agreed), ("disagreed", disagreed)):
        for theme_id in ids:
            party = parties.setdefault(pool[theme_id].party_id, {"agreed": [], "disagreed": []})
            party[key].append(theme_id)
    ordered = sorted(parties.items(), key=lambda item: (-len(item[1]["agreed"]), len(item[1]["disagreed"]), item[0]))
    best = ordered[0][0] if ordered and ordered[0][1]["agreed"] else None
    return {"parties": [{"party_id": party_id, **themes} for party_id, themes in ordered], "best_party_id": best}
