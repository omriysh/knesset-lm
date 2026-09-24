"""
summarize_knesset_batches.py

Two-pass summarization for a Knesset using Google's Gemini Batch API.

Every meeting gets two batch requests, a topics pass and an opinions pass
(prompts in summarization/prompts.py). The model answers in plain text; Python
parses it (summarization/output_parsing.py), verifies every quote against the
transcript, and writes Data/summaries/<knesset>/<committee>/<stem>.json:

    {"is_protocol": bool,
     "topics":      [str, ...],
     "opinions":    [{"speaker": str, "opinion": str, "quote": str, "quote_verified": bool}, ...]}

The file is written only once both passes succeeded, so an existing file always
means a complete summary. Meetings longer than the model context are skipped.

State is saved to a JSON file after every batch so runs can be safely interrupted
and resumed. If interrupted mid-poll, all in-flight jobs are reconnected on
resume (no re-submission / double billing).

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
    # full run
    python scripts/summarize_knesset_batches.py --knesset 25
    python scripts/summarize_knesset_batches.py --knesset 25 --force-summarize
    python scripts/summarize_knesset_batches.py --knesset 25 --skip "ועדת הכנסת"
"""

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from pathlib import Path

from tqdm import tqdm

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google import genai

import config
from config import CHARS_PER_TOK, MAX_TOKENS
from utils.knesset_db import (
    get_all_committees,
    get_committee_sessions,
    get_session_transcript,
    SESSION_TYPE_CLASSIFIED,
)
from utils.meeting import load_meeting, build_transcript_text
from summarization.prompts import SYSTEM_PROMPT_TOPICS, SYSTEM_PROMPT_OPINIONS
from summarization.output_parsing import parse_topics, parse_opinions, verify_quotes

# ── Batch constants ───────────────────────────────────────────────────────────

_WIN_UNSAFE       = re.compile(r'[\\/:*?"<>|]')
_CANCELLED_STATUS = {193}

GEMINI_MODEL                   = "gemini-3.8-flash"
GEMINI_CTX_TOKENS              = 800_000                            # input tokens per request (1M ctx minus output headroom)
MAX_TRANSCRIPT_CHARS           = GEMINI_CTX_TOKENS * CHARS_PER_TOK
THINKING_LEVEL                 = "low"                              # gemini 3.x: low | medium | high | none (omit)
TEMPERATURE                    = 0.3
MAX_BATCH_INPUT_TOKENS         = 10_000_000                         # per-job cap
MAX_REQUESTS_PER_BATCH         = 500                                # per-job request count cap
MAX_CONCURRENT_JOBS            = 100                                # api limit on active batch jobs
ENQUEUE_CAP_TOKENS             = 380_000_000                        # cross-job enqueue cap (tier 2 = 400M for gemini-3.8-flash)
BATCH_METADATA_OVERHEAD_TOKENS = 100                                # per-line JSONL framing
POLL_INTERVAL_S                = 60
MAX_PASS_ATTEMPTS              = 3       # per-pass retry budget before giving up on the meeting

PASSES = {
    "topics":   SYSTEM_PROMPT_TOPICS,
    "opinions": SYSTEM_PROMPT_OPINIONS,
}

# Published batch prices (USD per 1M tokens) used only for the dry-run estimate.
BATCH_PRICE_PER_M = {
    "gemini-3.8-flash":       (0.375, 1.875),
    "gemini-3.1-flash-lite":  (0.125, 0.75),
    "gemini-3.1-pro-preview": (1.0, 6.0),
}
ESTIMATED_OUTPUT_TOKENS_PER_MEETING = 3_000   # both passes incl. thinking (low)

_QUOTA_ERROR_MARKERS = ("RESOURCE_EXHAUSTED", "QUOTA", "429", "RATE LIMIT")

SETTINGS = {
    "model":          GEMINI_MODEL,
    "thinking_level": THINKING_LEVEL,
    "enqueue_cap":    ENQUEUE_CAP_TOKENS,
}


# ── Path helpers ──────────────────────────────────────────────────────────────

def _safe_dirname(name: str) -> str:
    return re.sub(r"[\s_]+", "_", _WIN_UNSAFE.sub("_", name)).strip("_")


def _session_filename(date_iso: str, session_id: int) -> str:
    if date_iso and len(date_iso) >= 10:
        y, m, d = date_iso[:10].split("-")
        return f"{d}_{m}_{y}_{session_id}"
    return f"00_00_0000_{session_id}"


def _make_key(entry: dict, pass_name: str) -> str:
    """Batch result key. Gemini batch output order is not guaranteed; correlate by key."""
    return f"{entry['meeting_id']}:{pass_name}"


def _split_key(key: str) -> tuple[str, str]:
    meeting_id, _, pass_name = key.rpartition(":")
    return meeting_id, pass_name


def _is_quota_error(exc: BaseException) -> bool:
    msg = str(exc).upper()
    return any(marker in msg for marker in _QUOTA_ERROR_MARKERS)


# ── Summary output ────────────────────────────────────────────────────────────

def _write_summary(summ_path: Path, is_protocol: bool, topics: list[str], opinions: list[dict]) -> None:
    summ_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"is_protocol": is_protocol, "topics": topics, "opinions": opinions}
    summ_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _finish_entry(entry: dict, state: dict) -> None:
    """Both passes are in: verify quotes, write the JSON, count it."""
    topics   = entry["results"]["topics"]
    opinions = entry["results"]["opinions"]
    summ_path = Path(entry["summ"])

    if entry["not_protocol"]:
        _write_summary(summ_path, False, [], [])
        state["stats"]["not_protocol"] += 1
        return

    try:
        transcript = build_transcript_text(load_meeting(entry["proto"]))
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


def _build_request(system_prompt: str, committee: str, date: str, meeting_id: str, transcript: str) -> dict:
    user_text = (
        f"ועדה: {committee}\n"
        f"תאריך: {date}\n"
        f"מזהה ישיבה: {meeting_id}\n\n"
        f"פרוטוקול הישיבה:\n\n{transcript}"
    )
    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents":          [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig":  _generation_config(),
    }


def _estimate_tokens(req: dict) -> int:
    total = 0
    for part in req.get("systemInstruction", {}).get("parts", []):
        total += len(part.get("text", ""))
    for content in req.get("contents", []):
        for part in content.get("parts", []):
            total += len(part.get("text", ""))
    return total // CHARS_PER_TOK + BATCH_METADATA_OVERHEAD_TOKENS


def _pending_passes(entry: dict) -> list[str]:
    return [p for p in PASSES if entry["results"].get(p) is None]


def _build_requests_for_entries(entries: list[dict], desc: str = "Building requests") -> list[tuple[dict, str]]:
    """
    One request per (meeting, pending pass). Returns [(request, key), ...].
    Meetings whose transcript fails to load are skipped with a warning.
    """
    out: list[tuple[dict, str]] = []
    for entry in tqdm(entries, desc=desc, unit="meeting", leave=False):
        proto_path = Path(entry["proto"])
        try:
            transcript = build_transcript_text(load_meeting(proto_path))
        except Exception as exc:
            tqdm.write(f"  [WARN] load failed {proto_path.name}: {exc}")
            continue
        for pass_name in _pending_passes(entry):
            req = _build_request(PASSES[pass_name], entry["committee"], entry["date"], entry["meeting_id"], transcript)
            out.append((req, _make_key(entry, pass_name)))
    return out


def _split_batches(items: list[tuple[dict, str]]) -> list[list[tuple[dict, str]]]:
    """Partition into sub-batches under MAX_BATCH_INPUT_TOKENS / MAX_REQUESTS_PER_BATCH."""
    batches: list[list] = []
    current: list = []
    current_tokens = 0
    for req, key in items:
        tok = _estimate_tokens(req)
        if tok > MAX_BATCH_INPUT_TOKENS:
            raise ValueError(f"Request {key} estimated at {tok:,} tokens exceeds per-batch cap {MAX_BATCH_INPUT_TOKENS:,}")
        if current and (current_tokens + tok > MAX_BATCH_INPUT_TOKENS or len(current) == MAX_REQUESTS_PER_BATCH):
            batches.append(current)
            current, current_tokens = [], 0
        current.append((req, key))
        current_tokens += tok
    if current:
        batches.append(current)
    return batches


# ── Result extraction ─────────────────────────────────────────────────────────

def _extract_text(response: dict) -> str | None:
    if not response or "error" in response:
        return None
    candidates = response.get("candidates", [])
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p["text"] for p in parts if "text" in p and not p.get("thought")).strip() or None


def _download_results(client: genai.Client, job) -> list[dict]:
    dest = getattr(job, "dest", None)
    if dest is None:
        raise RuntimeError(f"Cannot locate output file on completed job {job.name}; check the google-genai SDK version")
    dest_name = getattr(dest, "file_name", None) or str(dest)
    raw = client.files.download(file=dest_name)
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


# ── Result processing ─────────────────────────────────────────────────────────

def _apply_pass_result(entry: dict, pass_name: str, text: str | None) -> bool:
    """Parse one pass output into entry["results"]. Returns False when the output is unusable."""
    if text is None:
        return False
    parsed = parse_topics(text) if pass_name == "topics" else parse_opinions(text)
    if parsed is None:
        return False
    entry["results"][pass_name] = parsed
    if parsed == [] and pass_name == "topics":
        entry["not_protocol"] = True
    return True


def _process_results(results: list[dict], keys_in_batch: list[str], state: dict) -> None:
    """
    Apply batch results to the queue entries, updating state in-place.

    A key with an error, an unparsable answer, or no result at all counts as
    one attempt for that pass; MAX_PASS_ATTEMPTS drops the meeting (no file
    is written, so a later run retries it). A meeting whose passes are both
    in is written to disk and removed from the queue.
    """
    queue = state["queue"]
    entries_by_id = {e["meeting_id"]: e for e in queue}
    pending = set(keys_in_batch)
    done_ids: set[str] = set()

    def _fail(entry: dict, pass_name: str, reason: str) -> None:
        entry["attempts"][pass_name] += 1
        tqdm.write(f"  [ERROR] {Path(entry['proto']).name} ({pass_name}): {reason}  attempt {entry['attempts'][pass_name]}/{MAX_PASS_ATTEMPTS}")
        if entry["attempts"][pass_name] >= MAX_PASS_ATTEMPTS:
            state["stats"]["failed"] += 1
            done_ids.add(entry["meeting_id"])

    for result in results:
        key = result.get("key")
        meeting_id, pass_name = _split_key(key or "")
        entry = entries_by_id.get(meeting_id)
        if entry is None or pass_name not in PASSES or key not in pending:
            tqdm.write(f"  [WARN] result with unknown key {key!r}, skipping")
            continue
        pending.discard(key)
        if entry["meeting_id"] in done_ids:
            continue

        response = result.get("response") or {}
        if not _apply_pass_result(entry, pass_name, _extract_text(response)):
            err_src = result.get("error") or response.get("error") or {}
            reason = err_src.get("message", "empty or unparsable response") if isinstance(err_src, dict) else str(err_src)
            _fail(entry, pass_name, reason)
            continue

        if not _pending_passes(entry) or entry["not_protocol"]:
            _finish_entry(entry, state)
            done_ids.add(entry["meeting_id"])

    for key in pending:
        meeting_id, pass_name = _split_key(key)
        entry = entries_by_id.get(meeting_id)
        if entry is not None and meeting_id not in done_ids:
            _fail(entry, pass_name, "no result in batch output")

    state["queue"] = [e for e in queue if e["meeting_id"] not in done_ids]


# ── State persistence ─────────────────────────────────────────────────────────

def _save_state(state: dict, path: Path) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[WARN] Corrupted state file {path} ({exc}), starting fresh.")
        return None


# ── Concurrent pool ───────────────────────────────────────────────────────────

def _write_jsonl(items: list[tuple[dict, str]], label: str, tmp_dir: Path) -> Path:
    jsonl_path = tmp_dir / f"{label}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for req, key in items:
            f.write(json.dumps({"key": key, "request": req}, ensure_ascii=False) + "\n")
    return jsonl_path


def _submit_no_poll(client, items, label, tmp_dir, state, state_path, est_tokens) -> dict:
    """Write JSONL, upload, submit. Adds the job to state["active_jobs"] and saves state."""
    jsonl_path = _write_jsonl(items, label, tmp_dir)
    tqdm.write(f"  [upload] {label}: {len(items)} requests, ~{est_tokens/1_000_000:.2f}M tokens est, "
               f"{jsonl_path.stat().st_size // 1024} KB")

    uploaded = client.files.upload(file=jsonl_path, config={"mime_type": "application/jsonl"})
    try:
        job = client.batches.create(model=SETTINGS["model"], src=uploaded.name, config={"display_name": label})
    except Exception as exc:
        print(f"  [submit] batches.create failed for {label}: {exc}, deleting orphan upload")
        try:
            client.files.delete(name=uploaded.name)
        except Exception as del_exc:
            print(f"  [submit] could not delete upload {uploaded.name}: {del_exc}")
        raise

    info = {
        "job_name":      job.name,
        "uploaded_name": uploaded.name,
        "batch_label":   label,
        "est_tokens":    est_tokens,
        "keys":          [key for _, key in items],
    }
    state.setdefault("active_jobs", []).append(info)
    _save_state(state, state_path)
    return info


def _poll_and_drain(client, active: list[dict], state: dict, state_path: Path) -> list[dict]:
    """Poll active jobs; process every terminal one and return their infos."""
    terminal = ("SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "ERROR", "EXPIRED")
    drained: list[dict] = []

    for info in list(active):
        try:
            job = client.batches.get(name=info["job_name"])
        except Exception as exc:
            tqdm.write(f"  [WARN] get job {info['batch_label']}: {exc}")
            continue
        state_str = str(getattr(job, "state", "")).upper()
        if not any(s in state_str for s in terminal):
            continue

        if any(s in state_str for s in ("SUCCEEDED", "COMPLETED")):
            try:
                results = _download_results(client, job)
            except Exception as exc:
                tqdm.write(f"  [WARN] download {info['batch_label']}: {exc}, leaving for retry")
                continue
            tqdm.write(f"  [drain] {info['batch_label']} SUCCEEDED ({len(results)} results)")
            _process_results(results, info["keys"], state)
        else:
            tqdm.write(f"  [drain] {info['batch_label']} {state_str}, bumping attempts for {len(info['keys'])} requests")
            _process_results([], info["keys"], state)

        try:
            client.files.delete(name=info["uploaded_name"])
        except Exception as exc:
            tqdm.write(f"  [WARN] delete upload {info['uploaded_name']}: {exc}")

        state["active_jobs"] = [j for j in state["active_jobs"] if j["job_name"] != info["job_name"]]
        _save_state(state, state_path)
        drained.append(info)

    return drained


def _run_pool(client, sub_batches: list[tuple[list, str]], tmp_dir: Path, state: dict, state_path: Path, desc: str) -> None:
    """
    Submit sub-batches under the enqueued-token budget, drain completions as
    they arrive, refill freed budget from pending. Quota errors pause submission
    until something drains.
    """
    pending = list(sub_batches)
    active: list[dict] = []
    budget_used = 0
    enqueue_cap = SETTINGS["enqueue_cap"]

    pbar = tqdm(total=sum(len(items) for items, _ in sub_batches), desc=desc, unit="req", dynamic_ncols=True)

    while pending or active:
        submitted = 0
        while pending:
            items, label = pending[0]
            est = sum(_estimate_tokens(r) for r, _ in items)
            if active and (budget_used + est > enqueue_cap or len(active) >= MAX_CONCURRENT_JOBS):
                tqdm.write(f"  [budget] pool full ({len(active)} jobs, {budget_used/1_000_000:.1f}M used), draining")
                break
            try:
                info = _submit_no_poll(client, items, label, tmp_dir, state, state_path, est)
            except Exception as exc:
                if _is_quota_error(exc):
                    tqdm.write(f"  [quota] server refused submit, waiting for drain: {exc}")
                    break
                raise
            active.append(info)
            budget_used += est
            pending.pop(0)
            submitted += 1
            tqdm.write(f"  [submit] {label}  est={est/1_000_000:.2f}M tok  pool={len(active)}  "
                       f"used={budget_used/1_000_000:.1f}/{enqueue_cap/1_000_000:.0f}M  pending={len(pending)}")

        drained = _poll_and_drain(client, active, state, state_path)
        for info in drained:
            active.remove(info)
            budget_used -= info["est_tokens"]
            pbar.update(len(info["keys"]))

        if not submitted and not drained and active:
            time.sleep(POLL_INTERVAL_S)

    pbar.close()


def _resume_active_jobs(client, state: dict, state_path: Path) -> None:
    """Drain every job that was in flight when the previous run stopped."""
    active = list(state.get("active_jobs", []))
    if not active:
        return
    print(f"\nResuming {len(active)} active batch job(s) from prior run …")
    while active:
        for info in _poll_and_drain(client, active, state, state_path):
            active.remove(info)
        if active:
            tqdm.write(f"  [resume] {len(active)} job(s) still running, sleeping {POLL_INTERVAL_S}s …")
            time.sleep(POLL_INTERVAL_S)
    print("  Resume complete.\n")


# ── Scan phase ────────────────────────────────────────────────────────────────

def _new_entry(proto_path: Path, summ_path: Path, committee: str, date_iso: str, session_id: int) -> dict:
    return {
        "proto":        str(proto_path),
        "summ":         str(summ_path),
        "committee":    committee,
        "date":         date_iso,
        "meeting_id":   str(session_id),
        "attempts":     {p: 0 for p in PASSES},
        "results":      {p: None for p in PASSES},
        "not_protocol": False,
    }


def _scan_committees(knesset_num: int, force_summarize: bool, skip_patterns: list[str]) -> dict:
    """Walk all committees, download missing protocols, queue every unsummarized meeting."""
    print(f"\n{'='*60}\nScan — Knesset {knesset_num}\n{'='*60}")
    committees = get_all_committees(knesset_num)
    if not committees:
        print("No committees found.")
        sys.exit(1)
    print(f"Found {len(committees)} committees.")

    if skip_patterns:
        before = len(committees)
        committees = [c for c in committees if not any(p in c["Name"] for p in skip_patterns)]
        print(f"Skipping {before - len(committees)} committee(s) by name pattern.")

    queue: list[dict] = []
    cnt = {k: 0 for k in ("total", "classified", "cancelled", "downloaded", "no_transcript", "already_done", "too_long")}

    for committee in tqdm(committees, desc="Scanning committees", unit="committee"):
        name         = committee["Name"]
        committee_id = committee["CommitteeID"]
        dirname      = _safe_dirname(name)
        proto_dir    = config.transcriptions_dir(knesset_num) / dirname
        summ_dir     = config.summaries_dir(knesset_num)      / dirname

        try:
            sessions = get_committee_sessions(committee_id, knesset_num)
        except Exception as exc:
            tqdm.write(f"  [WARN] sessions fetch failed for {name}: {exc}")
            continue
        if not sessions:
            continue

        proto_dir.mkdir(parents=True, exist_ok=True)
        summ_dir.mkdir(parents=True, exist_ok=True)

        for session in tqdm(sessions, desc=f"{name[:30]}", unit="session", leave=False):
            cnt["total"] += 1
            session_id = session["session_id"]
            date_iso   = session["date"]
            stem       = _session_filename(date_iso, session_id)
            proto_path = proto_dir / f"{stem}.json"
            summ_path  = summ_dir  / f"{stem}.json"

            if session.get("type_id") == SESSION_TYPE_CLASSIFIED:
                cnt["classified"] += 1
                continue
            if session.get("status_id") in _CANCELLED_STATUS:
                cnt["cancelled"] += 1
                continue
            if summ_path.exists() and summ_path.stat().st_size > 0 and not force_summarize:
                cnt["already_done"] += 1
                continue

            if not proto_path.exists():
                try:
                    transcript = get_session_transcript(session_id)
                except Exception as exc:
                    tqdm.write(f"  [WARN] transcript fetch failed for session {session_id}: {exc}")
                    cnt["no_transcript"] += 1
                    continue
                if not transcript:
                    cnt["no_transcript"] += 1
                    continue
                payload = {"meeting_id": str(session_id), "date": date_iso, "committee": name,
                           "knesset_num": knesset_num, **transcript}
                proto_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                cnt["downloaded"] += 1

            try:
                transcript_chars = len(build_transcript_text(load_meeting(proto_path)))
            except Exception as exc:
                tqdm.write(f"  [WARN] {proto_path.name}: {exc}")
                continue
            if transcript_chars > MAX_TRANSCRIPT_CHARS:
                cnt["too_long"] += 1
                tqdm.write(f"  [skip-long] {proto_path.name} ({transcript_chars:,} chars)")
                continue

            queue.append(_new_entry(proto_path, summ_path, name, date_iso, session_id))

    print(f"\nScan complete:")
    print(f"  Total sessions    : {cnt['total']}")
    print(f"  Classified/cancel : {cnt['classified'] + cnt['cancelled']}")
    print(f"  Already done      : {cnt['already_done']}")
    print(f"  No transcript     : {cnt['no_transcript']}")
    print(f"  Too long (skip)   : {cnt['too_long']}")
    print(f"  Downloaded        : {cnt['downloaded']}")
    print(f"  Queued            : {len(queue)}")

    return {
        "knesset_num":   knesset_num,
        "model":         SETTINGS["model"],
        "scan_complete": True,
        "queue":         queue,
        "active_jobs":   [],
        "stats":         {"summarized": 0, "not_protocol": 0, "failed": 0},
    }


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
                   for i, batch in enumerate(_split_batches(items))]
    total_tok = sum(_estimate_tokens(r) for batch, _ in sub_batches for r, _ in batch)
    print(f"  {len(items)} requests across {len(sub_batches)} sub-batch(es)  (~{total_tok/1_000_000:.1f}M tokens)")
    return sub_batches


def _run(client, state: dict, state_path: Path, tmp_dir: Path) -> None:
    """Submit everything pending, drain, then retry meetings that still have pending passes."""
    round_num = 0
    while state["queue"]:
        round_num += 1
        print(f"\n{'='*60}\nRound {round_num} — {len(state['queue'])} meeting(s) pending\n{'='*60}")
        sub_batches = _prepare_sub_batches(state, f"r{round_num:02d}")
        if not sub_batches:
            print("  No buildable requests, clearing queue.")
            state["queue"] = []
            break
        _run_pool(client, sub_batches, tmp_dir, state, state_path, desc=f"Round {round_num}")
        s = state["stats"]
        print(f"  Round {round_num} done — remaining={len(state['queue'])}  "
              f"summarized={s['summarized']}  not_proto={s['not_protocol']}  failed={s['failed']}")


def _dry_run(state: dict, tmp_dir: Path) -> None:
    """Build every JSONL file, print the token/cost estimate, submit nothing."""
    n_meetings = len(state["queue"])
    print(f"\n{'='*60}\nDry run — {n_meetings} meetings x {len(PASSES)} passes\n{'='*60}")
    sub_batches = _prepare_sub_batches(state, "dry")
    total_in = 0
    for items, label in sub_batches:
        path = _write_jsonl(items, label, tmp_dir)
        total_in += sum(_estimate_tokens(r) for r, _ in items)
        print(f"  wrote {path.name}: {len(items)} requests, {path.stat().st_size // 1024} KB")

    total_out = n_meetings * ESTIMATED_OUTPUT_TOKENS_PER_MEETING
    price = BATCH_PRICE_PER_M.get(SETTINGS["model"])
    print(f"\n  Model            : {SETTINGS['model']} (thinking={SETTINGS['thinking_level']})")
    print(f"  Est. input tokens: {total_in/1_000_000:.1f}M  (CHARS_PER_TOK={CHARS_PER_TOK}, conservative)")
    print(f"  Est. output      : {total_out/1_000_000:.1f}M")
    if price:
        cost = total_in / 1e6 * price[0] + total_out / 1e6 * price[1]
        print(f"  Est. batch cost  : ${cost:,.0f}  at ${price[0]}/${price[1]} per M in/out")
    print(f"  Enqueue cap      : {SETTINGS['enqueue_cap']/1_000_000:.0f}M → "
          f"{max(1, -(-total_in // SETTINGS['enqueue_cap']))} wave(s)")
    print(f"  JSONL files      : {tmp_dir}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--knesset",         type=int,  default=25)
    ap.add_argument("--state-file",      type=Path, default=None,
                    help="Resume/save state here (default: batch_state_k<N>.json in cwd)")
    ap.add_argument("--tmp-dir",         type=Path, default=None, help="Directory for temporary JSONL upload files")
    ap.add_argument("--force-summarize", action="store_true", help="Re-summarize even if a .json summary already exists")
    ap.add_argument("--skip",            nargs="*", default=[], help="Committee name substrings to skip")
    ap.add_argument("--model",           default=GEMINI_MODEL, help=f"Gemini model id (default {GEMINI_MODEL})")
    ap.add_argument("--thinking-level",  default=THINKING_LEVEL, choices=["none", "low", "medium", "high"],
                    help="Gemini 3.x thinking level; 'none' omits thinkingConfig")
    ap.add_argument("--enqueue-cap-tokens", type=int, default=ENQUEUE_CAP_TOKENS,
                    help="Max tokens enqueued across active jobs (tier 1: 3M, tier 2: 400M)")
    ap.add_argument("--sample",          type=int, default=None,
                    help="Pilot: keep only N random meetings from the scan (applied when the state is created)")
    ap.add_argument("--seed",            type=int, default=0, help="Random seed for --sample")
    ap.add_argument("--dry-run",         action="store_true",
                    help="Scan + build JSONL + print cost estimate; submit nothing (scan state is saved for reuse)")
    args = ap.parse_args()

    SETTINGS["model"]          = args.model
    SETTINGS["thinking_level"] = args.thinking_level
    SETTINGS["enqueue_cap"]    = args.enqueue_cap_tokens

    state_path = args.state_file or Path(f"batch_state_k{args.knesset}.json")
    tmp_dir    = args.tmp_dir or Path(tempfile.mkdtemp(prefix="knesset_batch_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    print(f"JSONL debug files: {tmp_dir}")

    skip_patterns = [s.strip() for s in (args.skip or []) if s.strip()]

    state = _load_state(state_path)
    if state is None or not state.get("scan_complete") or "queue" not in state:
        if state is not None and "queue" not in state:
            print(f"[WARN] {state_path} is from an older script version, rescanning")
        state = _scan_committees(args.knesset, args.force_summarize, skip_patterns)
        if args.sample:
            _apply_sample(state, args.sample, args.seed)
        _save_state(state, state_path)
        print(f"State saved: {state_path}")
    else:
        state.setdefault("active_jobs", [])
        if state.get("model") and state["model"] != SETTINGS["model"]:
            print(f"[WARN] state file was created for model {state['model']}, running with {SETTINGS['model']}")
        print(f"\n{'='*60}\nResuming — Knesset {state.get('knesset_num', args.knesset)}\n{'='*60}")
        print(f"  State file             : {state_path}")
        print(f"  Meetings remaining     : {len(state['queue'])}")
        print(f"  Active jobs (in-flight): {len(state['active_jobs'])}")
        print(f"  Already summarized     : {state['stats']['summarized']}")

    if args.dry_run:
        _dry_run(state, tmp_dir)
        return

    api_key = os.environ.get(config.GOOGLE_API_KEY_ENV) or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(f"ERROR: set {config.GOOGLE_API_KEY_ENV} or GEMINI_API_KEY")
        sys.exit(1)
    client = genai.Client(api_key=api_key)

    _resume_active_jobs(client, state, state_path)
    _run(client, state, state_path, tmp_dir)

    s = state["stats"]
    print(f"\n{'='*60}\nDONE — Knesset {args.knesset}\n{'='*60}")
    print(f"  Summarized     : {s['summarized']}")
    print(f"  Not protocol   : {s['not_protocol']}")
    print(f"  Failed         : {s['failed']}")
    print(f"  State file     : {state_path}")


if __name__ == "__main__":
    main()
