"""
profiles/game.py

The "היכרות" game: the visitor swipes MK themes with the names blacked out, agreeing or disagreeing,
then sees which parties they agreed with. The pool is every theme of a candidate with a full profile,
keyed by theme id, with the candidate's 26th-list party and the theme's subject and approach. Only themes
with a subject are played: cards are picked subject first (the least shown), then party (the least shown),
then theme. A swipe counts for the whole approach: every party with a theme in it gets the point.
"""

import random
import re
from collections import Counter
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
    subject_id: int | None = None
    approach_id: int | None = None


def build_pool(candidates: list[tuple[int, int, str]], themes: list[tuple[int, str, str, int]],
               theme_subjects: dict[int, tuple[int, int | None]]) -> dict[int, PoolTheme]:
    """candidates: (party_id, position, mk_id); themes: (theme id, mk_id, title, opinion_count);
    theme_subjects: theme id → (subject id, approach id)."""
    owner_of = {mk_id: (party_id, position) for party_id, position, mk_id in candidates}
    return {theme_id: PoolTheme(theme_id, *owner_of[mk_id], mk_id, title, opinion_count, *theme_subjects.get(theme_id, (None, None)))
            for theme_id, mk_id, title, opinion_count in themes if mk_id in owner_of}


def is_playable(theme: PoolTheme) -> bool:
    return theme.subject_id is not None and theme.opinion_count >= MIN_THEME_OPINIONS


def pick_cards(pool: dict[int, PoolTheme], seen: list[int], count: int, rng: random.Random | None = None,
               parties: set[int] | None = None) -> list[int]:
    """
    Up to count unseen playable theme ids (with a subject and enough opinions), of `parties` only when given, so
    only subjects where one of them has a stance come up. Each card's subject is a random
    one of those shown least so far (never shown, while any is left); its party is the one shown least so far
    among the parties with an unseen theme in that subject, a random one on a tie; then a random theme of theirs.
    """
    rng = rng or random.Random()
    seen_ids = set(seen)
    unseen: dict[int, dict[int, list[int]]] = {}
    for theme in pool.values():
        if theme.theme_id not in seen_ids and is_playable(theme) and (parties is None or theme.party_id in parties):
            unseen.setdefault(theme.subject_id, {}).setdefault(theme.party_id, []).append(theme.theme_id)
    seen_themes = [pool[theme_id] for theme_id in seen if theme_id in pool]
    subject_shown = Counter(theme.subject_id for theme in seen_themes)
    party_shown = Counter(theme.party_id for theme in seen_themes)
    picked = []
    for _ in range(count):
        if not unseen:
            break
        fewest_shows = min(subject_shown[s] for s in unseen)
        subject_id = rng.choice(sorted(s for s in unseen if subject_shown[s] == fewest_shows))
        parties = unseen[subject_id]
        fewest_shows = min(party_shown[p] for p in parties)
        party_id = rng.choice(sorted(p for p in parties if party_shown[p] == fewest_shows))
        theme_ids = parties[party_id]
        theme_id = theme_ids.pop(rng.randrange(len(theme_ids)))
        if not theme_ids:
            del parties[party_id]
            if not parties:
                del unseen[subject_id]
        picked.append(theme_id)
        subject_shown[subject_id] += 1
        party_shown[party_id] += 1
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


def _approach_themes(pool: dict[int, PoolTheme]) -> dict[tuple[int, int], dict[int, int]]:
    """(subject id, approach id) → party id → the party's theme with the most opinions in that approach."""
    by_approach: dict[tuple[int, int], dict[int, int]] = {}
    for theme in pool.values():
        if theme.approach_id is None:
            continue
        party_themes = by_approach.setdefault((theme.subject_id, theme.approach_id), {})
        current = party_themes.get(theme.party_id)
        if current is None or pool[current].opinion_count < theme.opinion_count:
            party_themes[theme.party_id] = theme.theme_id
    return by_approach


def game_results(pool: dict[int, PoolTheme], agreed: list[int], disagreed: list[int]) -> dict:
    """
    Per party, its agreed and disagreed matches: a swipe matches the swiped theme's party and every other party
    with a theme in the same approach, each as {"theme_id": that party's theme (the swiped one, else its theme
    with the most opinions in the approach), "swiped_id"}. Parties are ordered by agreed count, then by fewer
    disagreed; best_party_id is the first one with any agreed match (None when nothing was agreed).
    """
    by_approach = _approach_themes(pool)
    parties: dict[int, dict] = {}
    for key, ids in (("agreed", agreed), ("disagreed", disagreed)):
        for theme_id in ids:
            swiped = pool[theme_id]
            matches = {swiped.party_id: theme_id}
            if swiped.approach_id is not None:
                for party_id, party_theme_id in by_approach[(swiped.subject_id, swiped.approach_id)].items():
                    matches.setdefault(party_id, party_theme_id)
            for party_id, match_id in matches.items():
                parties.setdefault(party_id, {"agreed": [], "disagreed": []})[key].append({"theme_id": match_id, "swiped_id": theme_id})
    ordered = sorted(parties.items(), key=lambda item: (-len(item[1]["agreed"]), len(item[1]["disagreed"]), item[0]))
    best = ordered[0][0] if ordered and ordered[0][1]["agreed"] else None
    return {"parties": [{"party_id": party_id, **matches} for party_id, matches in ordered], "best_party_id": best}
