"""
tests/test_search_opinions.py

Tests for utils.tools.handle_search_opinions — hybrid BM25 + embedding search
over summary opinion bullets, filtered to a single MK via the mk_id metadata
written by scripts/backfill_bullet_mk_ids.py.

Follows the repo test conventions: tiny BM25Index fixture in tmp_path,
config.BM25_DIR monkeypatched, dense side patched at the module seam
(utils.tools._embed_opinion_ranking) so no chromadb/transformers import.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
import utils.tools as tools_mod
from utils.tools import handle_search_opinions
from agent.subgraph.evidence import ToolEnvelope
from retrieval.bm25_index import BM25Index


# ── Fixture: bullets BM25 db with mk_id-tagged extras ─────────────────────────

MK_A = "101"   # שמחה רוטמן — three tagged bullets, two about חינוך
MK_B = "102"   # גלעד קריב — one tagged bullet about חינוך

ROWS = [
    {"id": "ועדת החינוך__m1__3",
     "label": "", "label_lemmatized": "",
     "body": "שמחה רוטמן: תמך ברפורמה בנושא חינוך",
     "body_lemmatized": "שמחה רוטמן: תמך ברפורמה בנושא חינוך",
     "extra": {"meeting_id": "m1", "committee": "ועדת החינוך", "bullet_idx": 3,
               "mk_id": MK_A, "speaker": "שמחה רוטמן", "date": "2025-01-06"}},
    {"id": "ועדת החינוך__m2__5",
     "label": "", "label_lemmatized": "",
     "body": "שמחה רוטמן: התנגד לקיצוץ בתקציב מערכת חינוך",
     "body_lemmatized": "שמחה רוטמן: התנגד לקיצוץ בתקציב מערכת חינוך",
     "extra": {"meeting_id": "m2", "committee": "ועדת החינוך", "bullet_idx": 5,
               "mk_id": MK_A, "speaker": "שמחה רוטמן", "date": "2025-02-10"}},
    {"id": "ועדת הכספים__m3__1",
     "label": "", "label_lemmatized": "",
     "body": "שמחה רוטמן: הציג עמדה בנושא ביטחון ותקציבו",
     "body_lemmatized": "שמחה רוטמן: הציג עמדה בנושא ביטחון ותקציבו",
     "extra": {"meeting_id": "m3", "committee": "ועדת הכספים", "bullet_idx": 1,
               "mk_id": MK_A, "speaker": "שמחה רוטמן"}},
    {"id": "ועדת החינוך__m1__7",
     "label": "", "label_lemmatized": "",
     "body": "גלעד קריב: התנגד לרפורמה בנושא חינוך",
     "body_lemmatized": "גלעד קריב: התנגד לרפורמה בנושא חינוך",
     "extra": {"meeting_id": "m1", "committee": "ועדת החינוך", "bullet_idx": 7,
               "mk_id": MK_B, "speaker": "גלעד קריב"}},
    # Untagged topic bullet about the same topic — must never be returned.
    {"id": "ועדת החינוך__m1__0",
     "label": "", "label_lemmatized": "",
     "body": "דיון כללי בנושא חינוך והשכלה גבוהה",
     "body_lemmatized": "דיון כללי בנושא חינוך והשכלה גבוהה",
     "extra": {"meeting_id": "m1", "committee": "ועדת החינוך", "bullet_idx": 0}},
]


@pytest.fixture()
def bullets_db(tmp_path, monkeypatch):
    """Build bullets.db under a temp BM25_DIR and point config at it."""
    db_dir = tmp_path / "bm25" / "25"
    db_dir.mkdir(parents=True)
    idx = BM25Index(db_dir / "bullets.db")
    idx.create_table()
    idx.insert_many(ROWS)
    idx.close()
    monkeypatch.setattr(config, "BM25_DIR", tmp_path / "bm25")
    # Dense side off by default; individual tests patch it back on.
    monkeypatch.setattr(
        tools_mod, "_embed_opinion_ranking",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no embedder in tests")),
    )
    return db_dir / "bullets.db"


def _payload(env: ToolEnvelope) -> list[dict]:
    return json.loads(env.full)


# ── validation ────────────────────────────────────────────────────────────────

class TestValidation:
    def test_missing_query_is_error(self, bullets_db):
        env = handle_search_opinions({"mk_id": MK_A})
        assert env.error is not None

    def test_missing_mk_id_is_error(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך"})
        assert env.error is not None

    def test_never_raises_returns_envelope(self, bullets_db):
        env = handle_search_opinions({})
        assert isinstance(env, ToolEnvelope)

    def test_missing_db_returns_bm25_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "BM25_DIR", tmp_path / "empty")
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        assert env.error == "bm25_db_missing"


# ── mk filter ─────────────────────────────────────────────────────────────────

class TestMkFilter:
    def test_returns_only_bullets_of_requested_mk(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        assert env.error is None
        payload = _payload(env)
        assert payload, "expected hits for MK_A about חינוך"
        assert all(hit["mk_id"] == MK_A for hit in payload)

    def test_other_mk_gets_their_own_bullets(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_B})
        payload = _payload(env)
        assert len(payload) == 1
        assert payload[0]["meeting_id"] == "m1"

    def test_untagged_bullets_never_returned(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        ids = {hit["bullet_id"] for hit in _payload(env)}
        assert "ועדת החינוך__m1__0" not in ids

    def test_unknown_mk_returns_empty_not_error(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": "999"})
        assert env.error is None
        assert _payload(env) == []

    def test_topic_filter_applies_within_mk(self, bullets_db):
        # MK_A has a ביטחון bullet; a חינוך query must not return it.
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        bodies = [hit["text"] for hit in _payload(env)]
        assert all("ביטחון" not in b for b in bodies)


# ── payload shape ─────────────────────────────────────────────────────────────

class TestPayloadShape:
    def test_hit_fields(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        hit = _payload(env)[0]
        for field in ("bullet_id", "text", "speaker", "mk_id",
                      "meeting_id", "committee", "date"):
            assert field in hit, f"missing field {field}"

    def test_date_none_when_absent_in_extra(self, bullets_db):
        env = handle_search_opinions({"query": "ביטחון", "mk_id": MK_A})
        payload = _payload(env)
        assert len(payload) == 1
        assert payload[0]["date"] is None

    def test_metadata_kind_and_count(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        assert env.metadata["kind"] == "search"
        assert env.metadata["count"] == len(_payload(env))

    def test_provenance_carries_query_and_mk(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        assert env.provenance.get("mk_id") == MK_A


# ── hybrid fusion ─────────────────────────────────────────────────────────────

class TestHybridFusion:
    def test_embedding_unavailable_degrades_with_warning(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        assert env.error is None
        assert "embedding_unavailable" in env.metadata.get("warnings", [])

    def test_dense_ranking_fused_via_rrf(self, bullets_db, monkeypatch):
        # Dense side surfaces only the m2 bullet; RRF must lift it above the
        # BM25-only m1 bullet regardless of BM25's internal ordering.
        monkeypatch.setattr(
            tools_mod, "_embed_opinion_ranking",
            lambda **kwargs: ["ועדת החינוך__m2__5"],
        )
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        payload = _payload(env)
        assert payload[0]["bullet_id"] == "ועדת החינוך__m2__5"
        assert "warnings" not in env.metadata or \
            "embedding_unavailable" not in env.metadata["warnings"]

    def test_dense_only_ids_are_hydrated_from_bm25(self, bullets_db, monkeypatch):
        # Dense returns a valid id that BM25's MATCH may not surface for this
        # query; the handler must hydrate its row via fetch_by_ids.
        monkeypatch.setattr(
            tools_mod, "_embed_opinion_ranking",
            lambda **kwargs: ["ועדת הכספים__m3__1"],
        )
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        ids = {hit["bullet_id"] for hit in _payload(env)}
        assert "ועדת הכספים__m3__1" in ids
        texts = {h["bullet_id"]: h["text"] for h in _payload(env)}
        assert "ביטחון" in texts["ועדת הכספים__m3__1"]

    def test_dense_hit_for_wrong_mk_is_dropped(self, bullets_db, monkeypatch):
        # Even if the dense side leaks another MK's bullet id, the handler
        # must drop it — mk_id filter is a hard guarantee.
        monkeypatch.setattr(
            tools_mod, "_embed_opinion_ranking",
            lambda **kwargs: ["ועדת החינוך__m1__7"],
        )
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        assert all(hit["mk_id"] == MK_A for hit in _payload(env))


# ── top_k ─────────────────────────────────────────────────────────────────────

class TestTopK:
    def test_top_k_caps_results(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A, "top_k": 1})
        assert len(_payload(env)) == 1

    def test_default_top_k_from_config(self, bullets_db):
        env = handle_search_opinions({"query": "חינוך", "mk_id": MK_A})
        assert env.provenance.get("top_k") == config.SEARCH_OPINIONS_DEFAULT_TOP_K


# ── registry wiring ──────────────────────────────────────────────────────────

class TestRegistry:
    def test_search_opinions_registered(self):
        from agent.research_agent.tools import RESEARCH_TOOL_REGISTRY
        spec = next(
            (s for s in RESEARCH_TOOL_REGISTRY if s.name == "search_opinions"), None)
        assert spec is not None
        assert set(spec.schema["required"]) == {"query", "mk_id"}
        assert spec.handler is handle_search_opinions
