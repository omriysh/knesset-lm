"""
test_tool_dispatch.py

Tests for utils.tools.dispatch and related tool infrastructure.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
import config
from utils.tools import ToolSpec, ToolRegistry, dispatch, handle_find_mk, search_speeches
from agent.subgraph.evidence import ToolEnvelope
from retrieval import knesset_db_store as store


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_ok_handler(payload="test"):
    def handler(args: dict) -> ToolEnvelope:
        return ToolEnvelope(
            summary="ok",
            full=str(payload),
            metadata={"kind": "fetch", "source": "test", "count": 1},
            provenance={"tool": "test"},
        )
    return handler


def _make_registry(*entries: ToolSpec) -> ToolRegistry:
    return list(entries)


def _make_spec(name: str, handler=None) -> ToolSpec:
    return ToolSpec(
        name=name,
        schema={"type": "object", "properties": {}},
        handler=handler or _make_ok_handler(),
        task_kinds=["discover"],
        cost_hint="cheap",
    )


# ── dispatch: unknown tool ────────────────────────────────────────────────────

class TestDispatchUnknownTool:
    def test_unknown_tool_returns_envelope(self):
        registry = _make_registry(_make_spec("find_mk"))
        result = dispatch(registry, "no_such_tool", {})
        assert isinstance(result, ToolEnvelope)

    def test_unknown_tool_sets_error(self):
        registry = _make_registry(_make_spec("find_mk"))
        result = dispatch(registry, "no_such_tool", {})
        assert result.error == "unknown_tool"

    def test_unknown_tool_does_not_raise(self):
        registry = _make_registry(_make_spec("find_mk"))
        # Should not raise, just return envelope with error
        envelope = dispatch(registry, "completely_missing", {})
        assert envelope is not None

    def test_unknown_tool_empty_registry(self):
        result = dispatch([], "find_mk", {})
        assert isinstance(result, ToolEnvelope)
        assert result.error == "unknown_tool"

    def test_unknown_tool_has_metadata(self):
        registry = _make_registry()
        result = dispatch(registry, "ghost_tool", {})
        assert "kind" in result.metadata
        assert result.metadata["kind"] == "error"


# ── dispatch: handler exceptions ─────────────────────────────────────────────

class TestDispatchHandlerException:
    def test_handler_exception_returns_envelope(self):
        def bad_handler(args: dict) -> ToolEnvelope:
            raise RuntimeError("Simulated tool failure")

        registry = _make_registry(_make_spec("bad_tool", handler=bad_handler))
        result = dispatch(registry, "bad_tool", {})
        assert isinstance(result, ToolEnvelope)
        assert result.error == "dispatch_exception"

    def test_handler_exception_does_not_raise(self):
        def exploding_handler(args: dict):
            raise ValueError("Boom!")

        registry = _make_registry(_make_spec("boom", handler=exploding_handler))
        # Should not propagate
        envelope = dispatch(registry, "boom", {})
        assert envelope is not None

    def test_handler_exception_metadata_has_exception(self):
        def bad(args):
            raise TypeError("type mismatch")

        registry = _make_registry(_make_spec("bad", handler=bad))
        result = dispatch(registry, "bad", {})
        # The metadata should contain exception info
        assert "exception" in result.metadata or result.error == "dispatch_exception"


# ── dispatch: successful call ─────────────────────────────────────────────────

class TestDispatchSuccess:
    def test_known_tool_calls_handler(self):
        called_with = {}

        def my_handler(args: dict) -> ToolEnvelope:
            called_with.update(args)
            return ToolEnvelope(
                summary="done",
                full="result",
                metadata={"kind": "fetch", "source": "test", "count": 1},
                provenance={},
            )

        registry = _make_registry(_make_spec("my_tool", handler=my_handler))
        result = dispatch(registry, "my_tool", {"key": "value"})
        assert called_with == {"key": "value"}
        assert result.error is None

    def test_dispatch_passes_args_to_handler(self):
        received_args = {}

        def capture_handler(args: dict) -> ToolEnvelope:
            received_args.update(args)
            return ToolEnvelope(
                summary="", full="", metadata={"kind": "test", "source": "test", "count": 0},
                provenance={},
            )

        registry = _make_registry(_make_spec("capture", handler=capture_handler))
        dispatch(registry, "capture", {"query": "נתניהו", "knesset_num": 25})
        assert received_args["query"] == "נתניהו"

    def test_dispatch_with_none_args_uses_empty_dict(self):
        """Passing None as args should not crash the handler."""
        received_args = {}

        def capture_handler(args: dict) -> ToolEnvelope:
            received_args["got"] = args
            return ToolEnvelope(summary="", full="",
                                metadata={"kind": "test", "source": "test", "count": 0},
                                provenance={})

        registry = _make_registry(_make_spec("capture", handler=capture_handler))
        dispatch(registry, "capture", None)
        assert isinstance(received_args.get("got"), dict)


# ── dispatch: find_mk against knesset.db ─────────────────────────────────────

class TestDispatchFindMkNoDB:
    def test_dispatch_find_mk_missing_db_returns_error_envelope(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "nonexistent.db")
        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        result = dispatch(RESEARCH_TOOL_REGISTRY, "find_mk", {"query": "נתניהו"})
        assert isinstance(result, ToolEnvelope)
        assert result.error == "knesset_db_missing"


class TestDispatchFindMkWithDB:
    """Conditional on a built knesset.db — skipped otherwise."""

    def test_dispatch_find_mk_with_real_db(self):
        if not store.exists():
            pytest.skip(f"knesset.db not built yet: {store.db_path()}")
        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        result = dispatch(RESEARCH_TOOL_REGISTRY, "find_mk", {"query": "נתניהו"})
        assert isinstance(result, ToolEnvelope)
        if result.error is not None:
            assert result.error not in ("dispatch_exception", "unknown_tool")


# ── search_speeches — shared helper behind handle_search_protocols_keyword ──

def _speeches_db(tmp_path, monkeypatch, rows):
    """rows: [{meeting_id, committee, idx, speaker, text}] -> connection to a temp knesset.db."""
    path = tmp_path / "knesset.db"
    monkeypatch.setattr(config, "KNESSET_DB", path)
    conn = store.connect(path)
    meetings = {r["meeting_id"]: r["committee"] for r in rows}
    store.insert_meetings(conn, [{"meeting_id": m, "knesset_num": 25, "committee": c} for m, c in meetings.items()])
    store.insert_speeches(conn, [{"meeting_id": r["meeting_id"], "knesset_num": 25, "idx": r["idx"],
                                  "speaker": r["speaker"], "mk_id": None, "text": r["text"]} for r in rows])
    store.rebuild_fts(conn, "speeches")
    return conn


SPEECH_ROWS = [
    {"meeting_id": "m1", "committee": "ועדת החינוך", "idx": 0, "speaker": "אלמוני", "text": "הצעת חוק בנושא חינוך"},
    {"meeting_id": "m2", "committee": "ועדת הכספים", "idx": 0, "speaker": "פלוני", "text": "דיון בנושא חינוך והשכלה גבוהה"},
    {"meeting_id": "m2", "committee": "ועדת הכספים", "idx": 1, "speaker": "פלוני", "text": "המשך הדיון בנושא חינוך"},
    {"meeting_id": "m3", "committee": "ועדת החוץ", "idx": 0, "speaker": "אחר", "text": "נושא לא קשור"},
]


def _ids(rows):
    return {f"{r['meeting_id']}_{r['speech_idx']}" for r in rows}


class TestSearchSpeechesHelper:
    def test_basic_search_no_filters(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, SPEECH_ROWS)
        assert _ids(search_speeches(conn, "חינוך")) == {"m1_0", "m2_0", "m2_1"}

    def test_meeting_ids_filter_is_ored(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, SPEECH_ROWS)
        assert {r["meeting_id"] for r in search_speeches(conn, "חינוך", meeting_ids=["m1", "m2"])} == {"m1", "m2"}
        assert {r["meeting_id"] for r in search_speeches(conn, "חינוך", meeting_ids=["m2"])} == {"m2"}

    def test_committees_filter_is_ored(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, SPEECH_ROWS)
        rows = search_speeches(conn, "חינוך", committees=["ועדת החינוך", "ועדת_הכספים"])
        assert {r["committee"] for r in rows} == {"ועדת החינוך", "ועדת הכספים"}

    def test_no_match_returns_empty(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, SPEECH_ROWS)
        assert search_speeches(conn, "חינוך", meeting_ids=["nonexistent"]) == []

    def test_per_meeting_best_score_collapse(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, SPEECH_ROWS)
        rows = search_speeches(conn, "חינוך")
        best: dict[str, float] = {}
        for r in rows:
            best[r["meeting_id"]] = min(best.get(r["meeting_id"], float("inf")), float(r["score"]))
        assert set(best) == {"m1", "m2"}
        m2_scores = [float(r["score"]) for r in rows if r["meeting_id"] == "m2"]
        assert len(m2_scores) == 2 and best["m2"] == min(m2_scores)


MK_SPEAKER_ROWS = [
    {"meeting_id": "mA", "committee": "ועדת החוץ והביטחון", "idx": 0,
     "speaker": "שרת ההתיישבות והמשימות הלאומיות אורית סטרוק", "text": "דיון בנושא התיישבות וביטחון"},
    {"meeting_id": "mA", "committee": "ועדת החוץ והביטחון", "idx": 1,
     "speaker": "אורית מלכה סטרוק", "text": "התיישבות היא נושא מרכזי"},
    {"meeting_id": "mB", "committee": "ועדת החוץ והביטחון", "idx": 0,
     "speaker": "אורית מלכה סטרוק (הציונות הדתית)", "text": "לקדם התיישבות ביהודה"},
    {"meeting_id": "mB", "committee": "ועדת החוץ והביטחון", "idx": 1,
     "speaker": "אורי מלכה", "text": "תומך בקידום התיישבות"},
    {"meeting_id": "mC", "committee": "ועדת הכספים", "idx": 0,
     "speaker": "משה כהן", "text": "התיישבות בהיבט הכלכלי"},
]


class TestSpeakerFilterRealWorld:
    """An MK stored under a ministerial title / middle name / party tag must be
    found by the speaker filter; a decoy sharing one token must not."""

    def test_finds_all_speaker_forms_of_one_mk(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, MK_SPEAKER_ROWS)
        assert _ids(search_speeches(conn, "התיישבות", speaker="אורית סטרוק")) == {"mA_0", "mA_1", "mB_0"}

    def test_excludes_different_person_sharing_a_token(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, MK_SPEAKER_ROWS)
        speakers = {r["speaker"] for r in search_speeches(conn, "התיישבות", speaker="אורית מלכה סטרוק")}
        assert "אורי מלכה" not in speakers and "משה כהן" not in speakers

    def test_title_form_is_matched(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, MK_SPEAKER_ROWS)
        assert _ids(search_speeches(conn, "וביטחון", speaker="אורית סטרוק")) == {"mA_0"}

    def test_surname_only_query_matches_all_forms(self, tmp_path, monkeypatch):
        conn = _speeches_db(tmp_path, monkeypatch, MK_SPEAKER_ROWS)
        assert _ids(search_speeches(conn, "התיישבות", speaker="סטרוק")) == {"mA_0", "mA_1", "mB_0"}


class TestHandleSearchProtocolsKeyword:
    def test_missing_db_returns_missing_envelope(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "KNESSET_DB", tmp_path / "nonexistent.db")
        from utils.tools import handle_search_protocols_keyword
        result = handle_search_protocols_keyword({"query": "חינוך"})
        assert result.error == "knesset_db_missing"

    def test_missing_query_is_validation_error(self):
        from utils.tools import handle_search_protocols_keyword
        assert handle_search_protocols_keyword({"query": ""}).error == "missing_query"

    def test_payload_shape(self, tmp_path, monkeypatch):
        _speeches_db(tmp_path, monkeypatch, SPEECH_ROWS).close()
        from utils.tools import handle_search_protocols_keyword
        result = handle_search_protocols_keyword({"query": "חינוך", "top_k": 5, "committee_ids": ["ועדת הכספים"]})
        assert result.error is None
        payload = json.loads(result.full)
        assert {p["meeting_id"] for p in payload} == {"m2"}
        assert set(payload[0]) >= {"speech_id", "text", "meeting_id", "committee", "speaker", "speech_idx"}


# ── ToolSpec ──────────────────────────────────────────────────────────────────

class TestToolSpec:
    def test_tool_spec_to_dict_excludes_handler(self):
        spec = _make_spec("find_mk")
        d = spec.to_dict()
        assert "name" in d
        assert "schema" in d
        assert "task_kinds" in d
        assert "cost_hint" in d
        assert "handler" not in d
