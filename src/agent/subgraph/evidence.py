"""Generic evidence store for subgraph agents.

Per design §4.2 / §4.3, every subgraph that collects tool outputs writes them
to an ``EvidenceStore`` as ``EvidenceEntry`` objects wrapping a
``ToolEnvelope``. The store keeps a small, prompt-friendly summary in memory
and spills full payloads to disk once cumulative in-memory size exceeds the
configured cap. A hard count cap raises ``EvidenceCapExceeded``.

This module is generic: ``payload``-shaped fields are typed as ``dict`` /
``str`` / free objects, and no research-specific kind values are encoded.
``ResearchAgent``'s tools (Phase 5) populate envelope fields with their own
shapes; this module never inspects them.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Iterator

import config as _config


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EvidenceCapExceeded(Exception):
    """Raised when the entry-count cap (``EVIDENCE_MAX_ENTRIES``) is hit.

    The subgraph is expected to abort the current run on this error; v1
    treats it as a hard fail rather than a graceful truncation.
    """


# ---------------------------------------------------------------------------
# ToolEnvelope
# ---------------------------------------------------------------------------


@dataclass
class ToolEnvelope:
    """Unified return shape from every tool invoked inside a subgraph.

    Field set follows the Phase 2 task spec exactly. The design's open-ended
    ``full: object`` and ``provenance: list[dict]`` are tightened to ``str``
    and ``dict`` here for v1; tools that need richer shapes can JSON-encode
    into ``full`` or nest under ``provenance``.
    """

    summary: str
    full: str
    metadata: dict
    provenance: dict
    truncated: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "full": self.full,
            "metadata": dict(self.metadata) if self.metadata is not None else {},
            "provenance": dict(self.provenance) if self.provenance is not None else {},
            "truncated": bool(self.truncated),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ToolEnvelope":
        return cls(
            summary=data.get("summary", ""),
            full=data.get("full", ""),
            metadata=dict(data.get("metadata") or {}),
            provenance=dict(data.get("provenance") or {}),
            truncated=bool(data.get("truncated", False)),
            error=data.get("error"),
        )


# ---------------------------------------------------------------------------
# EvidenceEntry
# ---------------------------------------------------------------------------


@dataclass
class EvidenceEntry:
    """One persisted row in the evidence store.

    Field set follows the Phase 2 task spec (envelope-wrapped form), not the
    design's flatter draft. The envelope owns ``summary`` / ``full`` /
    ``metadata`` / ``provenance``; this dataclass adds identity, attribution,
    and timestamp.
    """

    id: str
    tool_name: str
    step_id: str
    envelope: ToolEnvelope
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "step_id": self.step_id,
            "envelope": self.envelope.to_dict(),
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EvidenceEntry":
        env = data.get("envelope") or {}
        return cls(
            id=data.get("id", ""),
            tool_name=data.get("tool_name", ""),
            step_id=data.get("step_id", ""),
            envelope=ToolEnvelope.from_dict(env if isinstance(env, dict) else {}),
            timestamp=float(data.get("timestamp") or time.time()),
        )


# ---------------------------------------------------------------------------
# EvidenceStore
# ---------------------------------------------------------------------------


def _envelope_full_bytes(envelope: ToolEnvelope) -> int:
    """Approximate in-memory byte cost of an envelope's heavy fields.

    Used to decide when to spill ``full`` to disk. We measure the encoded
    UTF-8 length of ``full`` plus the JSON dump of ``metadata`` /
    ``provenance``; this overshoots true Python overhead but is monotone.
    """
    try:
        full_bytes = len((envelope.full or "").encode("utf-8"))
    except Exception:
        full_bytes = 0
    try:
        meta_bytes = len(
            json.dumps(envelope.metadata or {}, ensure_ascii=False).encode("utf-8")
        )
    except Exception:
        meta_bytes = 0
    try:
        prov_bytes = len(
            json.dumps(envelope.provenance or {}, ensure_ascii=False).encode("utf-8")
        )
    except Exception:
        prov_bytes = 0
    return full_bytes + meta_bytes + prov_bytes


class EvidenceStore:
    """In-memory evidence store with disk spill-over.

    Behaviour per Phase 2 task spec:
      - ``add(entry)`` returns the entry id; assigns one if missing.
      - ``get(id)`` returns the entry or None; transparently reloads spilled
        ``full`` payloads from disk.
      - ``iter()`` yields entries in insertion order.
      - When cumulative in-memory ``full`` bytes would exceed
        ``config.EVIDENCE_MAX_BYTES_IN_MEM`` (resolved via fallback below),
        the entry's ``full`` is written to a temp file and replaced in
        memory with an empty string. Summary / metadata / provenance stay
        resident so prompt views remain fast.
      - When entry count would exceed ``config.EVIDENCE_MAX_ENTRIES``,
        ``add`` raises ``EvidenceCapExceeded``.

    The store is single-process and not thread-safe; subgraph agents are
    serial executors.
    """

    def __init__(self, spill_dir: str | None = None):
        self._entries: dict[str, EvidenceEntry] = {}
        self._order: list[str] = []
        self._spill_paths: dict[str, str] = {}
        self._in_mem_bytes: int = 0

        # Resolve caps via config, with a sane fallback for the in-mem
        # threshold (config.py exposes EVIDENCE_MAX_BYTES_TOTAL; the task
        # spec calls the same threshold EVIDENCE_MAX_BYTES_IN_MEM, so we
        # accept either name and fall back to TOTAL).
        self._max_entries: int = int(getattr(_config, "EVIDENCE_MAX_ENTRIES", 200))
        self._max_in_mem: int = int(
            getattr(
                _config,
                "EVIDENCE_MAX_BYTES_IN_MEM",
                getattr(_config, "EVIDENCE_MAX_BYTES_TOTAL", 8 * 1024 * 1024),
            )
        )

        if spill_dir is None:
            spill_dir = os.path.join(tempfile.gettempdir(), "knesset_lm_evidence")
        os.makedirs(spill_dir, exist_ok=True)
        self._spill_dir: str = spill_dir

    # -- public API --------------------------------------------------------

    def add(self, entry: EvidenceEntry) -> str:
        """Insert ``entry``; spill its ``full`` payload to disk if needed."""
        if len(self._entries) >= self._max_entries:
            raise EvidenceCapExceeded(
                f"Evidence count cap reached: {self._max_entries} entries"
            )

        if not entry.id:
            entry.id = self._mint_id()
        if entry.id in self._entries:
            # Re-adding an existing id is a programming error — fail loud.
            raise ValueError(f"EvidenceEntry id collision: {entry.id!r}")

        env_bytes = _envelope_full_bytes(entry.envelope)

        # If adding this entry's full payload would push us over the in-mem
        # budget, spill it before storing.
        if (
            self._max_in_mem > 0
            and env_bytes > 0
            and self._in_mem_bytes + env_bytes > self._max_in_mem
        ):
            self._spill_full(entry)
        else:
            self._in_mem_bytes += env_bytes

        self._entries[entry.id] = entry
        self._order.append(entry.id)
        return entry.id

    def get(self, id: str) -> EvidenceEntry | None:
        """Return the entry, transparently rehydrating spilled ``full``."""
        entry = self._entries.get(id)
        if entry is None:
            return None
        if id in self._spill_paths and not entry.envelope.full:
            try:
                with open(self._spill_paths[id], "r", encoding="utf-8") as fh:
                    entry.envelope.full = fh.read()
            except OSError:
                # Treat missing/unreadable spill as a non-fatal data loss;
                # the caller will see an empty `full`. Summary still works.
                pass
        return entry

    def iter(self) -> Iterator[EvidenceEntry]:
        """Yield entries in insertion order (without rehydrating spills)."""
        for eid in list(self._order):
            entry = self._entries.get(eid)
            if entry is not None:
                yield entry

    def summary_view(self) -> list[dict]:
        """Prompt-friendly summary of all entries — no full payloads.

        Includes tool arguments (from provenance tool_calls) so the LLM can
        see what was queried, but excludes full results and provenance details.
        """
        out: list[dict] = []
        for entry in self.iter():
            env = entry.envelope
            prov = env.provenance if isinstance(env.provenance, dict) else {}
            out.append({
                "id":         entry.id,
                "tool_name":  entry.tool_name,
                "step_id":    entry.step_id,
                "summary":    env.summary or "",
                "tool_calls": prov.get("tool_calls") or [],
                "metadata":   env.metadata or {},
                "truncated":  bool(env.truncated),
                "error":      env.error,
            })
        return out

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, id: object) -> bool:
        return isinstance(id, str) and id in self._entries

    # -- helpers -----------------------------------------------------------

    def _mint_id(self) -> str:
        return f"ev_{uuid.uuid4().hex[:12]}"

    def _spill_full(self, entry: EvidenceEntry) -> None:
        """Write the entry's ``full`` payload to disk and clear it in memory."""
        payload = entry.envelope.full or ""
        path = os.path.join(self._spill_dir, f"{entry.id}.full.txt")
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(payload)
        except OSError:
            # If we can't spill, fall back to in-memory storage and accept
            # the budget overrun rather than dropping data.
            self._in_mem_bytes += _envelope_full_bytes(entry.envelope)
            return
        self._spill_paths[entry.id] = path
        entry.envelope.full = ""
        # metadata + provenance stay in memory; recompute their cost only.
        meta_only = ToolEnvelope(
            summary="",
            full="",
            metadata=entry.envelope.metadata,
            provenance=entry.envelope.provenance,
        )
        self._in_mem_bytes += _envelope_full_bytes(meta_only)


__all__ = [
    "EvidenceCapExceeded",
    "EvidenceEntry",
    "EvidenceStore",
    "ToolEnvelope",
]


# Suppress unused-import lint for asdict — kept for symmetry with potential
# future to_dict shortcuts; safe to remove if a linter flags it.
_ = asdict
