"""Why did a query_protocols call return nothing? Cheap checks (LIMIT 1 queries and lookups in the
cached filter vocabulary) that turn an empty result into actionable English messages.

Each diagnostic is {filter, value, problem, message, suggestions}; handlers put the list in
envelope.metadata["diagnostics"] and append the messages to envelope.summary.
"""

from __future__ import annotations

import re

import config
from retrieval import knesset_db_store as store
from utils.tool_helpers.filter_resolution import (
    FilterVocabulary,
    filter_vocabulary,
    knessets_text,
    requested_knesset_nums,
    resolve_committee,
    resolve_mk_name,
    resolve_party_in_each_knesset,
)

_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_HEBREW_LETTER_RE = re.compile(r"[א-ת]")
_SUMMARY_SCOPES = ("topics", "opinions")
AND_ED_KEY_WORDS_ADVICE = "All key words are AND-ed: try 1-2 words or a synonym."


def diagnostic(filter_name: str, value, problem: str, message: str, suggestions: list | None = None) -> dict:
    return {"filter": filter_name, "value": value, "problem": problem, "message": message,
            "suggestions": list(suggestions or [])}


def _quoted_list(values: list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def is_latin_only_query(query: str) -> bool:
    return bool(_LATIN_LETTER_RE.search(query)) and not _HEBREW_LETTER_RE.search(query)


def latin_query_diagnostic(query: str) -> dict:
    return diagnostic("query", query, "non_hebrew_query",
                      f"The query '{query}' has Latin letters and no Hebrew; the data is in Hebrew, "
                      f"so search with Hebrew words.")


def ordinal(number: int) -> str:
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def unknown_party_message(party: str, knesset_nums: int | tuple[int, ...]) -> str:
    """The fact only: no closest-party suggestions, which would introduce political bias."""
    ordinals = [ordinal(n) for n in store.knesset_num_list(knesset_nums)]
    knessets = ordinals[0] if len(ordinals) == 1 else f"{', '.join(ordinals[:-1])} or {ordinals[-1]}"
    return f"Party '{party}' is not a faction of the {knessets} Knesset."


def unknown_party_diagnostic(party: str, knesset_nums: int | tuple[int, ...]) -> dict:
    return diagnostic("party", party, "unknown_party", unknown_party_message(party, knesset_nums))


def unknown_committee_diagnostic(committee: str, candidates: list[str]) -> dict:
    return diagnostic("committees", committee, "unknown_committee",
                      f"Committee '{committee}' matches no committee with meetings; closest: "
                      f"{_quoted_list(candidates)}. Pass one of them (or call find_committee).",
                      candidates)


def unresolved_mk_name_diagnostic(mk_name: str, knesset_nums: int | tuple[int, ...], candidates: list[dict]) -> dict:
    names = [f"{c['full_name']} (mk_id {c['mk_id']})" for c in candidates]
    closest = f"; closest: {', '.join(names)}" if names else ""
    return diagnostic("mk_id", mk_name, "unresolved_mk_name",
                      f"mk_id '{mk_name}' is not a number and matches no single MK of {knessets_text(knesset_nums)}"
                      f"{closest}. Pass the numeric mk_id from find_mk.",
                      [c["mk_id"] for c in candidates])


def diagnose_empty_protocol_query(conn, args: dict) -> list[dict]:
    """Diagnostics for a query_protocols call whose every requested scope returned 0 rows."""
    knesset_nums = requested_knesset_nums(args.get("knesset_num"))
    query = (args.get("query") or "").strip()
    search_in = list(args.get("search_in") or store.PROTOCOL_SCOPES)
    vocabulary = filter_vocabulary(conn, knesset_nums)

    knessets_without_data = [n for n in knesset_nums if n not in vocabulary.knesset_nums_with_meetings]
    if knessets_without_data:
        covered = ", ".join(str(n) for n in vocabulary.knesset_nums_with_meetings)
        return [diagnostic("knesset_num", knessets_without_data[0], "knesset_no_data",
                           f"{knessets_text(knessets_without_data)} has no protocol data; only Knesset {covered} "
                           f"protocols are in the DB.", vocabulary.knesset_nums_with_meetings)]

    diagnostics: list[dict] = []
    diagnostics += _date_diagnostics(args.get("date_from"), args.get("date_to"), vocabulary)
    diagnostics += _meeting_diagnostics(conn, args.get("meeting_ids") or [], search_in, knesset_nums)
    resolved_filters, filter_diagnostics = _resolved_filters(conn, args, vocabulary)
    diagnostics += filter_diagnostics
    if query and is_latin_only_query(query):
        diagnostics.append(latin_query_diagnostic(query))
    if diagnostics:
        return diagnostics
    return _rows_without_key_words_diagnostics(conn, query, search_in, knesset_nums, resolved_filters, vocabulary)


def _date_diagnostics(date_from: str | None, date_to: str | None, vocabulary: FilterVocabulary) -> list[dict]:
    first_meeting_date, last_meeting_date = vocabulary.meeting_date_range
    if date_from and date_to and date_from > date_to:
        return [diagnostic("date_from", f"{date_from}..{date_to}", "date_range_inverted",
                           f"date_from {date_from} is after date_to {date_to}; swap them.")]
    if (date_to and first_meeting_date and date_to < first_meeting_date) or (
            date_from and last_meeting_date and date_from > last_meeting_date):
        return [diagnostic("date_from", f"{date_from or ''}..{date_to or ''}", "date_out_of_coverage",
                           f"No meetings in {date_from or 'the start'}..{date_to or 'today'}: "
                           f"{knessets_text(vocabulary.knesset_nums)} protocols cover {first_meeting_date} to {last_meeting_date}.",
                           [first_meeting_date, last_meeting_date])]
    return []


def _meeting_diagnostics(conn, meeting_ids: list, search_in: list[str], knesset_nums: tuple[int, ...]) -> list[dict]:
    diagnostics: list[dict] = []
    only_summary_scopes = all(scope in _SUMMARY_SCOPES for scope in search_in)
    for meeting_id in [str(m) for m in meeting_ids]:
        meeting = store.get_meeting(conn, meeting_id)
        if meeting is None:
            diagnostics.append(diagnostic("meeting_ids", meeting_id, "meeting_not_found",
                                          f"meeting_id {meeting_id} does not exist in the DB; take meeting ids "
                                          f"from earlier query_protocols rows."))
        elif meeting.get("knesset_num") not in knesset_nums:
            diagnostics.append(diagnostic("meeting_ids", meeting_id, "meeting_in_other_knesset",
                                          f"Meeting {meeting_id} belongs to Knesset {meeting.get('knesset_num')}; "
                                          f"pass knesset_num={meeting.get('knesset_num')}."))
        elif meeting.get("is_protocol") == 0:
            diagnostics.append(diagnostic("meeting_ids", meeting_id, "meeting_not_a_protocol",
                                          f"Meeting {meeting_id} is a bill or background document, not a protocol, "
                                          f"and is excluded from query_protocols."))
        elif not meeting.get("summary_path") and only_summary_scopes:
            diagnostics.append(diagnostic("meeting_ids", meeting_id, "meeting_has_no_summary",
                                          f'Meeting {meeting_id} has no AI summary (no topics or opinions); '
                                          f'search its transcript with search_in=["speeches"].', ["speeches"]))
    return diagnostics


def _resolved_filters(conn, args: dict, vocabulary: FilterVocabulary) -> tuple[dict, list[dict]]:
    """The mk_id / party / committees filters as exact DB values, plus a diagnostic per unresolvable one."""
    knesset_nums = vocabulary.knesset_nums
    diagnostics: list[dict] = []
    resolved = {"mk_id": None, "party": None, "committees": None,
                "meeting_ids": [str(m) for m in args.get("meeting_ids") or []] or None,
                "date_from": args.get("date_from") or None, "date_to": args.get("date_to") or None}

    mk_id = str(args.get("mk_id") or "").strip()
    if mk_id.isdigit():
        if mk_id in vocabulary.mk_name_by_id:
            resolved["mk_id"] = mk_id
        else:
            diagnostics.append(diagnostic("mk_id", mk_id, "mk_not_in_knesset",
                                          f"mk_id {mk_id} is not an MK of {knessets_text(knesset_nums)} (the DB covers "
                                          f"the MKs of {knessets_text(knesset_nums)} only); get the id from find_mk."))
    elif mk_id:
        resolution = resolve_mk_name(mk_id, vocabulary)
        if resolution.mk_id is None:
            diagnostics.append(unresolved_mk_name_diagnostic(mk_id, knesset_nums, resolution.candidates))
        resolved["mk_id"] = resolution.mk_id

    party = (args.get("party") or "").strip()
    if party:
        party_names = resolve_party_in_each_knesset(conn, party, knesset_nums)
        if not party_names:
            diagnostics.append(unknown_party_diagnostic(party, knesset_nums))
        resolved["party"] = party_names or None

    committee_names: list[str] = []
    for committee in args.get("committees") or []:
        resolution = resolve_committee(str(committee).replace("_", " "), vocabulary)
        if not resolution.db_names:
            diagnostics.append(unknown_committee_diagnostic(str(committee), resolution.candidates))
        committee_names += resolution.db_names
    resolved["committees"] = committee_names or None
    return resolved, diagnostics


def _row_counts_note(conn, scopes: list[str], knesset_nums: tuple[int, ...], filters: dict,
                     vocabulary: FilterVocabulary) -> str:
    """' (MK יאיר לפיד: 210 topics, 31 opinions, 42 speeches rows)' for an MK / party filter, else ''."""
    if not (filters["mk_id"] or filters["party"]):
        return ""
    cap = config.FILTER_DIAGNOSTICS_ROW_COUNT_CAP
    counts = []
    for scope in scopes:
        count = store.count_protocol_rows(conn, scope, knesset_nums, cap=cap, **filters)
        counts.append(f"{count}{'+' if count >= cap else ''} {scope}")
    who = (f"MK {vocabulary.mk_name_by_id.get(filters['mk_id'], filters['mk_id'])}" if filters["mk_id"]
           else f"party {' / '.join(repr(name) for name in filters['party'])}")
    return f" ({who}: {', '.join(counts)} rows)"


def _rows_without_key_words_diagnostics(conn, query: str, search_in: list[str], knesset_nums: tuple[int, ...],
                                        filters: dict, vocabulary: FilterVocabulary) -> list[dict]:
    has_filters = any(filters.values())
    scopes_with_rows = [scope for scope in search_in
                        if store.count_protocol_rows(conn, scope, knesset_nums, cap=1, **filters)]
    if not scopes_with_rows:
        return [diagnostic("filters", None, "filters_no_rows",
                           f"The filters together match no {', '.join(search_in)} rows even without key words; "
                           f"drop or relax one filter (e.g. the MK may not sit on that committee, or the dates "
                           f"miss its meetings).")]
    if not query:
        return []
    if not has_filters:
        return [diagnostic("query", query, "keywords_no_match",
                           f"No {knessets_text(knesset_nums)} {', '.join(search_in)} row contains all of the key words "
                           f"'{query}'. {AND_ED_KEY_WORDS_ADVICE}", [scopes_with_rows[0]])]
    counts_note = _row_counts_note(conn, scopes_with_rows, knesset_nums, filters, vocabulary)
    return [diagnostic("query", query, "keywords_no_match",
                       f"The filters alone match rows in {', '.join(scopes_with_rows)}{counts_note}, but none "
                       f"contains all of the key words '{query}'. {AND_ED_KEY_WORDS_ADVICE}", scopes_with_rows)]


__all__ = ["diagnose_empty_protocol_query", "diagnostic", "is_latin_only_query", "latin_query_diagnostic",
           "ordinal", "unknown_committee_diagnostic", "unknown_party_diagnostic", "unknown_party_message", "unresolved_mk_name_diagnostic"]
