"""Thin envelope-wrappers over :mod:`utils.knesset_db`.

Per design §5.2: every research tool returns a :class:`ToolEnvelope`. The
``utils/knesset_db`` layer pre-dates that contract and returns plain dicts
or lists of dicts. These adapters bridge the two without leaking
research-agent-specific values into ``utils/`` (the layer must stay
agent-agnostic per the import-rules section of the project CLAUDE.md).

Each ``adapt_*`` function:
  * accepts the same inputs the tool handler will pass through,
  * calls the appropriate ``utils.knesset_db`` public symbol,
  * wraps the result in a ``ToolEnvelope`` with a tool-specific ``kind``
    and ``source``, the result count, and a structured ``provenance``.

Adapters never raise — call-site failures are surfaced as
``ToolEnvelope(error=...)`` so the executor LLM can see them in the same
shape every other tool reports.
"""

from __future__ import annotations

import json
import traceback

from agent.subgraph.evidence import ToolEnvelope
from utils.knesset_db import (
    _get_active_committee_members_by_id,
    get_all_committees,
    get_person_votes,
    get_person_votes_on_topic,
    search_plenum_votes,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ok(
    payload,
    *,
    kind: str,
    source: str,
    provenance: dict | None = None,
    warnings: list[str] | None = None,
) -> ToolEnvelope:
    """Build a successful :class:`ToolEnvelope` from a raw payload.

    ``payload`` may be a dict or a list. ``count`` reflects the number of
    top-level records: 1 for a dict, ``len(payload)`` for a list, 0 for
    None/empty values.
    """
    if payload is None:
        count = 0
    elif isinstance(payload, list):
        count = len(payload)
    else:
        count = 1

    metadata: dict = {
        "kind":   kind,
        "source": source,
        "count":  count,
    }
    if warnings:
        metadata["warnings"] = list(warnings)

    return ToolEnvelope(
        summary="",
        full=json.dumps(payload, ensure_ascii=False, default=str),
        metadata=metadata,
        provenance=provenance or {},
    )


def _err(error_code: str, *, kind: str, source: str, **prov) -> ToolEnvelope:
    """Build an error :class:`ToolEnvelope` with an empty payload."""
    return ToolEnvelope(
        summary="",
        full="",
        metadata={"kind": kind, "source": source, "count": 0},
        provenance=dict(prov),
        error=error_code,
    )


def _safely(fn, *, kind: str, source: str, **prov):
    """Run ``fn`` and convert any unexpected exception into an error envelope."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — surface to envelope, never crash
        print(f"[adapters] {source} call failed: {exc}", flush=True)
        env = _err("adapter_exception", kind=kind, source=source, **prov)
        env.metadata["exception"] = str(exc)
        env.metadata["traceback"] = traceback.format_exc()
        return env


# ---------------------------------------------------------------------------
# Voting tools
# ---------------------------------------------------------------------------


def paging_metadata(*, offset: int, returned: int, page_size: int, total: int | None) -> dict:
    """Envelope ``metadata["paging"]``: which rows were returned and whether more exist."""
    has_more = offset + returned < total if total is not None else returned >= page_size
    paging = {"offset": offset, "returned": returned, "has_more": has_more}
    if total is not None:
        paging["total"] = total
    return paging


def adapt_query_votes(
    *,
    topic: str,
    person_id: int | None,
    knesset_num: int | None,
    offset: int = 0,
    page_size: int = 20,
) -> ToolEnvelope:
    """Plenum votes matching ``topic`` (all votes when empty), with ``person_id``'s result on each
    when given; one Knesset or all of them when ``knesset_num`` is None. An empty page is a success."""
    kind = "fetch" if person_id is not None else "search"
    provenance = {"topic": topic, "person_id": person_id, "knesset_num": knesset_num,
                  "offset": offset, "page_size": page_size}

    def _run() -> ToolEnvelope:
        if person_id is None:
            votes, total = search_plenum_votes(topic or None, knesset_num, offset, page_size)
        elif topic:
            votes, total = get_person_votes_on_topic(person_id, topic, knesset_num, offset, page_size)
        else:
            votes, total = get_person_votes(person_id, knesset_num, offset, page_size)
        envelope = _ok(votes, kind=kind, source="odata", provenance=provenance)
        envelope.metadata["paging"] = paging_metadata(offset=offset, returned=len(votes),
                                                      page_size=page_size, total=total)
        return envelope

    return _safely(_run, kind=kind, source="odata", **provenance)


# ---------------------------------------------------------------------------
# Helpers used by name-search-backed tools
# ---------------------------------------------------------------------------


def fetch_committee_record(committee_id: str, knesset_num: int | None = None) -> dict | None:
    """Return the committee record matching ``committee_id`` or None.

    Used as the ``fetch_by_id`` callback for ``find_committee``. The committee list
    endpoint is not filterable by id, so this walks the (cached) committee list of
    *knesset_num*, or of the current and upcoming Knessets when it is not given.
    """
    try:
        cid = int(committee_id)
    except (TypeError, ValueError) as exc:
        print(f"[adapters] committee id {committee_id!r} is not a number: {exc}", flush=True)
        return None

    for committee_knesset_num in (25, 26) if knesset_num is None else (knesset_num,):
        try:
            for c in get_all_committees(committee_knesset_num):
                if int(c.get("CommitteeID") or 0) == cid:
                    record = {
                        "committee_id": str(c.get("CommitteeID") or ""),
                        "name":         c.get("Name") or "",
                        "knesset_num":  c.get("KnessetNum"),
                        "is_current":   c.get("IsCurrent"),
                    }
                    try:
                        record["members"] = _get_active_committee_members_by_id(
                            cid, committee_knesset_num, current_only=bool(c.get("IsCurrent")))
                    except Exception as exc:
                        print(f"[adapters] committee {cid} members fetch failed: {exc}", flush=True)
                    return record
        except Exception as exc:
            print(f"[adapters] committee list (knesset {committee_knesset_num}) fetch failed: {exc}", flush=True)
            continue
    return None


__all__ = [
    "adapt_query_votes",
    "paging_metadata",
    "fetch_committee_record",
]
