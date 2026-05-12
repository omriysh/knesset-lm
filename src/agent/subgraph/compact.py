"""Compact view builder for evidence tool results (subgraph layer).

``apply_compact`` deterministically reduces a parsed tool result using the
tool's ``compact_spec`` and the executor's per-call selection
(``key_indices`` / ``key_quotes``).

``build_compact_view`` assembles the compact payload for one evidence entry,
processing each tool call result individually so multi-tool steps work
correctly.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.subgraph.evidence import EvidenceEntry
    from utils.tools import ToolSpec


# ---------------------------------------------------------------------------
# Core compaction
# ---------------------------------------------------------------------------


def apply_compact(
    data: Any,
    compact_spec: dict,
    key_indices: list[int] | None = None,
    key_quotes: list[str] | None = None,
) -> Any:
    """Apply compaction rules to a parsed tool result payload.

    Args:
        data:         parsed result (list, dict, or str).
        compact_spec: from ``ToolSpec.compact_spec``.
        key_indices:  executor-selected 0-based positions (list/nested_list).
        key_quotes:   executor-selected text passages (text kind).
    """
    if data is None:
        return data

    kind = compact_spec.get("kind")
    if kind is None:
        if isinstance(data, str):
            kind = "text"
        elif isinstance(data, list):
            kind = "list"
        else:
            kind = "dict"

    # ── Text ────────────────────────────────────────────────────────────────
    if kind == "text" or isinstance(data, str):
        if key_quotes:
            sep = compact_spec.get("quote_separator", "\n---\n")
            return sep.join(str(q) for q in key_quotes)
        max_chars: int = compact_spec.get("max_chars", 2000)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return text[:max_chars] + ("…" if len(text) > max_chars else "")

    # ── List / nested_list ───────────────────────────────────────────────────
    if kind in ("list", "nested_list"):
        list_path: str | None = compact_spec.get("list_path")
        alt_list_path: str | None = compact_spec.get("alt_list_path")
        actual_path: str | None = None

        if kind == "nested_list" and isinstance(data, dict):
            if list_path and list_path in data:
                items: list = list(data.get(list_path) or [])
                actual_path = list_path
            elif alt_list_path and alt_list_path in data:
                items = list(data.get(alt_list_path) or [])
                actual_path = alt_list_path
            else:
                items = []
        elif isinstance(data, list):
            items = list(data)
        else:
            return data

        if key_indices:
            max_guard: int = compact_spec.get("max_items", 50)
            items = [items[i] for i in key_indices if 0 <= i < len(items)][:max_guard]
        else:
            max_items: int = compact_spec.get("max_items", 20)
            items = items[:max_items]

        item_spec: dict = compact_spec.get("item_spec") or {}
        if item_spec:
            items = [_apply_item_spec(item, item_spec) for item in items]

        if actual_path and isinstance(data, dict):
            return {**data, actual_path: items}
        return items

    # ── Dict ────────────────────────────────────────────────────────────────
    return _apply_item_spec(data, compact_spec) if isinstance(data, dict) else data


def _apply_item_spec(item: Any, spec: dict) -> Any:
    if not isinstance(item, dict):
        return item
    drop_fields: set[str] = set(spec.get("drop_fields") or [])
    keep_fields: list[str] | None = spec.get("keep_fields")
    text_fields: dict[str, int] = spec.get("text_fields") or {}
    nested: dict[str, dict] = spec.get("nested") or {}
    max_items_fields: dict[str, int] = spec.get("max_items_fields") or {}

    result: dict = {}
    for k, v in item.items():
        if keep_fields and k not in keep_fields:
            continue
        if k in drop_fields:
            continue
        if k in text_fields and isinstance(v, str):
            mc = text_fields[k]
            v = v[:mc] + ("…" if len(v) > mc else "")
        if k in nested:
            v = apply_compact(v, nested[k])
        if k in max_items_fields and isinstance(v, list):
            v = v[: max_items_fields[k]]
        result[k] = v
    return result


# ---------------------------------------------------------------------------
# Evidence entry → compact view
# ---------------------------------------------------------------------------


def build_compact_view(
    entry: "EvidenceEntry",
    by_name: "dict[str, ToolSpec]",
) -> list[dict]:
    """Build a per-call compact payload for one evidence entry.

    Returns a list of dicts (one per tool call that produced this entry):
    ``{"tool_name": str, "summary": str, "compact": <compacted payload>}``
    """
    env = entry.envelope
    prov = env.provenance if isinstance(env.provenance, dict) else {}
    call_results: list[dict] = prov.get("tool_call_results") or []
    compact_keys: list[dict] = env.compact_keys or []

    ck_by_idx: dict[int, dict] = {
        ck["call_index"]: ck
        for ck in compact_keys
        if isinstance(ck, dict) and isinstance(ck.get("call_index"), int)
    }

    out: list[dict] = []
    for i, cr in enumerate(call_results):
        tool_name: str = cr.get("name") or entry.tool_name
        if tool_name == 'expand':
            continue  # skip expand calls (compaction not relevant, and may be noisy)
        
        full_str: str = cr.get("full") or ""
        ck: dict = ck_by_idx.get(i, {})
        call_summary: str = ck.get("summary") or cr.get("summary") or ""
        key_indices: list[int] | None = (
            [x for x in ck["key_indices"] if isinstance(x, int)]
            if isinstance(ck.get("key_indices"), list) else None
        )
        key_quotes: list[str] | None = (
            [str(q) for q in ck["key_quotes"]]
            if isinstance(ck.get("key_quotes"), list) else None
        )

        from utils.tools import ToolSpec as _TS  # local import to avoid circular
        spec = by_name.get(tool_name)
        compact_spec: dict = spec.compact_spec if spec is not None else {}

        try:
            data = json.loads(full_str) if full_str else None
        except Exception:
            data = full_str or None

        compacted = (
            apply_compact(data, compact_spec, key_indices, key_quotes)
            if data is not None and compact_spec
            else data
        )

        out.append({
            "tool_name":  tool_name,
            "summary":    call_summary,
            "compact":    compacted,
        })

    return out


__all__ = ["apply_compact", "build_compact_view"]
