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

def _response(text: str, finish_reason: str = "STOP") -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish_reason}]}


def _write_structured_transcript(tmp_path: Path, meeting_id: str) -> Path:
    proto = tmp_path / "raw_transcriptions" / "25" / "ועדה" / f"01_01_2025_{meeting_id}.json"
    proto.parent.mkdir(parents=True, exist_ok=True)
    proto.write_text(json.dumps({"committee": "ועדה", "date": "2025-01-01", "speeches": [
        {"speaker": 'ח"כ יעל', "text_he": "החוק מסוכן, לדעתי הוא פוגע בזכויות."},
        {"speaker": 'היו"ר', "text_he": "תודה רבה."},
    ]}, ensure_ascii=False), encoding="utf-8")
    return proto


def _entry(tmp_path: Path, meeting_id: str) -> dict:
    proto = _write_structured_transcript(tmp_path, meeting_id)
    return batch._new_entry(proto, "ועדה", "2025-01-01", meeting_id, batch._load_transcript({"proto": str(proto)}))


def _opinions_key(entry: dict, position: int = 0) -> str:
    return batch._make_key(entry, batch._opinions_task(entry["opinion_spans"][position]))


def _state(entries: list[dict]) -> dict:
    return batch._new_state(25, entries)


def test_summary_goes_next_to_the_transcript_under_summaries(tmp_path):
    entry = _entry(tmp_path, "9")
    assert Path(entry["summ"]) == tmp_path / "summaries" / "25" / "ועדה" / "01_01_2025_9.json"


def test_meeting_written_only_after_both_passes(tmp_path):
    entry = _entry(tmp_path, "1")
    state = _state([entry])
    summ = Path(entry["summ"])

    batch._process_results([{"key": "1|topics", "response": _response("- נושא")}], ["1|topics"], state)
    assert not summ.exists() and state["queue"] == [entry] and entry["results"]["topics"] == ["נושא"]

    batch._process_results(
        [{"key": _opinions_key(entry), "response": _response('- ח"כ יעל || מתנגדת לחוק || החוק מסוכן, לדעתי הוא פוגע בזכויות')}],
        [_opinions_key(entry)], state)
    assert state["queue"] == [] and state["stats"]["summarized"] == 1
    written = json.loads(summ.read_text(encoding="utf-8"))
    assert written == {"is_protocol": True, "topics": ["נושא"], "opinions": [
        {"speaker": 'ח"כ יעל', "opinion": "מתנגדת לחוק", "quote": "החוק מסוכן, לדעתי הוא פוגע בזכויות", "quote_verified": True}]}


def test_not_protocol_from_topics_pass_finishes_immediately(tmp_path):
    entry = _entry(tmp_path, "2")
    state = _state([entry])
    batch._process_results([{"key": "2|topics", "response": _response("לא פרוטוקול")}], ["2|topics"], state)
    assert state["queue"] == [] and state["stats"]["not_protocol"] == 1
    assert json.loads(Path(entry["summ"]).read_text(encoding="utf-8")) == {"is_protocol": False, "topics": [], "opinions": []}


def test_unparsable_and_missing_results_count_attempts_then_drop(tmp_path):
    entry = _entry(tmp_path, "3")
    state = _state([entry])
    opinions_key = _opinions_key(entry)
    for _ in range(batch.MAX_PASS_ATTEMPTS - 1):
        batch._process_results([{"key": opinions_key, "response": _response("")}], ["3|topics", opinions_key], state)
    assert entry["attempts"] == {"topics": batch.MAX_PASS_ATTEMPTS - 1,
                                 batch._split_key(opinions_key)[1]: batch.MAX_PASS_ATTEMPTS - 1}
    assert state["queue"] == [entry]

    batch._process_results([], ["3|topics"], state)
    assert state["queue"] == [] and state["stats"]["failed"] == 1
    assert not Path(entry["summ"]).exists()


def test_pending_passes_only_builds_missing_requests(tmp_path):
    entry = _entry(tmp_path, "4")
    entry["results"]["topics"] = ["נושא"]
    items = batch._build_requests_for_entries([entry], desc="t")
    assert [key for _, key in items] == [_opinions_key(entry)]
    assert items[0][0]["systemInstruction"]["parts"][0]["text"] == batch.SYSTEM_PROMPT_OPINIONS
    assert items[0][0]["generationConfig"] == {"maxOutputTokens": batch.MAX_TOKENS, "temperature": 0.3,
                                               "thinkingConfig": {"thinkingLevel": "low"}}


# ── long transcripts: chunked opinions pass, truncated answers ────────────────

def _long_full_text_entry(tmp_path: Path, meeting_id: str, n_turns: int, turn_chars: int) -> dict:
    turns = []
    for number in range(n_turns):
        speaker = 'היו"ר אכרם חסון' if number % 2 == 0 else "מירב בן ארי (יש עתיד)"
        turns.append(f"{speaker}:\n\nדברים בנושא {number}. " + "א" * turn_chars + "\n\n")
    proto = tmp_path / "raw_transcriptions" / "25" / "מליאת_הכנסת" / f"08_07_2026_{meeting_id}.json"
    proto.parent.mkdir(parents=True, exist_ok=True)
    proto.write_text(json.dumps({"committee": "מליאת הכנסת", "date": "2026-07-08", "meeting_id": meeting_id,
                                 "full_text": "".join(turns)}, ensure_ascii=False), encoding="utf-8")
    return batch._new_entry(proto, "מליאת הכנסת", "2026-07-08", meeting_id, batch._load_transcript({"proto": str(proto)}))


def test_long_transcript_gets_chunked_opinions_but_one_topics_request(tmp_path, monkeypatch):
    monkeypatch.setattr(batch.config, "SUMMARY_OPINIONS_CHUNK_THRESHOLD_CHARS", 30_000)
    monkeypatch.setattr(batch.config, "SUMMARY_OPINIONS_CHUNK_TARGET_CHARS", 20_000)
    entry = _long_full_text_entry(tmp_path, "p77", n_turns=40, turn_chars=1_500)
    transcript = batch._load_transcript(entry)
    spans = entry["opinion_spans"]
    assert len(spans) == 4 and spans[0][0] == 0 and spans[-1][1] == len(transcript)
    assert all(previous[1] == following[0] for previous, following in zip(spans, spans[1:]))
    assert all(transcript[start:].startswith(('היו"ר אכרם חסון:', "מירב בן ארי")) for start, _ in spans[1:])

    items = batch._build_requests_for_entries([entry], desc="t")
    keys = [key for _, key in items]
    assert keys[0] == "p77|topics" and len(keys) == 5
    topics_text = items[0][0]["contents"][0]["parts"][0]["text"]
    assert topics_text.startswith("גוף: מליאת הכנסת\n") and transcript in topics_text
    assert "חלק 2 מתוך 4 של הפרוטוקול" in items[2][0]["contents"][0]["parts"][0]["text"]


def test_chunked_opinions_are_merged_in_transcript_order(tmp_path, monkeypatch):
    monkeypatch.setattr(batch.config, "SUMMARY_OPINIONS_CHUNK_THRESHOLD_CHARS", 30_000)
    monkeypatch.setattr(batch.config, "SUMMARY_OPINIONS_CHUNK_TARGET_CHARS", 20_000)
    entry = _long_full_text_entry(tmp_path, "p78", n_turns=40, turn_chars=1_500)
    state = _state([entry])
    keys = [_opinions_key(entry, position) for position in range(len(entry["opinion_spans"]))]
    results = [{"key": key, "response": _response(f"- מירב בן ארי || עמדה {position} || דברים בנושא")}
               for position, key in reversed(list(enumerate(keys)))]
    results.append({"key": "p78|topics", "response": _response("- נושא")})
    batch._process_results(results, keys + ["p78|topics"], state)
    written = json.loads(Path(entry["summ"]).read_text(encoding="utf-8"))
    assert [o["opinion"] for o in written["opinions"]] == [f"עמדה {position}" for position in range(len(keys))]


def test_truncated_opinions_answer_is_split_and_rerequested(tmp_path, monkeypatch):
    monkeypatch.setattr(batch, "MIN_RESPLIT_CHARS", 1_000)
    entry = _long_full_text_entry(tmp_path, "p79", n_turns=10, turn_chars=500)
    state = _state([entry])
    truncated_key = _opinions_key(entry)
    batch._process_results([{"key": truncated_key, "response": _response("- מירב || עמדה || ציט", "MAX_TOKENS")}],
                           [truncated_key], state)
    assert len(entry["opinion_spans"]) == 2 and entry["attempts"] == {}
    assert state["stats"]["truncated_resplit"] == 1 and state["queue"] == [entry]
    assert [key for _, key in batch._build_requests_for_entries([entry], desc="t")] == \
        ["p79|topics", _opinions_key(entry, 0), _opinions_key(entry, 1)]


def test_truncated_answer_too_short_to_split_keeps_complete_lines(tmp_path):
    entry = _entry(tmp_path, "5")
    state = _state([entry])
    batch._process_results([{"key": "5|topics", "response": _response("- נושא א\n- נושא ב\n- נוש", "MAX_TOKENS")}],
                           ["5|topics"], state)
    assert entry["results"]["topics"] == ["נושא א", "נושא ב"]
    assert state["stats"]["truncated_accepted"] == 1


# ── scan ──────────────────────────────────────────────────────────────────────

def test_scan_queues_only_transcripts_without_a_summary_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(batch.config, "DATA_DIR", tmp_path)
    _write_structured_transcript(tmp_path, "11")
    _write_structured_transcript(tmp_path, "12")
    summary_in_renamed_folder = tmp_path / "summaries" / "25" / "ועדה_בשם_קודם" / "01_01_2025_12.json"
    summary_in_renamed_folder.parent.mkdir(parents=True)
    summary_in_renamed_folder.write_text('{"is_protocol": true, "topics": [], "opinions": []}', encoding="utf-8")

    queue, counts = batch._scan_transcripts(25, force_summarize=False, skip_patterns=[])
    assert [entry["meeting_id"] for entry in queue] == ["11"]
    assert counts["already_done"] == 1


def test_rescan_keeps_answers_of_a_leftover_queue_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(batch.config, "DATA_DIR", tmp_path)
    leftover = _entry(tmp_path, "13")
    leftover["results"]["topics"] = ["נושא"]
    _write_structured_transcript(tmp_path, "14")
    queue, _ = batch._scan_transcripts(25, force_summarize=False, skip_patterns=[], previous_queue=[leftover])
    assert [entry["meeting_id"] for entry in queue] == ["13", "14"]
    assert queue[0] is leftover


# ── transcripts longer than the model context: chunked topics pass ────────────

def _over_context_entry(tmp_path: Path, monkeypatch, meeting_id: str) -> dict:
    monkeypatch.setattr(batch, "MAX_TRANSCRIPT_CHARS", 25_000)
    return _long_full_text_entry(tmp_path, meeting_id, n_turns=40, turn_chars=1_500)


def _topics_key(entry: dict, position: int) -> str:
    return batch._make_key(entry, batch._topics_task(entry["topic_spans"][position]))


def test_transcript_longer_than_the_context_gets_chunked_topics(tmp_path, monkeypatch):
    entry = _over_context_entry(tmp_path, monkeypatch, "p90")
    transcript = batch._load_transcript(entry)
    spans = entry["topic_spans"]
    assert len(spans) == 3 and spans[0][0] == 0 and spans[-1][1] == len(transcript)
    assert all(end - start <= batch.MAX_TRANSCRIPT_CHARS for start, end in spans)
    assert all(previous[1] == following[0] for previous, following in zip(spans, spans[1:]))
    assert all(transcript[start:].startswith(('היו"ר אכרם חסון:', "מירב בן ארי")) for start, _ in spans[1:])

    items = batch._build_requests_for_entries([entry], desc="t")
    topic_items = [(request, key) for request, key in items if batch._task_kind(batch._split_key(key)[1]) == "topics"]
    assert [key for _, key in topic_items] == [_topics_key(entry, position) for position in range(3)]
    for position, (request, _) in enumerate(topic_items):
        text = request["contents"][0]["parts"][0]["text"]
        assert f"חלק {position + 1} מתוך 3 של הפרוטוקול" in text
        assert text.endswith(transcript[spans[position][0]:spans[position][1]])
        assert request["systemInstruction"]["parts"][0]["text"] == batch.SYSTEM_PROMPT_TOPICS


def test_short_transcript_keeps_one_whole_topics_request(tmp_path):
    entry = _entry(tmp_path, "6")
    assert len(entry["topic_spans"]) == 1
    assert [key for _, key in batch._build_requests_for_entries([entry], desc="t")][0] == "6|topics"


def test_chunked_topics_are_merged_in_order_without_repeats(tmp_path, monkeypatch):
    entry = _over_context_entry(tmp_path, monkeypatch, "p91")
    state = _state([entry])
    topic_keys = [_topics_key(entry, position) for position in range(3)]
    opinion_keys = [_opinions_key(entry, position) for position in range(len(entry["opinion_spans"]))]
    answers = ["- תקציב\n- חינוך", "לא פרוטוקול", "- חינוך\n- ביטחון"]
    results = [{"key": key, "response": _response(answer)} for key, answer in reversed(list(zip(topic_keys, answers)))]
    results += [{"key": key, "response": _response("- מירב בן ארי || עמדה || דברים בנושא")} for key in opinion_keys]
    batch._process_results(results, topic_keys + opinion_keys, state)
    written = json.loads(Path(entry["summ"]).read_text(encoding="utf-8"))
    assert written["is_protocol"] is True
    assert written["topics"] == ["תקציב", "חינוך", "ביטחון"]


def test_chunked_topics_all_not_protocol_marks_the_meeting(tmp_path, monkeypatch):
    entry = _over_context_entry(tmp_path, monkeypatch, "p92")
    state = _state([entry])
    topic_keys = [_topics_key(entry, position) for position in range(3)]
    opinion_keys = [_opinions_key(entry, position) for position in range(len(entry["opinion_spans"]))]
    results = [{"key": key, "response": _response("לא פרוטוקול")} for key in topic_keys + opinion_keys]
    batch._process_results(results, topic_keys + opinion_keys, state)
    assert state["queue"] == [] and state["stats"]["not_protocol"] == 1
    assert json.loads(Path(entry["summ"]).read_text(encoding="utf-8"))["is_protocol"] is False


def test_scan_queues_a_transcript_longer_than_the_context(tmp_path, monkeypatch):
    monkeypatch.setattr(batch.config, "DATA_DIR", tmp_path)
    entry = _over_context_entry(tmp_path, monkeypatch, "p93")
    queue, counts = batch._scan_transcripts(25, force_summarize=False, skip_patterns=[])
    assert [queued["meeting_id"] for queued in queue] == ["p93"]
    assert queue[0]["topic_spans"] == entry["topic_spans"]


def test_leftover_state_entry_without_topic_spans_still_has_one_topics_task(tmp_path):
    entry = _entry(tmp_path, "7")
    del entry["topic_spans"]
    assert batch._all_tasks(entry)[0] == "topics"
