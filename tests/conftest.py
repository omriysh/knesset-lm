"""
conftest.py

Shared pytest fixtures and helpers.
"""

import json
import sys
import tempfile
from pathlib import Path

import pytest

# Bootstrap sys.path so tests can import from src/ without installation
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ── Machine JSON factories ────────────────────────────────────────────────────

def _minimal_machine(extra_nodes=None, extra_edges=None) -> dict:
    """Return the smallest valid v2 machine: begin → one LLM node."""
    nodes = [
        {"id": "begin_001", "type": "begin", "label": "Begin",
         "position": {"x": 0, "y": 0}, "data": {}},
        {"id": "llm_001",   "type": "llm_call", "label": "Router",
         "position": {"x": 200, "y": 0},
         "data": {"system_prompt": "You are helpful.", "stage": "router",
                  "temperature": 0.7, "max_tokens": 512}},
    ]
    edges = [
        {"id": "e_001", "source": "begin_001", "target": "llm_001",
         "type": "transition", "label": ""},
    ]
    nodes.extend(extra_nodes or [])
    edges.extend(extra_edges or [])
    return {"version": 2, "id": "test_machine", "name": "Test", "nodes": nodes, "edges": edges}


@pytest.fixture()
def minimal_machine_path(tmp_path):
    """Write a minimal machine JSON to a temp file and return its Path."""
    p = tmp_path / "machine.json"
    p.write_text(json.dumps(_minimal_machine()), encoding="utf-8")
    return p


@pytest.fixture()
def machine_with_tool_path(tmp_path):
    """Machine with one LLM node + one tool node."""
    nodes = [
        {"id": "tool_001", "type": "tool", "label": "get_profile",
         "position": {"x": 200, "y": 100},
         "data": {
             "function_name": "find_mk",
             "description": "Find MK",
             "parameters": {"type": "object", "properties": {
                 "name": {"type": "string"}
             }},
         }},
    ]
    edges = [
        {"id": "e_tool", "source": "llm_001", "target": "tool_001",
         "type": "tool_link", "label": ""},
    ]
    data = _minimal_machine(extra_nodes=nodes, extra_edges=edges)
    p = tmp_path / "machine_tool.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ── Sample knesset.db (real rows + a few synthetic edge-case rows) ────────────

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "protocols_sample.json").read_text(encoding="utf-8"))
ROLES = SAMPLE["roles"]
X_MK = SAMPLE["X"]
SYN_MEETING = "2299001"


def _real_text(table: str, meeting_role: str, idx: int, field: str) -> str:
    mid = ROLES[meeting_role]
    return next(r[field] for r in SAMPLE[table] if r["meeting_id"] == mid and r["idx"] == idx)


def synthetic_rows() -> dict:
    """Edge-case rows: a newest meeting with ktiv-variant words and very long texts, plus
    summary rows attached to the real is_protocol=0 meeting (M5)."""
    x = X_MK["mk_id"]
    c2 = SAMPLE["committees"]["C2"]
    long_opinion = "ביטחון " + " ".join([_real_text("opinions", "M2", 0, "opinion")] * 40)
    long_quote = " ".join([_real_text("opinions", "M2", 0, "quote")] * 20)
    long_speech = "ביטחון " + "\n".join([_real_text("speeches", "M2", 8, "text")] * 5)
    return {
        "meetings": [{"meeting_id": SYN_MEETING, "knesset_num": 25, "committee": c2, "date": "2023-06-15",
                      "format": "structured", "transcript_path": None, "summary_path": "syn.json",
                      "is_protocol": 1}],
        "topics": [
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "idx": 0, "text": "דיון בנושא ביטחון המדינה"},
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "idx": 1, "text": "תקציב ביטחון הפנים"},
            {"meeting_id": ROLES["M5"], "knesset_num": 25, "idx": 0, "text": "ביטחון בתי הספר"},
        ],
        "opinions": [
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "idx": 0, "speaker_label": "היו\"ר עודד פורר",
             "speaker_name": X_MK["full_name"], "mk_id": x, "party": X_MK["party"], "opinion": long_opinion,
             "quote": long_quote, "quote_verified": 1, "speech_idx": 2, "quote_offset": 0},
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "idx": 1, "speaker_label": "איל קופמן",
             "speaker_name": "איל קופמן", "mk_id": None, "party": None,
             "opinion": "חיזוק ביטחון הציבור מחייב תקציב", "quote": "ביטחון הציבור הוא ערך עליון",
             "quote_verified": 1, "speech_idx": 1, "quote_offset": 0},
            {"meeting_id": ROLES["M5"], "knesset_num": 25, "idx": 0, "speaker_label": "עודד פורר",
             "speaker_name": X_MK["full_name"], "mk_id": x, "party": X_MK["party"],
             "opinion": "ביטחון התלמידים חשוב", "quote": "ביטחון התלמידים", "quote_verified": 1,
             "speech_idx": 0, "quote_offset": 0},
        ],
        "attendance": [
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "name": "עודד פורר (ישראל ביתנו)", "mk_id": x,
             "party": X_MK["party"]},
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "name": "איל קופמן", "mk_id": None, "party": None},
            {"meeting_id": ROLES["M5"], "knesset_num": 25, "name": "עודד פורר", "mk_id": x, "party": X_MK["party"]},
        ],
        "speeches": [
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "idx": 0, "speaker": "היו\"ר עודד פורר", "mk_id": x,
             "text": "אנו פותחים את הדיון בנושא ביטחון המדינה."},
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "idx": 1, "speaker": "איל קופמן", "mk_id": None,
             "text": "ביטחון הציבור הוא ערך עליון."},
            {"meeting_id": SYN_MEETING, "knesset_num": 25, "idx": 2, "speaker": "היו\"ר עודד פורר", "mk_id": x,
             "text": long_speech},
        ],
    }


_COLUMNS = {
    "mks": ("mk_id", "knesset_num", "first_name", "last_name", "full_name", "party", "aliases"),
    "committees": ("committee_id", "knesset_num", "name", "is_current"),
    "meetings": ("meeting_id", "knesset_num", "committee", "date", "format", "transcript_path",
                 "summary_path", "is_protocol"),
    "attendance": ("meeting_id", "knesset_num", "name", "mk_id", "party"),
    "topics": ("meeting_id", "knesset_num", "idx", "text"),
    "opinions": ("meeting_id", "knesset_num", "idx", "speaker_label", "speaker_name", "mk_id", "party",
                 "opinion", "quote", "quote_verified", "speech_idx", "quote_offset"),
    "speeches": ("meeting_id", "knesset_num", "idx", "speaker", "mk_id", "text"),
}


def build_sample_db(path: Path, with_synthetic: bool = True) -> Path:
    """Write the sampled rows (+ synthetic rows) into a fresh knesset.db at path via raw SQL on
    the store schema, then rebuild the FTS indexes."""
    from retrieval import knesset_db_store as store

    tables = {
        "mks": SAMPLE["mks"], "committees": SAMPLE["committee_rows"], "meetings": SAMPLE["meetings"],
        "attendance": SAMPLE["attendance"], "topics": SAMPLE["topics"], "opinions": SAMPLE["opinions"],
        "speeches": SAMPLE["speeches"],
    }
    if with_synthetic:
        for table, rows in synthetic_rows().items():
            tables[table] = list(tables[table]) + rows
    conn = store.connect(path)
    try:
        for table, rows in tables.items():
            cols = _COLUMNS[table]
            conn.executemany(
                f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [tuple(r.get(c) for c in cols) for r in rows])
        for fts in ("topics_fts", "opinions_fts", "speeches_fts"):
            conn.execute(f"INSERT INTO {fts}({fts}) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def sample_db(tmp_path, monkeypatch):
    """Temp knesset.db with the real sampled rows; config.KNESSET_DB points at it."""
    import config
    path = build_sample_db(tmp_path / "knesset.db")
    monkeypatch.setattr(config, "KNESSET_DB", path)
    return path
