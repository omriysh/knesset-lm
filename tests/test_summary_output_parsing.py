"""
tests/test_summary_output_parsing.py

Tests for summarization.output_parsing (two-pass summary text -> dicts, quote
verification) and for the result-merging logic of scripts/summarize_knesset_batches.py.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from summarization.output_parsing import parse_topics, parse_opinions, verify_quotes
import summarize_knesset_batches as batch


# ── parse_topics ──────────────────────────────────────────────────────────────

def test_parse_topics_bullets_and_numbering():
    assert parse_topics("- נושא א\n* נושא ב\n1. נושא ג\n2) נושא ד\n\n") == ["נושא א", "נושא ב", "נושא ג", "נושא ד"]


def test_parse_topics_sentinel_variants():
    assert parse_topics("לא פרוטוקול") == []
    assert parse_topics('  "לא פרוטוקול".\n') == []
    assert parse_topics("לֹא פרוטוקול") == []


def test_parse_topics_empty_is_none():
    assert parse_topics("") is None
    assert parse_topics("  \n ") is None


def test_parse_topics_line_without_bullet_still_counts():
    assert parse_topics("נושא בלי מקף") == ["נושא בלי מקף"]


# ── parse_opinions ────────────────────────────────────────────────────────────

def test_parse_opinions_three_fields():
    text = '- היו"ר דוד ביטן || תומך בחוק || "אני תומך בחוק הזה."\n- ח"כ יעל (העבודה) || מתנגדת || החוק מסוכן'
    assert parse_opinions(text) == [
        {"speaker": 'היו"ר דוד ביטן', "opinion": "תומך בחוק", "quote": "אני תומך בחוק הזה."},
        {"speaker": 'ח"כ יעל (העבודה)', "opinion": "מתנגדת", "quote": "החוק מסוכן"},
    ]


def test_parse_opinions_skips_malformed_lines_but_keeps_good_ones():
    text = "- שורה בלי מפריד\n- דובר || עמדה || ציטוט\n-  || עמדה בלי דובר || ציטוט"
    assert parse_opinions(text) == [{"speaker": "דובר", "opinion": "עמדה", "quote": "ציטוט"}]


def test_parse_opinions_only_malformed_is_empty_list_not_none():
    assert parse_opinions("- שורה בלי מפריד") == []


def test_parse_opinions_sentinel_and_empty():
    assert parse_opinions("לא פרוטוקול") == []
    assert parse_opinions("") is None


def test_parse_opinions_extra_separators_go_into_quote():
    assert parse_opinions("- א || ב || ג || ד")[0]["quote"] == "ג || ד"


# ── verify_quotes ─────────────────────────────────────────────────────────────

TRANSCRIPT = 'ח"כ יעל: החוק מסוכן, לדעתי הוא פוגע בזכויות.\nהיו"ר: תודה רבה.'


def test_verify_quotes_ignores_punctuation_and_niqqud():
    ops = [{"quote": '"החוק מסוכן, לדעתי הוא פוגע בזכויות"'}, {"quote": "הַחוק מסוכן"}]
    verify_quotes(ops, TRANSCRIPT)
    assert [o["quote_verified"] for o in ops] == [True, True]


def test_verify_quotes_rejects_paraphrase_splice_and_empty():
    ops = [{"quote": "החוק מצוין"}, {"quote": "החוק מסוכן [...] תודה רבה"}, {"quote": ""}]
    verify_quotes(ops, TRANSCRIPT)
    assert [o["quote_verified"] for o in ops] == [False, False, False]


# ── batch script result merging ───────────────────────────────────────────────

def _response(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _entry(tmp_path: Path, meeting_id: str) -> dict:
    proto = tmp_path / f"{meeting_id}.json"
    proto.write_text(json.dumps({"speeches": [
        {"speaker": 'ח"כ יעל', "text_he": "החוק מסוכן, לדעתי הוא פוגע בזכויות."},
        {"speaker": 'היו"ר', "text_he": "תודה רבה."},
    ]}, ensure_ascii=False), encoding="utf-8")
    return batch._new_entry(proto, tmp_path / "out" / f"{meeting_id}.json", "ועדה", "2025-01-01", int(meeting_id))


def _state(entries: list[dict]) -> dict:
    return {"knesset_num": 25, "queue": entries, "active_jobs": [],
            "stats": {"summarized": 0, "not_protocol": 0, "failed": 0}}


def test_meeting_written_only_after_both_passes(tmp_path):
    entry = _entry(tmp_path, "1")
    state = _state([entry])
    summ = Path(entry["summ"])

    batch._process_results([{"key": "1:topics", "response": _response("- נושא")}], ["1:topics"], state)
    assert not summ.exists() and state["queue"] == [entry] and entry["results"]["topics"] == ["נושא"]

    batch._process_results(
        [{"key": "1:opinions", "response": _response('- ח"כ יעל || מתנגדת לחוק || החוק מסוכן, לדעתי הוא פוגע בזכויות')}],
        ["1:opinions"], state)
    assert state["queue"] == [] and state["stats"]["summarized"] == 1
    written = json.loads(summ.read_text(encoding="utf-8"))
    assert written == {"is_protocol": True, "topics": ["נושא"], "opinions": [
        {"speaker": 'ח"כ יעל', "opinion": "מתנגדת לחוק", "quote": "החוק מסוכן, לדעתי הוא פוגע בזכויות", "quote_verified": True}]}


def test_not_protocol_from_topics_pass_finishes_immediately(tmp_path):
    entry = _entry(tmp_path, "2")
    state = _state([entry])
    batch._process_results([{"key": "2:topics", "response": _response("לא פרוטוקול")}], ["2:topics"], state)
    assert state["queue"] == [] and state["stats"]["not_protocol"] == 1
    assert json.loads(Path(entry["summ"]).read_text(encoding="utf-8")) == {"is_protocol": False, "topics": [], "opinions": []}


def test_unparsable_and_missing_results_count_attempts_then_drop(tmp_path):
    entry = _entry(tmp_path, "3")
    state = _state([entry])
    for _ in range(batch.MAX_PASS_ATTEMPTS - 1):
        batch._process_results([{"key": "3:opinions", "response": _response("")}], ["3:topics", "3:opinions"], state)
    assert entry["attempts"] == {"topics": batch.MAX_PASS_ATTEMPTS - 1, "opinions": batch.MAX_PASS_ATTEMPTS - 1}
    assert state["queue"] == [entry]

    batch._process_results([], ["3:topics"], state)
    assert state["queue"] == [] and state["stats"]["failed"] == 1
    assert not Path(entry["summ"]).exists()


def test_pending_passes_only_builds_missing_requests(tmp_path):
    entry = _entry(tmp_path, "4")
    entry["results"]["topics"] = ["נושא"]
    items = batch._build_requests_for_entries([entry], desc="t")
    assert [key for _, key in items] == ["4:opinions"]
    assert items[0][0]["systemInstruction"]["parts"][0]["text"] == batch.SYSTEM_PROMPT_OPINIONS
    assert items[0][0]["generationConfig"] == {"maxOutputTokens": batch.MAX_TOKENS, "temperature": 0.3,
                                               "thinkingConfig": {"thinkingLevel": "low"}}
