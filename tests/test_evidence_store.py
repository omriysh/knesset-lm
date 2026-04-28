"""
test_evidence_store.py

Tests for EvidenceStore, EvidenceEntry, ToolEnvelope, and EvidenceCapExceeded.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from agent.subgraph.evidence import (
    EvidenceCapExceeded,
    EvidenceEntry,
    EvidenceStore,
    ToolEnvelope,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_envelope(
    summary="Found 3 MKs",
    full='[{"mk_id": "1", "name": "Test MK"}]',
    metadata=None,
    provenance=None,
    truncated=False,
    error=None,
) -> ToolEnvelope:
    return ToolEnvelope(
        summary=summary,
        full=full,
        metadata=metadata or {"kind": "search", "source": "bm25", "count": 3},
        provenance=provenance or {"query": "test", "knesset_num": 25},
        truncated=truncated,
        error=error,
    )


def _make_entry(id="ev_001", tool_name="find_mk", step_id="s1", **kwargs) -> EvidenceEntry:
    return EvidenceEntry(
        id=id,
        tool_name=tool_name,
        step_id=step_id,
        envelope=_make_envelope(**kwargs),
    )


# ── ToolEnvelope roundtrip ────────────────────────────────────────────────────

class TestToolEnvelopeRoundtrip:
    def test_roundtrip_basic(self):
        env = _make_envelope()
        restored = ToolEnvelope.from_dict(env.to_dict())
        assert restored.summary == env.summary
        assert restored.full == env.full
        assert restored.metadata == env.metadata
        assert restored.provenance == env.provenance
        assert restored.truncated == env.truncated
        assert restored.error == env.error

    def test_roundtrip_with_error(self):
        env = _make_envelope(error="bm25_db_missing", summary="", full="")
        restored = ToolEnvelope.from_dict(env.to_dict())
        assert restored.error == "bm25_db_missing"

    def test_roundtrip_truncated(self):
        env = _make_envelope(truncated=True)
        restored = ToolEnvelope.from_dict(env.to_dict())
        assert restored.truncated is True

    def test_to_dict_structure(self):
        env = _make_envelope()
        d = env.to_dict()
        assert "summary" in d
        assert "full" in d
        assert "metadata" in d
        assert "provenance" in d
        assert "truncated" in d
        assert "error" in d

    def test_from_dict_none_error(self):
        d = {
            "summary": "test",
            "full": "data",
            "metadata": {},
            "provenance": {},
        }
        env = ToolEnvelope.from_dict(d)
        assert env.error is None
        assert env.truncated is False

    def test_roundtrip_hebrew_content(self):
        env = _make_envelope(
            summary="מצאנו 3 חברי כנסת",
            full='[{"שם": "נתניהו"}]',
        )
        restored = ToolEnvelope.from_dict(env.to_dict())
        assert restored.summary == "מצאנו 3 חברי כנסת"


# ── EvidenceStore: add and get ────────────────────────────────────────────────

class TestAddAndGet:
    def test_add_and_get(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        entry = _make_entry(id="ev_001")
        eid = store.add(entry)
        assert eid == "ev_001"

        retrieved = store.get("ev_001")
        assert retrieved is not None
        assert retrieved.id == "ev_001"
        assert retrieved.tool_name == "find_mk"
        assert retrieved.step_id == "s1"
        assert retrieved.envelope.summary == "Found 3 MKs"

    def test_add_assigns_id_when_empty(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        entry = EvidenceEntry(id="", tool_name="find_mk", step_id="s1",
                              envelope=_make_envelope())
        eid = store.add(entry)
        assert eid
        assert eid.startswith("ev_")

    def test_get_missing_returns_none(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        assert store.get("nonexistent") is None

    def test_get_all_fields_preserved(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        env = ToolEnvelope(
            summary="Test summary",
            full='{"data": "value"}',
            metadata={"kind": "fetch", "source": "odata", "count": 1},
            provenance={"meeting_id": "42"},
            truncated=False,
            error=None,
        )
        entry = EvidenceEntry(id="ev_test", tool_name="get_meeting_summary",
                              step_id="s2", envelope=env)
        store.add(entry)
        retrieved = store.get("ev_test")
        assert retrieved.envelope.summary == "Test summary"
        assert retrieved.envelope.metadata["kind"] == "fetch"
        assert retrieved.step_id == "s2"

    def test_duplicate_id_raises(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        store.add(_make_entry(id="ev_dup"))
        with pytest.raises(ValueError, match="ev_dup"):
            store.add(_make_entry(id="ev_dup"))


# ── EvidenceStore: iter ───────────────────────────────────────────────────────

class TestIter:
    def test_iter_returns_all(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        store.add(_make_entry(id="ev_001", tool_name="find_mk"))
        store.add(_make_entry(id="ev_002", tool_name="search_topics"))
        store.add(_make_entry(id="ev_003", tool_name="get_meeting_summary"))
        entries = list(store.iter())
        assert len(entries) == 3
        ids = [e.id for e in entries]
        assert "ev_001" in ids
        assert "ev_002" in ids
        assert "ev_003" in ids

    def test_iter_insertion_order(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        for i in range(5):
            store.add(_make_entry(id=f"ev_00{i}"))
        ids = [e.id for e in store.iter()]
        assert ids == [f"ev_00{i}" for i in range(5)]

    def test_iter_empty_store(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        assert list(store.iter()) == []


# ── EvidenceStore: cap exceeded ───────────────────────────────────────────────

class TestCapExceeded:
    def test_cap_exceeded_raises(self, tmp_path):
        # Use a tiny cap to avoid creating 200 entries
        import config as _config
        original_cap = _config.EVIDENCE_MAX_ENTRIES
        _config.EVIDENCE_MAX_ENTRIES = 3
        try:
            store = EvidenceStore(spill_dir=str(tmp_path))
            for i in range(3):
                store.add(_make_entry(id=f"ev_{i:03d}"))
            with pytest.raises(EvidenceCapExceeded):
                store.add(_make_entry(id="ev_overflow"))
        finally:
            _config.EVIDENCE_MAX_ENTRIES = original_cap

    def test_cap_exceeded_exception_type(self, tmp_path):
        import config as _config
        original_cap = _config.EVIDENCE_MAX_ENTRIES
        _config.EVIDENCE_MAX_ENTRIES = 1
        try:
            store = EvidenceStore(spill_dir=str(tmp_path))
            store.add(_make_entry(id="ev_001"))
            try:
                store.add(_make_entry(id="ev_002"))
                assert False, "Expected EvidenceCapExceeded"
            except EvidenceCapExceeded as exc:
                assert "cap" in str(exc).lower() or "1" in str(exc)
        finally:
            _config.EVIDENCE_MAX_ENTRIES = original_cap


# ── EvidenceStore: len and contains ──────────────────────────────────────────

class TestLenContains:
    def test_len_empty(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        assert len(store) == 0

    def test_len_after_add(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        store.add(_make_entry(id="ev_001"))
        store.add(_make_entry(id="ev_002"))
        assert len(store) == 2

    def test_contains_true(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        store.add(_make_entry(id="ev_001"))
        assert "ev_001" in store

    def test_contains_false(self, tmp_path):
        store = EvidenceStore(spill_dir=str(tmp_path))
        assert "ev_missing" not in store
