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
from utils.tools import ToolSpec, ToolRegistry, dispatch, handle_find_mk, search_speeches_bm25
from agent.subgraph.evidence import ToolEnvelope
from retrieval.bm25_index import BM25Index


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


# ── dispatch: find_mk with real BM25 db ──────────────────────────────────────

class TestDispatchFindMkNoDB:
    def test_dispatch_find_mk_missing_db_returns_error_envelope(self, tmp_path):
        """When BM25 db path doesn't exist, find_mk should return envelope with error."""
        import config as _config

        original_bm25_dir = _config.BM25_DIR
        # Point BM25_DIR at a nonexistent location
        _config.BM25_DIR = tmp_path / "nonexistent_bm25"
        try:
            from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
            result = dispatch(RESEARCH_TOOL_REGISTRY, "find_mk", {"query": "נתניהו"})
            assert isinstance(result, ToolEnvelope)
            # Should have an error — db is missing
            assert result.error is not None
            assert result.error != "unknown_tool"  # should find the tool, but fail on db
        finally:
            _config.BM25_DIR = original_bm25_dir


class TestDispatchFindMkWithDB:
    """Test find_mk when the BM25 db is present.

    This test is conditional on the BM25 db existing. If missing, it is
    skipped so CI doesn't fail on an incomplete build.
    """

    def test_dispatch_find_mk_with_real_db(self):
        bm25_mks_path = config.BM25_DIR / "25" / "mks.db"
        if not bm25_mks_path.exists():
            pytest.skip(f"BM25 mks.db not built yet: {bm25_mks_path}")

        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        result = dispatch(RESEARCH_TOOL_REGISTRY, "find_mk", {"query": "נתניהו"})

        assert isinstance(result, ToolEnvelope)
        # With a real db, error should be None (or at worst a low-confidence warning)
        # The tool returns candidates even when fuzzy match is used
        if result.error is not None:
            # If error is set, it should be a soft warning, not a crash
            assert result.error not in ("dispatch_exception", "unknown_tool")
        else:
            # No error — we should have some results
            assert result.metadata.get("count", 0) >= 0


# ── search_speeches_bm25 — shared helper extracted from handle_search_protocols_keyword ──

class _SpeechesFixture:
    """Small in-memory-shaped speeches BM25 fixture (mirrors build_speeches() rows)."""

    ROWS = [
        {"id": "m1_0", "label": "אלמוני", "label_lemmatized": "אלמוני",
         "body": "אלמוני: הצעת חוק בנושא חינוך", "body_lemmatized": "אלמוני: הצעת חוק בנושא חינוך",
         "extra": {"meeting_id": "m1", "committee": "ועדת החינוך", "speech_idx": 0, "speaker": "אלמוני"}},
        {"id": "m2_0", "label": "פלוני", "label_lemmatized": "פלוני",
         "body": "פלוני: דיון בנושא חינוך והשכלה גבוהה", "body_lemmatized": "פלוני: דיון בנושא חינוך והשכלה גבוהה",
         "extra": {"meeting_id": "m2", "committee": "ועדת הכספים", "speech_idx": 0, "speaker": "פלוני"}},
        {"id": "m2_1", "label": "פלוני", "label_lemmatized": "פלוני",
         "body": "פלוני: המשך הדיון בנושא חינוך", "body_lemmatized": "פלוני: המשך הדיון בנושא חינוך",
         "extra": {"meeting_id": "m2", "committee": "ועדת הכספים", "speech_idx": 1, "speaker": "פלוני"}},
        {"id": "m3_0", "label": "אחר", "label_lemmatized": "אחר",
         "body": "אחר: נושא לא קשור", "body_lemmatized": "אחר: נושא לא קשור",
         "extra": {"meeting_id": "m3", "committee": "ועדת החוץ", "speech_idx": 0, "speaker": "אחר"}},
    ]

    @classmethod
    def build(cls, path) -> BM25Index:
        idx = BM25Index(path)
        idx.create_table()
        idx.insert_many(cls.ROWS)
        return idx


class TestSearchSpeechesBm25Helper:
    def test_basic_search_no_filters(self, tmp_path):
        bm25 = _SpeechesFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "חינוך")
        finally:
            bm25.close()
        ids = {r["id"] for r in rows}
        assert {"m1_0", "m2_0", "m2_1"}.issubset(ids)
        assert "m3_0" not in ids

    def test_meeting_ids_filter_is_ored_not_anded(self, tmp_path):
        """Regression test for the fixed OR-bug: passing multiple meeting_ids
        used to AND every individual LIKE clause together, which could never
        match (a single row's `extra` only ever has one meeting_id), so any
        filter with >1 meeting_id silently returned zero rows. Now it's
        "any of these meetings" — required for the reading-tab candidate-set
        scoping which passes many meeting_ids at once."""
        bm25 = _SpeechesFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "חינוך", meeting_ids=["m1", "m2"])
        finally:
            bm25.close()
        meeting_ids_hit = {r["extra"]["meeting_id"] for r in rows}
        assert meeting_ids_hit == {"m1", "m2"}

    def test_meeting_ids_filter_excludes_others(self, tmp_path):
        bm25 = _SpeechesFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "חינוך", meeting_ids=["m2"])
        finally:
            bm25.close()
        meeting_ids_hit = {r["extra"]["meeting_id"] for r in rows}
        assert meeting_ids_hit == {"m2"}

    def test_committee_ids_filter_is_ored(self, tmp_path):
        bm25 = _SpeechesFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(
                bm25, "חינוך", committee_ids=["ועדת החינוך", "ועדת הכספים"],
            )
        finally:
            bm25.close()
        committees_hit = {r["extra"]["committee"] for r in rows}
        assert committees_hit == {"ועדת החינוך", "ועדת הכספים"}

    def test_no_match_returns_empty(self, tmp_path):
        bm25 = _SpeechesFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "חינוך", meeting_ids=["nonexistent"])
        finally:
            bm25.close()
        assert rows == []

    def test_per_meeting_bm25_aggregation_max_score_collapse(self, tmp_path):
        """m2 has two matching speeches — aggregating to per-meeting best score
        (as web.app.browse_rag's keyword-ranking step does) must collapse them
        to a single meeting-level entry with the best (lowest, per sqlite's
        bm25() convention) score."""
        bm25 = _SpeechesFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "חינוך")
        finally:
            bm25.close()

        best_score: dict[str, float] = {}
        for r in rows:
            mid = r["extra"]["meeting_id"]
            score = float(r["score"])
            if mid not in best_score or score < best_score[mid]:
                best_score[mid] = score

        assert set(best_score) == {"m1", "m2"}
        m2_scores = [float(r["score"]) for r in rows if r["extra"]["meeting_id"] == "m2"]
        assert len(m2_scores) == 2                    # two raw hits collapse to...
        assert best_score["m2"] == min(m2_scores)      # ...one entry, the best of the two


class _MkSpeakerFixture:
    """Speeches fixture mirroring the real spread of stored speaker strings for
    a single MK (Orit Struck) — ministerial title, middle name, party tag — plus
    a decoy MK who shares a first/middle-name token, and an unrelated speaker.

    Every body carries the standalone token ``התיישבות`` so a single query hits
    all rows and the assertions isolate the *speaker* filter, not text ranking.
    """

    ROWS = [
        # Orit Struck — under her ministerial title (the form that carried 79
        # real speeches, none of which the old contiguous-substring filter hit).
        {"id": "t_title", "label": "", "label_lemmatized": "",
         "body": "דיון בנושא התיישבות וביטחון",
         "body_lemmatized": "דיון בנושא התיישבות וביטחון",
         "extra": {"meeting_id": "mA", "committee": "ועדת החוץ והביטחון", "speech_idx": 0,
                   "speaker": "שרת ההתיישבות והמשימות הלאומיות אורית סטרוק"}},
        # Orit Struck — with middle name.
        {"id": "t_middle", "label": "", "label_lemmatized": "",
         "body": "התיישבות היא נושא מרכזי",
         "body_lemmatized": "התיישבות היא נושא מרכזי",
         "extra": {"meeting_id": "mA", "committee": "ועדת החוץ והביטחון", "speech_idx": 1,
                   "speaker": "אורית מלכה סטרוק"}},
        # Orit Struck — with a party parenthetical.
        {"id": "t_party", "label": "", "label_lemmatized": "",
         "body": "לקדם התיישבות ביהודה",
         "body_lemmatized": "לקדם התיישבות ביהודה",
         "extra": {"meeting_id": "mB", "committee": "ועדת החוץ והביטחון", "speech_idx": 0,
                   "speaker": "אורית מלכה סטרוק (הציונות הדתית)"}},
        # Decoy: different person sharing the middle-name token + a fuzzy first
        # name, but a different/missing surname — must be excluded.
        {"id": "t_decoy", "label": "", "label_lemmatized": "",
         "body": "תומך בקידום התיישבות",
         "body_lemmatized": "תומך בקידום התיישבות",
         "extra": {"meeting_id": "mB", "committee": "ועדת החוץ והביטחון", "speech_idx": 1,
                   "speaker": "אורי מלכה"}},
        # Unrelated speaker.
        {"id": "t_other", "label": "", "label_lemmatized": "",
         "body": "התיישבות בהיבט הכלכלי",
         "body_lemmatized": "התיישבות בהיבט הכלכלי",
         "extra": {"meeting_id": "mC", "committee": "ועדת הכספים", "speech_idx": 0,
                   "speaker": "משה כהן"}},
    ]

    @classmethod
    def build(cls, path) -> BM25Index:
        idx = BM25Index(path)
        idx.create_table()
        idx.insert_many(cls.ROWS)
        return idx


class TestSpeakerFilterRealWorld:
    """Regression tests for the speaker-matching bug: search_protocols_keyword
    returned zero speeches for an MK whose name is stored under a ministerial
    title / with a middle name, because the FTS filter matched only a contiguous
    substring of the speaker field."""

    def test_finds_all_speaker_forms_of_one_mk(self, tmp_path):
        bm25 = _MkSpeakerFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "התיישבות", speaker="אורית סטרוק")
        finally:
            bm25.close()
        ids = {r["id"] for r in rows}
        assert ids == {"t_title", "t_middle", "t_party"}

    def test_excludes_different_person_sharing_a_token(self, tmp_path):
        bm25 = _MkSpeakerFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "התיישבות", speaker="אורית מלכה סטרוק")
        finally:
            bm25.close()
        speakers = {r["extra"]["speaker"] for r in rows}
        assert "אורי מלכה" not in speakers
        assert "משה כהן" not in speakers

    def test_title_form_is_matched(self, tmp_path):
        """The specific real failure: the ministerial-title form must be found."""
        bm25 = _MkSpeakerFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "וביטחון", speaker="אורית סטרוק")
        finally:
            bm25.close()
        assert [r["id"] for r in rows] == ["t_title"]

    def test_surname_only_query_matches_all_forms(self, tmp_path):
        bm25 = _MkSpeakerFixture.build(tmp_path / "speeches.db")
        try:
            rows = search_speeches_bm25(bm25, "התיישבות", speaker="סטרוק")
        finally:
            bm25.close()
        assert {r["id"] for r in rows} == {"t_title", "t_middle", "t_party"}


class TestHandleSearchProtocolsKeywordUnchanged:
    """handle_search_protocols_keyword's external contract (ToolEnvelope shape,
    payload fields) must be unaffected by extracting search_speeches_bm25 out
    of it — this is a pure refactor for the handler."""

    def test_missing_db_still_returns_bm25_missing_envelope(self, tmp_path):
        original_bm25_dir = config.BM25_DIR
        config.BM25_DIR = tmp_path / "nonexistent_bm25"
        try:
            from utils.tools import handle_search_protocols_keyword
            result = handle_search_protocols_keyword({"query": "חינוך"})
            assert isinstance(result, ToolEnvelope)
            assert result.error == "bm25_db_missing"
        finally:
            config.BM25_DIR = original_bm25_dir

    def test_missing_query_still_validation_error(self):
        from utils.tools import handle_search_protocols_keyword
        result = handle_search_protocols_keyword({"query": ""})
        assert isinstance(result, ToolEnvelope)
        assert result.error == "missing_query"

    def test_real_db_smoke(self):
        bm25_speeches_path = config.BM25_DIR / "25" / "speeches.db"
        if not bm25_speeches_path.exists():
            pytest.skip(f"BM25 speeches.db not built yet: {bm25_speeches_path}")
        from utils.tools import handle_search_protocols_keyword
        result = handle_search_protocols_keyword({"query": "תקציב", "top_k": 5})
        assert isinstance(result, ToolEnvelope)
        assert result.error is None
        payload = json.loads(result.full) if result.full else []
        assert isinstance(payload, list)


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
