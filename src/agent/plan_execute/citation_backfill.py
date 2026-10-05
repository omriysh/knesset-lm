"""
Fill protocol citations from the evidence they cite.

The synthesizer LLM copies each citation's quote by hand and sometimes drops fields the UI
needs to show, open and highlight the source (the opinion or speech text, an opinion's verbatim
`quote`, `speech_idx`). Each
cited protocol row is matched to its query_protocols evidence row; missing fields are copied
from it and `source_kind` (opinion / topic / speech) is added. Values the LLM wrote are kept.
"""

import json

SOURCE_KIND_BY_SCOPE = {"opinions": "opinion", "topics": "topic", "speeches": "speech"}
TEXT_FIELD_BY_SOURCE_KIND = {"opinion": "opinion", "topic": "topic", "speech": "text"}
BACKFILLED_FIELDS = ("committee", "date", "speech_idx", "quote", "speaker")


def protocol_rows_in_tool_result(full: str) -> list[tuple[str, dict]]:
    if not full.lstrip().startswith("{"):
        return []
    try:
        parsed = json.loads(full)
    except ValueError as exc:
        print(f"[citation_backfill] tool result is not valid JSON, skipped: {exc}", flush=True)
        return []
    if not isinstance(parsed, dict):
        return []
    return [(source_kind, row)
            for scope, source_kind in SOURCE_KIND_BY_SCOPE.items()
            for row in parsed.get(scope) or []
            if isinstance(row, dict) and row.get("meeting_id") is not None]


def protocol_rows_of_evidence(entry) -> list[tuple[str, dict]]:
    rows = protocol_rows_in_tool_result(entry.envelope.full or "")
    for call_result in (entry.envelope.provenance or {}).get("tool_call_results") or []:
        rows += protocol_rows_in_tool_result(call_result.get("full") or "")
    return rows


def cited_source_kind(node: dict) -> str | None:
    for source_kind, text_field in TEXT_FIELD_BY_SOURCE_KIND.items():
        if node.get(text_field):
            return source_kind
    return None


def matching_evidence_row(node: dict, rows: list[tuple[str, dict]]) -> tuple[str, dict] | None:
    source_kind_hint = cited_source_kind(node)
    candidates = [(kind, row) for kind, row in rows
                  if str(row["meeting_id"]) == str(node["meeting_id"])
                  and (source_kind_hint is None or kind == source_kind_hint)]
    for key in ("speech_idx", "idx"):
        if node.get(key) is not None:
            candidates = [(kind, row) for kind, row in candidates if str(row.get(key)) == str(node[key])]
    if len(candidates) > 1 and source_kind_hint is not None:
        cited_text = str(node[TEXT_FIELD_BY_SOURCE_KIND[source_kind_hint]])
        same_text = [(kind, row) for kind, row in candidates
                     if str(row.get(TEXT_FIELD_BY_SOURCE_KIND[kind], "")).startswith(cited_text[:40])]
        candidates = same_text or candidates
    return candidates[0] if candidates else None


def cited_protocol_nodes(quote):
    if isinstance(quote, list):
        for item in quote:
            yield from cited_protocol_nodes(item)
    elif isinstance(quote, dict):
        if quote.get("meeting_id") is not None:
            yield quote
        for chunk in quote.get("chunks") or []:
            yield from cited_protocol_nodes(chunk)


def backfill_protocol_citations(citations: list, store) -> None:
    rows_by_evidence_id: dict[str, list] = {}
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        evidence_id = citation.get("ev_id")
        if evidence_id not in rows_by_evidence_id:
            entry = store.get(evidence_id) if isinstance(evidence_id, str) else None
            rows_by_evidence_id[evidence_id] = protocol_rows_of_evidence(entry) if entry else []
        rows = rows_by_evidence_id[evidence_id]
        if not rows:
            continue
        for node in cited_protocol_nodes(citation.get("quote")):
            match = matching_evidence_row(node, rows)
            if match is None:
                continue
            source_kind, row = match
            for field in (*BACKFILLED_FIELDS, TEXT_FIELD_BY_SOURCE_KIND[source_kind]):
                if node.get(field) in (None, "") and row.get(field) not in (None, ""):
                    node[field] = row[field]
            node["source_kind"] = source_kind
