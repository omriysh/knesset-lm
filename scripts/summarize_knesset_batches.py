"""
summarize_knesset_batches.py

Two-pass summarization for a Knesset using Google's Gemini Batch API.

Every transcript on disk under Data/raw_transcriptions/<knesset>/ (committee meetings and plenum
sessions, downloaded by scripts/process_knesset.py) that has no summary yet gets a topics request
and an opinions request (prompts in summarization/prompts.py). A transcript longer than
config.SUMMARY_OPINIONS_CHUNK_THRESHOLD_CHARS gets one opinions request per chunk, cut at speaker
turns; an answer cut off by the output cap (finishReason MAX_TOKENS) has its chunk split in two
and re-requested. A transcript longer than the model context (MAX_TRANSCRIPT_CHARS) also gets one
topics request per chunk; the chunks' topics are merged in order without repeats. The model answers in plain text; Python parses it
(summarization/output_parsing.py), verifies every quote against the transcript, and writes
Data/summaries/<knesset>/<committee>/<stem>.json:

    {"is_protocol": bool,
     "topics":      [str, ...],
     "opinions":    [{"speaker": str, "opinion": str, "quote": str, "quote_verified": bool}, ...]}

The file is written only once every request of a meeting succeeded, so an existing file always
means a complete summary; a meeting whose summary exists (under any committee folder) is skipped.

State is saved to Data/summary_batches/batch_state_k<N>.json after every batch so runs can be
interrupted and resumed: in-flight jobs are reconnected on resume (no re-submission / double
billing) and a half-done queue is finished first. Every run then rescans the disk for transcripts
without a summary (--rescan merges the rescan into the leftover queue before processing it).

Requirements
------------
    pip install google-genai
    export GEMINI_API_KEY=...

Usage
-----
    cd knesset-lm
    # dry run: scan, build the JSONL files, print token/cost estimate, submit nothing
    python scripts/summarize_knesset_batches.py --knesset 25 --dry-run
    # pilot: 10 random meetings, separate state file
    python scripts/summarize_knesset_batches.py --knesset 25 --sample 10 --state-file pilot_k25.json
    # full run (scripts/process_knesset.py runs this step for you)
    python scripts/summarize_knesset_batches.py --knesset 25
    python scripts/summarize_knesset_batches.py --knesset 25 --force-summarize
    python scripts/summarize_knesset_batches.py --knesset 25 --skip "ועדת הכנסת"

Exits 1 when a meeting failed every attempt.
"""

import argparse
import json
import os
import random
import sys
import tempfile
from pathlib import Path

from tqdm import tqdm

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google import genai

import config
from config import CHARS_PER_TOK, MAX_TOKENS
from utils.meeting import load_meeting, build_transcript_text
from utils.protocol_download import json_files_by_meeting, safe_dirname, transcripts_by_meeting
from summarization.prompts import SYSTEM_PROMPT_TOPICS, SYSTEM_PROMPT_OPINIONS
from summarization.output_parsing import parse_topics, parse_opinions, verify_quotes
from summarization.summary_io import summary_path_for_transcript
from summarization.gemini_batch import (estimate_request_tokens, extract_text, load_state, resume_active_jobs,
                                        run_pool, save_state, split_batches, write_jsonl)
from summarization.transcript_chunks import opinion_chunk_spans, split_span, topic_chunk_spans

# ── Batch constants ───────────────────────────────────────────────────────────

GEMINI_MODEL                   = "gemini-3.8-flash"
GEMINI_CTX_TOKENS              = 800_000                            # input tokens per request (1M ctx minus output headroom)
MAX_TRANSCRIPT_CHARS           = GEMINI_CTX_TOKENS * CHARS_PER_TOK
THINKING_LEVEL                 = "low"                              # gemini 3.x: low | medium | high | none (omit)
TEMPERATURE                    = 0.3
MAX_BATCH_INPUT_TOKENS         = 10_000_000                         # per-job cap
MAX_REQUESTS_PER_BATCH         = 500                                # per-job request count cap
MAX_CONCURRENT_JOBS            = 100                                # api limit on active batch jobs
ENQUEUE_CAP_TOKENS             = 380_000_000                        # cross-job enqueue cap (tier 2 = 400M for gemini-3.8-flash)
POLL_INTERVAL_S                = 60
MAX_PASS_ATTEMPTS              = 3       # per-request retry budget before giving up on the meeting
MIN_RESPLIT_CHARS              = 20_000  # a truncated opinions chunk shorter than this is accepted as is
STATE_VERSION                  = 2

TOPICS_TASK   = "topics"
OPINIONS_TASK = "opinions"
PROMPTS = {
    TOPICS_TASK:   SYSTEM_PROMPT_TOPICS,
    OPINIONS_TASK: SYSTEM_PROMPT_OPINIONS,
}
TRUNCATED_FINISH_REASON = "MAX_TOKENS"

# Published batch prices (USD per 1M tokens) used only for the dry-run estimate.
BATCH_PRICE_PER_M = {
    "gemini-3.8-flash":       (0.375, 1.875),
    "gemini-3.1-flash-lite":  (0.125, 0.75),
    "gemini-3.1-pro-preview": (1.0, 6.0),
}
ESTIMATED_OUTPUT_TOKENS_PER_REQUEST = 1_500   # one pass incl. thinking (low)

SETTINGS = {
    "model":          GEMINI_MODEL,
    "thinking_level": THINKING_LEVEL,
    "enqueue_cap":    ENQUEUE_CAP_TOKENS,
}

RUN_COUNTERS = ("summarized", "not_protocol", "failed", "truncated_resplit", "truncated_accepted")


# ── Request keys ──────────────────────────────────────────────────────────────
# One batch request per (meeting, task); Gemini batch output order is not guaranteed, so results
# are correlated by key "<meeting_id>|topics" (or "<meeting_id>|topics:<start>-<end>" for a transcript
# longer than the model context) or "<meeting_id>|opinions:<start>-<end>".

def _opinions_task(span: list[int]) -> str:
    return f"{OPINIONS_TASK}:{span[0]}-{span[1]}"


def _topics_task(span: list[int]) -> str:
    return f"{TOPICS_TASK}:{span[0]}-{span[1]}"


def _topic_tasks(entry: dict) -> list[str]:
    """[TOPICS_TASK] for a transcript that fits the context (and for queue entries saved before topic
    chunking, which have no topic_spans), else one task per chunk."""
    spans = entry.get("topic_spans") or []
    return [TOPICS_TASK] if len(spans) <= 1 else [_topics_task(span) for span in spans]


def _task_kind(task: str) -> str:
    return task.partition(":")[0]


def _make_key(entry: dict, task: str) -> str:
    return f"{entry['meeting_id']}|{task}"


def _split_key(key: str) -> tuple[str, str]:
    meeting_id, _, task = key.partition("|")
    return meeting_id, task


def _span_of_task(task: str) -> list[int] | None:
    if ":" not in task:
        return None
    start, _, end = task.partition(":")[2].partition("-")
    return [int(start), int(end)]


# ── Summary output ────────────────────────────────────────────────────────────

def _write_summary(summ_path: Path, is_protocol: bool, topics: list[str], opinions: list[dict]) -> None:
    summ_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"is_protocol": is_protocol, "topics": topics, "opinions": opinions}
    summ_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_transcript(entry: dict) -> str:
    return build_transcript_text(load_meeting(entry["proto"]))


def _finish_entry(entry: dict, state: dict) -> None:
    """Every request is in: verify quotes, write the JSON, count it."""
    summ_path = Path(entry["summ"])
    if entry["not_protocol"]:
        _write_summary(summ_path, False, [], [])
        state["stats"]["not_protocol"] += 1
        return

    topic_lists = [entry["results"][task] for task in _topic_tasks(entry)]
    if not any(topic_lists):
        _write_summary(summ_path, False, [], [])
        state["stats"]["not_protocol"] += 1
        return
    topics = list(dict.fromkeys(topic for topic_list in topic_lists for topic in topic_list))
    opinions = [opinion for span in entry["opinion_spans"]
                for opinion in entry["results"][_opinions_task(span)]]
    try:
        transcript = _load_transcript(entry)
    except Exception as exc:
        print(f"  [verify] cannot reload {Path(entry['proto']).name} for quote check: {exc}")
        transcript = ""
    verify_quotes(opinions, transcript)
    unverified = sum(1 for o in opinions if not o["quote_verified"])
    if unverified:
        tqdm.write(f"  [verify] {summ_path.name}: {unverified}/{len(opinions)} quotes not found verbatim")

    _write_summary(summ_path, True, topics, opinions)
    state["stats"]["summarized"] += 1


# ── Request building ──────────────────────────────────────────────────────────

def _generation_config() -> dict:
    cfg = {"maxOutputTokens": MAX_TOKENS, "temperature": TEMPERATURE}
    level = SETTINGS["thinking_level"]
    if level and level != "none":
        cfg["thinkingConfig"] = {"thinkingLevel": level}
    return cfg


def _build_request(system_prompt: str, committee: str, date: str, meeting_id: str, transcript: str,
                   part: tuple[int, int] | None = None) -> dict:
    part_line = f"חלק {part[0]} מתוך {part[1]} של הפרוטוקול\n" if part else ""
    user_text = (
        f"גוף: {committee}\n"
        f"תאריך: {date}\n"
        f"מזהה ישיבה: {meeting_id}\n"
        f"{part_line}\n"
        f"פרוטוקול הישיבה:\n\n{transcript}"
    )
    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents":          [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig":  _generation_config(),
    }


def _all_tasks(entry: dict) -> list[str]:
    return _topic_tasks(entry) + [_opinions_task(span) for span in entry["opinion_spans"]]


def _pending_tasks(entry: dict) -> list[str]:
    return [task for task in _all_tasks(entry) if entry["results"].get(task) is None]


def _requests_for_entry(entry: dict, transcript: str) -> list[tuple[dict, str]]:
    out = []
    for task in _pending_tasks(entry):
        kind = _task_kind(task)
        span = _span_of_task(task)
        spans = entry["opinion_spans"] if kind == OPINIONS_TASK else entry.get("topic_spans") or []
        part = (spans.index(span) + 1, len(spans)) if span is not None and len(spans) > 1 else None
        text = transcript if span is None else transcript[span[0]:span[1]]
        req = _build_request(PROMPTS[kind], entry["committee"], entry["date"], entry["meeting_id"], text, part)
        out.append((req, _make_key(entry, task)))
    return out


def _build_requests_for_entries(entries: list[dict], desc: str = "Building requests") -> list[tuple[dict, str]]:
    """
    One request per (meeting, pending task). Returns [(request, key), ...].
    Meetings whose transcript fails to load are skipped with a warning.
    """
    out: list[tuple[dict, str]] = []
    for entry in tqdm(entries, desc=desc, unit="meeting", leave=False):
        try:
            transcript = _load_transcript(entry)
        except Exception as exc:
            tqdm.write(f"  [WARN] load failed {Path(entry['proto']).name}: {exc}")
            continue
        out.extend(_requests_for_entry(entry, transcript))
    return out


# ── Result processing ─────────────────────────────────────────────────────────

def _without_last_line(text: str) -> str:
    """A truncated answer ends mid-line; keep only its complete lines."""
    return text.rsplit("\n", 1)[0] if "\n" in text else ""


def _resplit_truncated_span(entry: dict, span: list[int]) -> bool:
    """Replace a truncated opinions chunk by its two halves (cut at a speaker turn).
    False when the chunk is too short to split or the split fails."""
    if span[1] - span[0] < MIN_RESPLIT_CHARS:
        return False
    try:
        halves = split_span(_load_transcript(entry), span[0], span[1], 2)
    except Exception as exc:
        tqdm.write(f"  [truncated] cannot reload {Path(entry['proto']).name} to split it: {exc}")
        return False
    if len(halves) < 2:
        return False
    position = entry["opinion_spans"].index(span)
    entry["opinion_spans"][position:position + 1] = halves
    entry["results"].pop(_opinions_task(span), None)
    for half in halves:
        entry["results"][_opinions_task(half)] = None
    return True


def _apply_task_result(entry: dict, task: str, text: str | None, finish_reason: str, state: dict) -> bool:
    """Parse one answer into entry["results"]. Returns False when the output is unusable.
    An answer cut off by the output cap is split and re-requested (opinions) or kept without its
    cut-off last line (topics, or an opinions chunk too short to split). "Not a protocol" from a
    whole-transcript topics answer ends the meeting; from a topics chunk it counts only if every chunk says so."""
    if text is None:
        return False
    truncated = finish_reason == TRUNCATED_FINISH_REASON
    span = _span_of_task(task)
    is_opinions = _task_kind(task) == OPINIONS_TASK
    if truncated and is_opinions and _resplit_truncated_span(entry, span):
        state["stats"]["truncated_resplit"] += 1
        tqdm.write(f"  [truncated] {Path(entry['proto']).name} opinions {span}: output cap reached, "
                   f"re-requesting as {len(entry['opinion_spans'])} chunks")
        return True
    if truncated:
        state["stats"]["truncated_accepted"] += 1
        tqdm.write(f"  [truncated] {Path(entry['proto']).name} {task}: output cap reached, keeping the complete lines")
        text = _without_last_line(text)
    parsed = parse_opinions(text) if is_opinions else parse_topics(text)
    if parsed is None:
        return False
    entry["results"][task] = parsed
    if parsed == [] and task == TOPICS_TASK:
        entry["not_protocol"] = True
    return True


def _process_results(results: list[dict], keys_in_batch: list[str], state: dict) -> None:
    """
    Apply batch results to the queue entries, updating state in-place.

    A key with an error, an unparsable answer, or no result at all counts as
    one attempt for that request; MAX_PASS_ATTEMPTS drops the meeting (no file
    is written, so a later run retries it). A meeting whose requests are all
    in is written to disk and removed from the queue.
    """
    queue = state["queue"]
    entries_by_id = {e["meeting_id"]: e for e in queue}
    pending = set(keys_in_batch)
    done_ids: set[str] = set()

    def _fail(entry: dict, task: str, reason: str) -> None:
        attempts = entry["attempts"][task] = entry["attempts"].get(task, 0) + 1
        tqdm.write(f"  [ERROR] {Path(entry['proto']).name} ({task}): {reason}  attempt {attempts}/{MAX_PASS_ATTEMPTS}")
        if attempts >= MAX_PASS_ATTEMPTS:
            state["stats"]["failed"] += 1
            done_ids.add(entry["meeting_id"])

    for result in results:
        key = result.get("key")
        meeting_id, task = _split_key(key or "")
        entry = entries_by_id.get(meeting_id)
        if entry is None or task not in _all_tasks(entry) or key not in pending:
            tqdm.write(f"  [WARN] result with unknown key {key!r}, skipping")
            continue
        pending.discard(key)
        if entry["meeting_id"] in done_ids:
            continue

        response = result.get("response") or {}
        text, finish_reason = extract_text(response)
        if not _apply_task_result(entry, task, text, finish_reason, state):
            err_src = result.get("error") or response.get("error") or {}
            reason = err_src.get("message", "empty or unparsable response") if isinstance(err_src, dict) else str(err_src)
            _fail(entry, task, reason)
            continue

        if not _pending_tasks(entry) or entry["not_protocol"]:
            _finish_entry(entry, state)
            done_ids.add(entry["meeting_id"])

    for key in pending:
        meeting_id, task = _split_key(key)
        entry = entries_by_id.get(meeting_id)
        if entry is not None and meeting_id not in done_ids and task in _all_tasks(entry):
            _fail(entry, task, "no result in batch output")

    state["queue"] = [e for e in queue if e["meeting_id"] not in done_ids]


# ── Scan phase ────────────────────────────────────────────────────────────────

def _new_entry(proto_path: Path, committee: str, date_iso: str, meeting_id: str, transcript: str) -> dict:
    spans = opinion_chunk_spans(transcript)
    entry = {
        "proto":         str(proto_path),
        "summ":          str(summary_path_for_transcript(proto_path)),
        "committee":     committee,
        "date":          date_iso,
        "meeting_id":    meeting_id,
        "opinion_spans": spans,
        "topic_spans":   topic_chunk_spans(transcript, MAX_TRANSCRIPT_CHARS),
        "attempts":      {},
        "results":       {},
        "not_protocol":  False,
    }
    for task in _all_tasks(entry):
        entry["results"][task] = None
    return entry


def _new_state(knesset_num: int, queue: list[dict]) -> dict:
    return {
        "version":     STATE_VERSION,
        "knesset_num": knesset_num,
        "model":       SETTINGS["model"],
        "queue":       queue,
        "active_jobs": [],
        "stats":       dict.fromkeys(RUN_COUNTERS, 0),
    }


def _scan_transcripts(knesset_num: int, force_summarize: bool, skip_patterns: list[str],
                      previous_queue: list[dict] = ()) -> tuple[list[dict], dict]:
    """
    Queue every transcript on disk that has no summary (under any committee folder). An entry of
    previous_queue for the same transcript is kept as is, with the answers it already has.
    Returns (queue, scan counters).
    """
    print(f"\n{'='*60}\nScan — Knesset {knesset_num}\n{'='*60}")
    transcripts = transcripts_by_meeting(knesset_num)
    summarized = {meeting_id for meeting_id, paths in
                  json_files_by_meeting(config.summaries_dir(knesset_num), "summaries").items()
                  if any(path.stat().st_size > 0 for path in paths)}
    skipped_dirnames = [safe_dirname(pattern) for pattern in skip_patterns]
    previous_by_proto = {entry["proto"]: entry for entry in previous_queue}

    queue: list[dict] = []
    counts = dict.fromkeys(("transcripts", "already_done", "skipped", "unreadable"), 0)
    counts["transcripts"] = len(transcripts)
    for meeting_id, proto_path in tqdm(sorted(transcripts.items()), desc="Scanning transcripts", unit="meeting"):
        if any(dirname in proto_path.parent.name for dirname in skipped_dirnames):
            counts["skipped"] += 1
            continue
        if meeting_id in summarized and not force_summarize:
            counts["already_done"] += 1
            continue
        if str(proto_path) in previous_by_proto:
            queue.append(previous_by_proto[str(proto_path)])
            continue
        try:
            meeting = load_meeting(proto_path)
            transcript = build_transcript_text(meeting)
        except Exception as exc:
            tqdm.write(f"  [WARN] {proto_path.name}: {exc}")
            counts["unreadable"] += 1
            continue
        committee = str(meeting.get("committee") or proto_path.parent.name.replace("_", " "))
        queue.append(_new_entry(proto_path, committee, str(meeting.get("date") or ""), meeting_id, transcript))

    n_chunked = sum(1 for entry in queue if len(entry["opinion_spans"]) > 1)
    n_topics_chunked = sum(1 for entry in queue if len(entry.get("topic_spans") or []) > 1)
    print("\nScan complete:")
    print(f"  Transcripts on disk : {counts['transcripts']}")
    print(f"  Already summarized  : {counts['already_done']}")
    print(f"  Skipped (--skip)    : {counts['skipped']}")
    print(f"  Unreadable          : {counts['unreadable']}")
    print(f"  Queued              : {len(queue)}  ({n_chunked} with a chunked opinions pass, "
          f"{n_topics_chunked} longer than the model context with a chunked topics pass)")
    return queue, counts


def _apply_sample(state: dict, sample: int, seed: int) -> None:
    if sample < len(state["queue"]):
        state["queue"] = random.Random(seed).sample(state["queue"], sample)
        print(f"  Sampled {sample} meetings (seed={seed})")


# ── Runs ──────────────────────────────────────────────────────────────────────

def _prepare_sub_batches(state: dict, tag: str) -> list[tuple[list, str]]:
    items = _build_requests_for_entries(state["queue"], desc=f"{tag} building")
    if not items:
        return []
    sub_batches = [(batch, f"knesset{state['knesset_num']}-{tag}-{i+1:03d}")
                   for i, batch in enumerate(split_batches(items, MAX_BATCH_INPUT_TOKENS, MAX_REQUESTS_PER_BATCH))]
    total_tok = sum(estimate_request_tokens(r) for batch, _ in sub_batches for r, _ in batch)
    print(f"  {len(items)} requests across {len(sub_batches)} sub-batch(es)  (~{total_tok/1_000_000:.1f}M tokens)")
    return sub_batches


def _run(client, state: dict, state_path: Path, tmp_dir: Path) -> None:
    """Submit everything pending, drain, then retry meetings that still have pending requests."""
    round_num = 0
    while state["queue"]:
        round_num += 1
        print(f"\n{'='*60}\nRound {round_num} — {len(state['queue'])} meeting(s) pending\n{'='*60}")
        sub_batches = _prepare_sub_batches(state, f"r{round_num:02d}")
        if not sub_batches:
            print("  No buildable requests, clearing queue.")
            state["queue"] = []
            break
        run_pool(client, SETTINGS["model"], sub_batches, tmp_dir, state, state_path, f"Round {round_num}",
                 _process_results, enqueue_cap=SETTINGS["enqueue_cap"], max_concurrent_jobs=MAX_CONCURRENT_JOBS,
                 poll_interval_s=POLL_INTERVAL_S)
        s = state["stats"]
        print(f"  Round {round_num} done — remaining={len(state['queue'])}  "
              f"summarized={s['summarized']}  not_proto={s['not_protocol']}  failed={s['failed']}")


def _estimated_output_tokens(request: dict, key: str) -> int:
    """Topics answers are short; opinions answers grow with the transcript (a ~150K-char meeting
    fills the output cap)."""
    if _task_kind(_split_key(key)[1]) == TOPICS_TASK:
        return 1_000
    return min(MAX_TOKENS, max(1_000, estimate_request_tokens(request) // 5))


def _dry_run(state: dict, tmp_dir: Path) -> None:
    """Build every JSONL file, print the token/cost estimate, submit nothing."""
    n_meetings = len(state["queue"])
    print(f"\n{'='*60}\nDry run — {n_meetings} meetings\n{'='*60}")
    sub_batches = _prepare_sub_batches(state, "dry")
    total_in = total_out = 0
    for items, label in sub_batches:
        path = write_jsonl(items, label, tmp_dir)
        total_in += sum(estimate_request_tokens(r) for r, _ in items)
        total_out += sum(_estimated_output_tokens(r, key) for r, key in items)
        print(f"  wrote {path.name}: {len(items)} requests, {path.stat().st_size // 1024} KB")

    price = BATCH_PRICE_PER_M.get(SETTINGS["model"])
    print(f"\n  Model            : {SETTINGS['model']} (thinking={SETTINGS['thinking_level']})")
    print(f"  Est. input tokens: {total_in/1_000_000:.1f}M  (CHARS_PER_TOK={CHARS_PER_TOK}, conservative)")
    print(f"  Est. output      : {total_out/1_000_000:.1f}M")
    if price:
        cost = total_in / 1e6 * price[0] + total_out / 1e6 * price[1]
        print(f"  Est. batch cost  : ${cost:,.2f}  at ${price[0]}/${price[1]} per M in/out")
    print(f"  Enqueue cap      : {SETTINGS['enqueue_cap']/1_000_000:.0f}M → "
          f"{max(1, -(-total_in // SETTINGS['enqueue_cap']))} wave(s)")
    print(f"  JSONL files      : {tmp_dir}")


# ── Entry point ───────────────────────────────────────────────────────────────

def _usable_state(state: dict | None, knesset_num: int, state_path: Path) -> dict | None:
    if state is None:
        return None
    if state.get("version") != STATE_VERSION or state.get("knesset_num") != knesset_num:
        if state.get("active_jobs"):
            print(f"[WARN] {state_path} is from an older script version and lists {len(state['active_jobs'])} "
                  f"in-flight job(s); they are NOT resumed. Finish them with the previous script version "
                  f"or cancel them in AI Studio.")
        else:
            print(f"[WARN] {state_path} is from an older script version or another Knesset, rescanning")
        return None
    return state


def summarize_knesset(knesset_num: int, *, skip_patterns: list[str] = (), force_summarize: bool = False,
                      rescan: bool = False, dry_run: bool = False, state_path: Path | None = None,
                      tmp_dir: Path | None = None, sample: int | None = None, seed: int = 0) -> dict:
    """
    Summarize every transcript of a Knesset that has no summary yet. Returns this run's counters
    (RUN_COUNTERS plus "queued": meetings still waiting when the run ended). Raises RuntimeError
    when no Gemini API key is set.
    """
    state_path = state_path or config.summary_batch_state_path(knesset_num)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = tmp_dir or Path(tempfile.mkdtemp(prefix="knesset_batch_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    print(f"State file       : {state_path}")
    print(f"JSONL debug files: {tmp_dir}")
    skip_patterns = [s.strip() for s in skip_patterns if s.strip()]

    state = _usable_state(load_state(state_path), knesset_num, state_path)
    if state is None:
        state = _new_state(knesset_num, [])
    state.setdefault("active_jobs", [])
    state["stats"] = dict.fromkeys(RUN_COUNTERS, 0)
    if state.get("model") and state["model"] != SETTINGS["model"] and state["queue"]:
        print(f"[WARN] state file was created for model {state['model']}, running with {SETTINGS['model']}")
    state["model"] = SETTINGS["model"]
    if state["queue"] or state["active_jobs"]:
        print(f"  Resuming: {len(state['queue'])} meeting(s) in the queue, {len(state['active_jobs'])} job(s) in flight")

    def rescan_into_state() -> None:
        queue, _ = _scan_transcripts(knesset_num, force_summarize, skip_patterns, state["queue"])
        state["queue"] = queue
        if sample:
            _apply_sample(state, sample, seed)
        save_state(state, state_path)

    rescanned = False
    if rescan or not state["queue"]:
        rescan_into_state()
        rescanned = True

    if dry_run:
        _dry_run(state, tmp_dir)
        return {**state["stats"], "queued": len(state["queue"])}

    api_key = os.environ.get(config.GOOGLE_API_KEY_ENV) or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(f"set {config.GOOGLE_API_KEY_ENV} or GEMINI_API_KEY to summarize")
    client = genai.Client(api_key=api_key)

    resume_active_jobs(client, state, state_path, _process_results, POLL_INTERVAL_S)
    _run(client, state, state_path, tmp_dir)
    if not rescanned and not sample:
        rescan_into_state()
        _run(client, state, state_path, tmp_dir)
    save_state(state, state_path)
    return {**state["stats"], "queued": len(state["queue"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--knesset",         type=int,  default=25)
    ap.add_argument("--state-file",      type=Path, default=None,
                    help="Resume/save state here (default: Data/summary_batches/batch_state_k<N>.json)")
    ap.add_argument("--tmp-dir",         type=Path, default=None, help="Directory for temporary JSONL upload files")
    ap.add_argument("--rescan",          action="store_true",
                    help="Rescan the disk before finishing a leftover queue (default: finish it, then rescan)")
    ap.add_argument("--force-summarize", action="store_true", help="Re-summarize even if a .json summary already exists")
    ap.add_argument("--skip",            nargs="*", default=[], help="Committee name substrings to skip")
    ap.add_argument("--model",           default=GEMINI_MODEL, help=f"Gemini model id (default {GEMINI_MODEL})")
    ap.add_argument("--thinking-level",  default=THINKING_LEVEL, choices=["none", "low", "medium", "high"],
                    help="Gemini 3.x thinking level; 'none' omits thinkingConfig")
    ap.add_argument("--enqueue-cap-tokens", type=int, default=ENQUEUE_CAP_TOKENS,
                    help="Max tokens enqueued across active jobs (tier 1: 3M, tier 2: 400M)")
    ap.add_argument("--sample",          type=int, default=None,
                    help="Pilot: keep only N random meetings from the scan")
    ap.add_argument("--seed",            type=int, default=0, help="Random seed for --sample")
    ap.add_argument("--dry-run",         action="store_true",
                    help="Scan + build JSONL + print cost estimate; submit nothing (scan state is saved for reuse)")
    args = ap.parse_args()

    SETTINGS["model"]          = args.model
    SETTINGS["thinking_level"] = args.thinking_level
    SETTINGS["enqueue_cap"]    = args.enqueue_cap_tokens

    try:
        stats = summarize_knesset(args.knesset, skip_patterns=args.skip or [], force_summarize=args.force_summarize,
                                  rescan=args.rescan, dry_run=args.dry_run, state_path=args.state_file,
                                  tmp_dir=args.tmp_dir, sample=args.sample, seed=args.seed)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"\n{'='*60}\nDONE — Knesset {args.knesset}\n{'='*60}")
    for key, value in stats.items():
        print(f"  {key:<20}: {value}")
    if stats["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
