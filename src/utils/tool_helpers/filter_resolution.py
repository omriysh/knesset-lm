"""Server-side resolution of the query_protocols filters (party, committees, mk_id given as a name)
to the exact strings stored in knesset.db, plus the cached filter vocabulary they resolve against.

The vocabulary is read with a handful of small queries and cached per db file, its mtime and the
Knesset numbers, so resolution and the empty-result diagnostics cost dictionary lookups.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import config
from rapidfuzz import fuzz, process
from retrieval import knesset_db_store as store
from utils.tool_helpers.fuzzy_name_index import FuzzyNameIndex

_QUOTE_TRANSLATION = str.maketrans({"״": '"', "“": '"', "”": '"', "׳": "'", "‘": "'", "’": "'", "`": "'"})
_PARTY_KEY_DROPPED_RE = re.compile(r"[\"'()\[\]]")
_PARTY_KEY_SEPARATOR_RE = re.compile(r"[\s\-–—־,]+")
_HEBREW_DEFINITE_ARTICLE = "ה"
_VOCABULARY_CACHE_MAX_ENTRIES = 8


def normalized_name_key(text: str) -> str:
    """Quotes unified, whitespace collapsed: 'ועדת  המדע  והטכנולוגיה ' → 'ועדת המדע והטכנולוגיה'."""
    return " ".join(str(text).translate(_QUOTE_TRANSLATION).split())


def normalized_party_key(text: str) -> str:
    """Quotes and brackets dropped, dashes as spaces, case folded: 'חד"ש-תע"ל' → 'חדש תעל'."""
    without_quotes = _PARTY_KEY_DROPPED_RE.sub("", str(text).translate(_QUOTE_TRANSLATION))
    return " ".join(_PARTY_KEY_SEPARATOR_RE.split(without_quotes)).strip().casefold()


def _same_word_ignoring_definite_article(first: str, second: str) -> bool:
    return (first == second or first == _HEBREW_DEFINITE_ARTICLE + second
            or _HEBREW_DEFINITE_ARTICLE + first == second)


PARTY_WHOLE_WORDS_SCORE = 0.9


def _alias_targets_by_key() -> dict[str, str]:
    return {normalized_party_key(alias): canonical for alias, canonical in config.PARTY_ALIASES.items()}


def score_party_names(query: str, party_names: list[str]) -> list[tuple[str, float]]:
    """Every party name with a 0-1 score for query, best first.

    1.0: same normalized name or a config.PARTY_ALIASES hit; 0.9: every query word is a whole
    word of the name (ה-prefix insensitive, 'עוצמה יהודית'); else the RapidFuzz ratio of the
    normalized names, so a word fragment ('ישר') scores low.
    """
    query_key = normalized_party_key(query)
    if not query_key:
        return []
    alias_target = _alias_targets_by_key().get(query_key)
    query_words = query_key.split(" ")
    scored: list[tuple[str, float]] = []
    for name in party_names:
        name_key = normalized_party_key(name)
        name_words = name_key.split(" ")
        if name_key == query_key or name == alias_target:
            score = 1.0
        elif all(any(_same_word_ignoring_definite_article(q, n) for n in name_words) for q in query_words):
            score = PARTY_WHOLE_WORDS_SCORE
        else:
            score = fuzz.ratio(query_key, name_key) / 100.0
        scored.append((name, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


@dataclass(frozen=True)
class PartyResolution:
    party: str | None
    method: str


def resolve_party(query: str, party_names: list[str]) -> PartyResolution:
    """Exact, then normalized / alias, then an unambiguous fuzzy match; never a guess and never a
    list of closest names (suggesting parties for an unknown name reads as political steering)."""
    if query in party_names:
        return PartyResolution(query, "exact")
    scored = score_party_names(query, party_names)
    if not scored:
        return PartyResolution(None, "unknown")
    best_name, best_score = scored[0]
    runner_up_score = scored[1][1] if len(scored) > 1 else 0.0
    if best_score == 1.0 and runner_up_score < 1.0:
        return PartyResolution(best_name, "alias")
    if best_score >= config.PARTY_MATCH_MIN_SCORE and best_score - runner_up_score >= config.NAME_FILTER_UNAMBIGUOUS_GAP:
        return PartyResolution(best_name, "fuzzy")
    return PartyResolution(None, "ambiguous" if best_score >= config.PARTY_MATCH_MIN_SCORE else "unknown")


def matching_party_names(query: str, party_names: list[str]) -> list[tuple[str, float]]:
    """find_party matches: the resolved party, else the parties whose name holds every query word
    ('הדתית' may name several); a fuzzy near-miss is never returned as a match."""
    resolution = resolve_party(query, party_names)
    scored = score_party_names(query, party_names)
    if resolution.party is not None:
        return [(name, score) for name, score in scored if name == resolution.party][:1] or [(resolution.party, 1.0)]
    return [(name, score) for name, score in scored if score >= PARTY_WHOLE_WORDS_SCORE]


def requested_knesset_nums(knesset_num: int | None) -> tuple[int, ...]:
    """The Knessets a protocol query covers: the requested one, or every processed Knesset when omitted."""
    return (int(knesset_num),) if knesset_num else tuple(config.PROTOCOL_KNESSET_NUMS)


def knessets_text(knesset_nums) -> str:
    """'Knesset 25' or 'Knessets 24, 25', for messages."""
    nums = store.knesset_num_list(knesset_nums)
    return f"Knesset {nums[0]}" if len(nums) == 1 else f"Knessets {', '.join(map(str, nums))}"


@dataclass
class FilterVocabulary:
    knesset_nums: tuple[int, ...]
    knesset_nums_with_meetings: list[int]
    meeting_date_range: tuple[str | None, str | None]
    party_member_counts: dict[str, int]
    committee_meeting_counts: dict[str, int]
    committee_names_by_key: dict[str, list[str]]
    committee_name_by_id: dict[str, str]
    mk_name_by_id: dict[str, str]
    mk_name_entries: list[dict]
    _committee_prefix_index: FuzzyNameIndex | None = None
    _mk_index: FuzzyNameIndex | None = None

    def committee_prefix_index(self) -> FuzzyNameIndex:
        if self._committee_prefix_index is None:
            self._committee_prefix_index = FuzzyNameIndex(
                [{"id": key, "label": key, "body": key, "extra": {}} for key in self.committee_names_by_key])
        return self._committee_prefix_index

    def mk_index(self) -> FuzzyNameIndex:
        if self._mk_index is None:
            self._mk_index = FuzzyNameIndex(self.mk_name_entries, require_query_token_coverage=True)
        return self._mk_index


_vocabulary_cache: dict[tuple, FilterVocabulary] = {}


def _db_file_version(conn) -> tuple[str, int]:
    db_file = conn.execute("PRAGMA database_list").fetchone()[2] or ""
    try:
        return db_file, os.stat(db_file).st_mtime_ns
    except OSError as exc:
        print(f"[filter_resolution] cannot stat {db_file!r}, vocabulary is not cached by mtime: {exc}")
        return db_file, -1


def filter_vocabulary(conn, knesset_nums: int | tuple[int, ...]) -> FilterVocabulary:
    knesset_nums = tuple(store.knesset_num_list(knesset_nums))
    db_file, db_mtime_ns = _db_file_version(conn)
    cache_key = (db_file, db_mtime_ns, knesset_nums)
    if db_mtime_ns != -1 and cache_key in _vocabulary_cache:
        return _vocabulary_cache[cache_key]
    vocabulary = _read_vocabulary(conn, knesset_nums)
    if len(_vocabulary_cache) >= _VOCABULARY_CACHE_MAX_ENTRIES:
        _vocabulary_cache.clear()
    _vocabulary_cache[cache_key] = vocabulary
    return vocabulary


def _read_vocabulary(conn, knesset_nums: tuple[int, ...]) -> FilterVocabulary:
    knesset_sql, knesset_params = store.knesset_condition("knesset_num", knesset_nums)
    committee_meeting_counts = {row[0]: row[1] for row in conn.execute(
        f"SELECT committee, COUNT(*) FROM meetings WHERE {knesset_sql} AND committee IS NOT NULL GROUP BY committee",
        knesset_params)}
    committee_name_by_id = {str(row[0]): row[1] for row in conn.execute(
        f"SELECT committee_id, name FROM committees WHERE {knesset_sql}", knesset_params)}
    committee_names_by_key: dict[str, list[str]] = {}
    for name in [*committee_meeting_counts, *committee_name_by_id.values()]:
        names = committee_names_by_key.setdefault(normalized_name_key(name), [])
        if name not in names:
            names.append(name)
    date_range = conn.execute(f"SELECT MIN(date), MAX(date) FROM meetings WHERE {knesset_sql}",
                              knesset_params).fetchone()
    mk_name_entries = store.name_entries(conn, "mks", knesset_nums)
    return FilterVocabulary(
        knesset_nums=knesset_nums,
        knesset_nums_with_meetings=[row[0] for row in conn.execute(
            "SELECT DISTINCT knesset_num FROM meetings ORDER BY knesset_num")],
        meeting_date_range=(date_range[0], date_range[1]),
        party_member_counts={row[0]: row[1] for row in conn.execute(
            f"SELECT party, COUNT(*) FROM mks WHERE {knesset_sql} AND party IS NOT NULL GROUP BY party",
            knesset_params)},
        committee_meeting_counts=committee_meeting_counts,
        committee_names_by_key=committee_names_by_key,
        committee_name_by_id=committee_name_by_id,
        mk_name_by_id={entry["id"]: entry["label"] for entry in mk_name_entries},
        mk_name_entries=mk_name_entries,
    )


def resolve_party_in_each_knesset(conn, query: str, knesset_nums) -> list[str]:
    """The names a party query resolves to, one per Knesset where it resolves (party names differ between
    Knessets: ש"ס is 'ש"ס' in one and a long name in another), latest Knesset first, deduplicated."""
    names: list[str] = []
    for knesset_num in reversed(store.knesset_num_list(knesset_nums)):
        resolution = resolve_party(query, list(filter_vocabulary(conn, knesset_num).party_member_counts))
        if resolution.party is not None and resolution.party not in names:
            names.append(resolution.party)
    return names


@dataclass(frozen=True)
class CommitteeResolution:
    requested: str
    db_names: list[str]
    method: str
    candidates: list[str] = field(default_factory=list)


def closest_names(query: str, names: list[str], limit: int = 3) -> list[str]:
    return [match[0] for match in process.extract(query, names, scorer=fuzz.ratio, limit=limit)]


def resolve_committee(requested: str, vocabulary: FilterVocabulary) -> CommitteeResolution:
    """A committee name (any whitespace) or numeric committee_id → every exact meetings.committee
    / committees.name string with that whitespace-normalized name; an unambiguous name prefix
    ('ועדת החוקה') also resolves. Unresolved: db_names empty, candidates = the 3 closest names."""
    requested = str(requested).strip()
    key = normalized_name_key(requested)
    if requested.isdigit():
        committee_name = vocabulary.committee_name_by_id.get(requested)
        if committee_name is None:
            return CommitteeResolution(requested, [], "unknown_id")
        key = normalized_name_key(committee_name)
    if key in vocabulary.committee_names_by_key:
        method = "committee_id" if requested.isdigit() else (
            "exact" if requested in vocabulary.committee_names_by_key[key] else "normalized")
        return CommitteeResolution(requested, list(vocabulary.committee_names_by_key[key]), method)
    prefix_key = vocabulary.committee_prefix_index().unambiguous_label_match(key)
    if prefix_key is not None:
        return CommitteeResolution(requested, list(vocabulary.committee_names_by_key[prefix_key]), "prefix")
    return CommitteeResolution(requested, [], "unknown",
                               closest_names(key, list(vocabulary.committee_names_by_key)))


@dataclass(frozen=True)
class MkNameResolution:
    mk_id: str | None
    full_name: str | None
    candidates: list[dict] = field(default_factory=list)


def resolve_mk_name(query: str, vocabulary: FilterVocabulary) -> MkNameResolution:
    """An MK name → mk_id when exactly one MK of the Knessets matches confidently."""
    index = vocabulary.mk_index()
    exact_mk_id = index.unambiguous_label_match(query)
    if exact_mk_id is not None:
        return MkNameResolution(exact_mk_id, vocabulary.mk_name_by_id.get(exact_mk_id))
    hits = index.search(query, top_k=3)
    candidates = [{"mk_id": hit["id"], "full_name": hit["label"], "score": round(hit["score"], 2)} for hit in hits]
    if hits and hits[0]["score"] >= config.MK_NAME_FILTER_MIN_SCORE:
        runner_up_score = hits[1]["score"] if len(hits) > 1 else 0.0
        if hits[0]["score"] - runner_up_score >= config.NAME_FILTER_UNAMBIGUOUS_GAP:
            return MkNameResolution(hits[0]["id"], hits[0]["label"], candidates)
    return MkNameResolution(None, None, candidates)


__all__ = [
    "CommitteeResolution", "FilterVocabulary", "MkNameResolution", "PartyResolution",
    "closest_names", "filter_vocabulary", "normalized_name_key", "normalized_party_key",
    "knessets_text", "matching_party_names", "requested_knesset_nums", "resolve_committee", "resolve_mk_name",
    "resolve_party", "resolve_party_in_each_knesset", "score_party_names",
]
