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
    get_mk_votes,
    get_recent_votes,
    get_votes_on_topic,
    get_votes_on_topic_by_mk,
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


def adapt_get_mk_votes(
    *,
    name: str,
    knesset_num: int = 25,
    top_n: int = 20,
) -> ToolEnvelope:
    def _run() -> ToolEnvelope:
        votes = get_mk_votes(mk_name=name, knesset_num=knesset_num, top_n=top_n)
        if not votes:
            return _err(
                "no_votes_found",
                kind="fetch",
                source="odata",
                query=name,
                knesset_num=knesset_num,
            )
        return _ok(
            votes,
            kind="fetch",
            source="odata",
            provenance={
                "mk_query":    name,
                "knesset_num": knesset_num,
                "top_n":       top_n,
            },
        )

    return _safely(
        _run,
        kind="fetch",
        source="odata",
        query=name,
        knesset_num=knesset_num,
    )


def adapt_get_votes_on_topic(*, topic: str, top_n: int = 20) -> ToolEnvelope:
    def _run() -> ToolEnvelope:
        votes = get_votes_on_topic(topic=topic, top_n=top_n)
        if not votes:
            return _err(
                "no_votes_found",
                kind="search",
                source="odata",
                topic=topic,
            )
        return _ok(
            votes,
            kind="search",
            source="odata",
            provenance={"topic": topic, "top_n": top_n},
        )

    return _safely(_run, kind="search", source="odata", topic=topic)


def adapt_get_votes_on_topic_by_mk(
    *,
    topic: str,
    name: str,
    knesset_num: int = 25,
    top_n: int = 20,
) -> ToolEnvelope:
    def _run() -> ToolEnvelope:
        votes = get_votes_on_topic_by_mk(
            topic=topic,
            mk_name=name,
            knesset_num=knesset_num,
            top_n=top_n,
        )
        if not votes:
            return _err(
                "no_votes_found",
                kind="fetch",
                source="odata",
                topic=topic,
                mk_query=name,
                knesset_num=knesset_num,
            )
        return _ok(
            votes,
            kind="fetch",
            source="odata",
            provenance={
                "topic":       topic,
                "mk_query":    name,
                "knesset_num": knesset_num,
                "top_n":       top_n,
            },
        )

    return _safely(
        _run,
        kind="fetch",
        source="odata",
        topic=topic,
        mk_query=name,
        knesset_num=knesset_num,
    )


def adapt_get_recent_votes(*, top_n: int = 10, knesset_num: int = 25) -> ToolEnvelope:
    def _run() -> ToolEnvelope:
        votes = get_recent_votes(top_n=top_n)
        return _ok(
            votes,
            kind="search",
            source="odata",
            provenance={"top_n": top_n, "knesset_num": knesset_num},
        )

    return _safely(_run, kind="search", source="odata", top_n=top_n)


# ---------------------------------------------------------------------------
# Helpers used by name-search-backed tools
# ---------------------------------------------------------------------------


def fetch_committee_record(committee_id: str) -> dict | None:
    """Return the committee record matching ``committee_id`` or None.

    Used as the ``fetch_by_id`` callback for ``find_committee``. Looks up
    the per-knesset committee list (cached) and matches by id.
    """
    try:
        cid = int(committee_id)
    except (TypeError, ValueError):
        return None

    # The /committees_kns_committee/list endpoint isn't filterable by id,
    # so we walk the knesset-level list. ``get_all_committees`` already
    # paginates / caches, and the result is small (~30 entries / Knesset).
    for knesset_num in (25, 26):  # cheap: covers current + upcoming
        try:
            for c in get_all_committees(knesset_num):
                if int(c.get("CommitteeID") or 0) == cid:
                    record = {
                        "committee_id": str(c.get("CommitteeID") or ""),
                        "name":         c.get("Name") or "",
                        "knesset_num":  c.get("KnessetNum"),
                        "is_current":   c.get("IsCurrent"),
                    }
                    try:
                        record["members"] = _get_active_committee_members_by_id(cid, knesset_num)
                    except Exception as exc:
                        print(f"[adapters] committee {cid} members fetch failed: {exc}", flush=True)
                    return record
        except Exception as exc:
            print(f"[adapters] committee list (knesset {knesset_num}) fetch failed: {exc}", flush=True)
            continue
    return None


__all__ = [
    "adapt_get_mk_votes",
    "adapt_get_votes_on_topic",
    "adapt_get_votes_on_topic_by_mk",
    "adapt_get_recent_votes",
    "fetch_committee_record",
]
