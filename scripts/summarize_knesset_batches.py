"""
summarize_knesset_batches.py

Batch summarization for a Knesset using Google's Gemini Batch API (gemini-2.5-flash-lite).

Phase 1: All single-chunk transcripts — submitted concurrently under an
         enqueued-token budget; results processed as each job drains.
Phase 2: Multi-chunk transcripts — one concurrent pool per round;
         each round advances every pending meeting by one chunk.

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
    python scripts/summarize_knesset_batches.py --knesset 25
    python scripts/summarize_knesset_batches.py --knesset 25 --state-file saved.json
    python scripts/summarize_knesset_batches.py --knesset 25 --force-summarize
    python scripts/summarize_knesset_batches.py --knesset 25 --skip "ועדת הכנסת"
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from google import genai

import config
from config import (
    NOT_PROTOCOL, CHARS_PER_TOK, MAX_TOKENS, MAX_SUMMARIZATION_CHUNKS,
)
from utils.knesset_db import (
    get_all_committees,
    get_committee_sessions,
    get_session_transcript,
    SESSION_TYPE_CLASSIFIED,
    get_active_committee_members_by_name,
)
from utils.meeting import (
    load_meeting, build_transcript_text, chunk_transcript, extract_attendance,
)
from summarization.pipeline import _build_attendance_block
from summarization.prompts import SYSTEM_PROMPT_BATCH_PASS1, SYSTEM_PROMPT_BATCH_CONTINUATION
from utils.knesset_db import get_mk_profile

# ── Batch constants ───────────────────────────────────────────────────────────

_WIN_UNSAFE       = re.compile(r'[\\/:*?"<>|]')
_CANCELLED_STATUS = {193}

# Matches  ח"כ FIRSTNAME [LASTNAME ...]  NOT already followed by  (party)
# Used to find unenriched MK mentions in the summary for post-processing.
_MK_UNENRICHED_RE = re.compile(
    r'(ח["\u05f3\u05f4\u2019\u201d]כ\s+'
    r'[\u05d0-\u05ea][\u05d0-\u05ea\'\-\u05f3\u05f4"]{0,20}'
    r'(?:\s+[\u05d0-\u05ea][\u05d0-\u05ea\'\-\u05f3\u05f4"]{0,20}){0,3})'
    r'(?!\s*\()',
    re.MULTILINE,
)
# Strip the ח"כ prefix to extract just the name
_MK_PREFIX_RE = re.compile(r'^ח["\u05f3\u05f4\u2019\u201d]כ\s+')

GEMINI_MODEL                    = "gemini-2.5-flash-lite"
GEMINI_CTX_TOKENS               = 500_000                            # input tokens per request
GEMINI_CHUNK_CHARS              = GEMINI_CTX_TOKENS * CHARS_PER_TOK  # 1 000 000 chars
MAX_BATCH_INPUT_TOKENS         = 9_500_000                         # per-batch cap (API limit 10M)
MAX_CONCURRENT_BATCH_REQUESTS = 100                                # api limit
ENQUEUE_CAP_TOKENS             = 9_500_000                         # cross-batch enqueue cap (API limit 10M)
BATCH_METADATA_OVERHEAD_TOKENS = 100                               # per-line JSONL framing
POLL_INTERVAL_S                = 60
MAX_POLL_ATTEMPTS              = 180     # 3 hours max (used by legacy single-job polling only)
MAX_ENTRY_ATTEMPTS             = 3       # per-entry retry budget before giving up

_QUOTA_ERROR_MARKERS = ("RESOURCE_EXHAUSTED", "QUOTA", "429", "RATE LIMIT")


# ── Path helpers ──────────────────────────────────────────────────────────────

def _safe_dirname(name: str) -> str:
    return re.sub(r"[\s_]+", "_", _WIN_UNSAFE.sub("_", name)).strip("_")


def _session_filename(date_iso: str, session_id: int) -> str:
    if date_iso and len(date_iso) >= 10:
        y, m, d = date_iso[:10].split("-")
        return f"{d}_{m}_{y}_{session_id}"
    return f"00_00_0000_{session_id}"


def _make_key(entry: dict) -> str:
    """
    Stable per-request key for batch result correlation. Gemini batch does not
    guarantee that output order matches input order — correlate by this key.
    """
    return f"{entry['meeting_id']}:{entry.get('chunk_index', 0)}"


def _is_quota_error(exc: BaseException) -> bool:
    msg = str(exc).upper()
    return any(marker in msg for marker in _QUOTA_ERROR_MARKERS)


# ── Cached profile lookups ───────────────────────────────────────────────────

@lru_cache(maxsize=4096)
def _cached_mk_profile(name: str, knesset_num: int):
    return get_mk_profile(name, knesset_num)


# ── Post-processing: attendance enrichment ────────────────────────────────────

def _enrich_summary_attendance(text: str, knesset_num: int) -> str:
    """
    Scan a completed summary for  ח"כ NAME  patterns that have no party info
    (i.e. not followed by  (...)). For each unique unenriched name, call
    get_mk_profile and insert  (party)  directly after the name.

    This catches MKs who appear in the body of the protocol but were not in
    the pre-computed attendance block, so the model couldn't add their party.
    """
    replacements: dict[str, str] = {}

    for m in _MK_UNENRICHED_RE.finditer(text):
        full_match = m.group(1)
        if full_match in replacements:
            continue

        name = _MK_PREFIX_RE.sub("", full_match).strip()
        profile = _cached_mk_profile(name, knesset_num)
        if not profile:
            replacements[full_match] = full_match
            continue

        factions = [
            f for f in (profile.get("factions") or [])
            if f and f.get("knesset") == knesset_num
        ]
        faction = max(factions, key=lambda f: f.get("start_date") or "", default=None)
        if not faction:
            replacements[full_match] = full_match
            continue

        replacements[full_match] = f"{full_match} ({faction['faction_name']})"

    for original, enriched in replacements.items():
        if original == enriched:
            continue
        # Negative lookahead prevents double-enriching on re-run
        pattern = re.compile(re.escape(original) + r'(?!\s*\()', re.MULTILINE)
        text = pattern.sub(enriched, text)

    return text


# ── Request building ──────────────────────────────────────────────────────────

def _build_request(
    system_prompt: str,
    committee: str,
    date: str,
    meeting_id: str,
    chunk: str,
    chunk_index: int,
    total_chunks: int,
    partial_summary: str | None,
    attendance_block: str,
) -> dict:
    """One Gemini batch JSONL request. No tool declarations — attendance is pre-computed."""
    header = (
        f"ועדה: {committee}\n"
        f"תאריך: {date}\n"
        f"מזהה ישיבה: {meeting_id}\n\n"
    )
    if attendance_block:
        header += f"נוכחים ונעדרים (מחושב מהפרוטוקול):\n{attendance_block}\n\n"

    if partial_summary is None:
        user_text = header + f"פרוטוקול הישיבה (חלק {chunk_index} מתוך {total_chunks}):\n\n{chunk}"
    else:
        user_text = (
            header
            + f"סיכום חלקי עד כה:\n{partial_summary}\n\n---\n\n"
            + f"המשך הפרוטוקול (חלק {chunk_index} מתוך {total_chunks}):\n\n{chunk}"
        )

    return {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents":          [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig":  {"maxOutputTokens": MAX_TOKENS, "temperature": 0.7},
    }


def _estimate_tokens(req: dict) -> int:
    total = 0
    for part in req.get("systemInstruction", {}).get("parts", []):
        total += len(part.get("text", ""))
    for content in req.get("contents", []):
        for part in content.get("parts", []):
            total += len(part.get("text", ""))
    return total // CHARS_PER_TOK + BATCH_METADATA_OVERHEAD_TOKENS


def _build_requests_for_entries(
    entries: list[dict],
    knesset_num: int,
    desc: str = "Building requests",
) -> tuple[list[dict], list[dict]]:
    """
    Build one Gemini request per queue entry. Loads meeting files from disk.
    Returns (requests, valid_entries) — entries that fail to load are excluded.

    Committee member lookups are memoised within this call so the same
    committee is only resolved once per round.
    """
    reqs_out:      list[dict] = []
    entries_out:   list[dict] = []
    members_cache: dict[str, list] = {}

    for entry in tqdm(entries, desc=desc, unit="meeting", leave=False):
        proto_path = Path(entry["proto"])
        try:
            meeting = load_meeting(proto_path)
        except Exception as e:
            tqdm.write(f"  [WARN] load failed {proto_path.name}: {e}")
            continue

        text   = build_transcript_text(meeting)
        chunks = chunk_transcript(text, max_chars=GEMINI_CHUNK_CHARS)

        chunk_idx = entry.get("chunk_index", 0)
        if chunk_idx >= len(chunks):
            tqdm.write(f"  [WARN] chunk_index {chunk_idx} ≥ {len(chunks)} for {proto_path.name}")
            continue

        partial_summ  = entry.get("partial_summary")
        system_prompt = SYSTEM_PROMPT_BATCH_PASS1 if partial_summ is None else SYSTEM_PROMPT_BATCH_CONTINUATION

        committee = entry["committee"]
        if committee not in members_cache:
            members_cache[committee] = get_active_committee_members_by_name(committee, knesset_num)
        members = members_cache[committee]

        raw_names  = extract_attendance(meeting)
        attendance = _build_attendance_block(raw_names, members, knesset_num)

        req = _build_request(
            system_prompt,
            entry["committee"], entry["date"], entry["meeting_id"],
            chunks[chunk_idx], chunk_idx + 1, entry["total_chunks"],
            partial_summ, attendance,
        )
        reqs_out.append(req)
        entries_out.append(entry)

    return reqs_out, entries_out


def _split_batches(
    reqs: list[dict],
    entries: list[dict],
) -> list[tuple[list[dict], list[dict]]]:
    """Partition into sub-batches each under MAX_BATCH_INPUT_TOKENS."""
    batches:     list[tuple] = []
    cur_reqs:    list[dict]  = []
    cur_entries: list[dict]  = []
    cur_tokens   = 0

    for req, entry in zip(reqs, entries):
        tok = _estimate_tokens(req)
        if tok > MAX_BATCH_INPUT_TOKENS:
            raise ValueError(
                f"Single request for meeting {entry.get('meeting_id')} estimated at "
                f"{tok:,} tokens — exceeds per-batch cap {MAX_BATCH_INPUT_TOKENS:,}. "
                f"Lower GEMINI_CHUNK_CHARS or skip this meeting."
            )
        if cur_reqs and (cur_tokens + tok > MAX_BATCH_INPUT_TOKENS or len(cur_reqs) == MAX_CONCURRENT_BATCH_REQUESTS):
            batches.append((cur_reqs, cur_entries))
            cur_reqs, cur_entries, cur_tokens = [], [], 0
        cur_reqs.append(req)
        cur_entries.append(entry)
        cur_tokens += tok

    if cur_reqs:
        batches.append((cur_reqs, cur_entries))

    return batches


# ── Result extraction ─────────────────────────────────────────────────────────

def _extract_text(response: dict) -> str | None:
    """Return generated text from a Gemini batch response dict, or None on error/empty."""
    if not response or "error" in response:
        return None
    candidates = response.get("candidates", [])
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p["text"] for p in parts if "text" in p).strip() or None


def _download_results(client: genai.Client, job) -> list[dict]:
    """
    Download and parse the output JSONL from a completed batch job.

    The output file reference lives on job.dest (google-genai SDK ≥ 1.x).
    Adjust the attribute name or download call if your SDK version differs.
    """
    dest = getattr(job, "dest", None)
    if dest is None:
        for attr in ("output_file", "output", "result_file", "response_file"):
            dest = getattr(job, attr, None)
            if dest is not None:
                break
    if dest is None:
        raise RuntimeError(
            f"Cannot locate output file on completed job {job.name}. "
            "Check the google-genai SDK version — batch result access API may differ."
        )

    dest_name = getattr(dest, "file_name", None) or str(dest)
    raw = client.files.download(file=dest_name)

    results = []
    for line in raw.decode("utf-8").splitlines():
        if line.strip():
            results.append(json.loads(line))
    return results


# ── Result processing ─────────────────────────────────────────────────────────

def _handle_terminal_failure(
    entry:       dict,
    state:       dict,
    is_multi:    bool,
    knesset_num: int,
    done_protos: set,
) -> None:
    """
    Give up on an entry after MAX_ENTRY_ATTEMPTS. For multi-chunk entries
    that already have a partial_summary, flush it to disk rather than
    discard hours of prior work.
    """
    proto_name = Path(entry["proto"]).name
    if is_multi and entry.get("partial_summary"):
        partial = _enrich_summary_attendance(entry["partial_summary"], knesset_num)
        summ_path = Path(entry["summ"])
        summ_path.parent.mkdir(parents=True, exist_ok=True)
        summ_path.write_text(partial, encoding="utf-8")
        state["stats"]["summarized"] += 1
        tqdm.write(f"    → {proto_name}: flushed partial summary ({len(partial)} chars) after {entry['attempts']} attempts")
    else:
        state["stats"]["failed"] += 1
        tqdm.write(f"    → {proto_name}: giving up after {entry['attempts']} attempts")
    done_protos.add(entry["proto"])


def _process_results(
    results:     list[dict],
    entries:     list[dict],
    state:       dict,
    is_multi:    bool,
    knesset_num: int,
) -> None:
    """
    Apply batch results to queue entries, updating state in-place.

    Results are correlated by `key` (Gemini batch output ordering is not
    guaranteed). Entries whose result errors *and* entries that receive
    no result at all both increment `attempts`; MAX_ENTRY_ATTEMPTS
    triggers a terminal drop (flushing partial summary for multi-chunk).
    """
    entries_by_key: dict[str, dict] = {_make_key(e): e for e in entries}
    unmatched_keys: set[str]        = set(entries_by_key.keys())
    done_protos:    set[str]        = set()
    queue_key = "multi_queue" if is_multi else "single_queue"

    for result in results:
        key   = result.get("key")
        entry = entries_by_key.get(key)
        if entry is None:
            tqdm.write(f"  [WARN] result with unknown key {key!r} — skipping")
            continue
        unmatched_keys.discard(key)

        proto      = entry["proto"]
        proto_path = Path(proto)
        summ_path  = Path(entry["summ"])
        response   = result.get("response") or {}
        text       = _extract_text(response)

        if text is None:
            err_src = result.get("error") or response.get("error") or {}
            err = err_src.get("message", "empty response") if isinstance(err_src, dict) else str(err_src)
            tqdm.write(f"  [ERROR] {proto_path.name}: {err}")
            entry["attempts"] = entry.get("attempts", 0) + 1
            if entry["attempts"] >= MAX_ENTRY_ATTEMPTS:
                _handle_terminal_failure(entry, state, is_multi, knesset_num, done_protos)
            continue

        if text.strip() == NOT_PROTOCOL:
            tqdm.write(f"  [not-protocol] {proto_path.name}")
            proto_path.unlink(missing_ok=True)
            state["stats"]["not_protocol"] += 1
            done_protos.add(proto)
            continue

        if not is_multi:
            text = _enrich_summary_attendance(text, knesset_num)
            summ_path.parent.mkdir(parents=True, exist_ok=True)
            summ_path.write_text(text, encoding="utf-8")
            state["stats"]["summarized"] += 1
            done_protos.add(proto)
        else:
            # entry is the same dict object that lives in state["multi_queue"],
            # so in-place mutation propagates directly to the queue.
            entry["partial_summary"]  = text
            entry["chunk_index"]     += 1
            entry["attempts"]         = 0  # reset on success

            if entry["chunk_index"] >= entry["total_chunks"]:
                text = _enrich_summary_attendance(text, knesset_num)
                summ_path.parent.mkdir(parents=True, exist_ok=True)
                summ_path.write_text(text, encoding="utf-8")
                state["stats"]["summarized"] += 1
                done_protos.add(proto)
            # NOTE: partial_summary stored WITHOUT enrichment intentionally —
            # enrichment runs only on the final completed summary, not mid-chain.

    # Entries that got no result this cycle — count as an attempt.
    for k in unmatched_keys:
        entry = entries_by_key[k]
        tqdm.write(f"  [no-result] {Path(entry['proto']).name} (key={k})")
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry["attempts"] >= MAX_ENTRY_ATTEMPTS:
            _handle_terminal_failure(entry, state, is_multi, knesset_num, done_protos)

    state[queue_key] = [e for e in state[queue_key] if e["proto"] not in done_protos]


# ── State persistence ─────────────────────────────────────────────────────────

def _save_state(state: dict, path: Path) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"[WARN] Corrupted state file {path} — starting fresh.")
        return None


# ── Concurrent pool primitives ────────────────────────────────────────────────

def _submit_no_poll(
    client:      genai.Client,
    reqs:        list[dict],
    entries:     list[dict],
    label:       str,
    tmp_dir:     Path,
    state:       dict,
    state_path:  Path,
    phase:       str,
    est_tokens:  int,
) -> dict:
    """
    Write JSONL → upload → submit (no poll). Adds the job to state["active_jobs"]
    and persists state. Returns the job's active-job info dict.

    Raises on submission failure. Caller should catch quota errors separately
    via `_is_quota_error`.
    """
    jsonl_path = tmp_dir / f"{label}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for entry, req in zip(entries, reqs):
            line = {"key": _make_key(entry), "request": req}
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    uploaded = client.files.upload(file=jsonl_path, config={"mime_type": "application/jsonl"})
    try:
        job = client.batches.create(
            model  = GEMINI_MODEL,
            src    = uploaded.name,
            config = {"display_name": label},
        )
    except Exception:
        # Submit failed — kill the orphan upload immediately.
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass
        raise

    info = {
        "job_name":      job.name,
        "uploaded_name": uploaded.name,
        "phase":         phase,
        "batch_label":   label,
        "est_tokens":    est_tokens,
        "entry_keys":    [_make_key(e) for e in entries],
    }
    state.setdefault("active_jobs", []).append(info)
    _save_state(state, state_path)
    return info


def _poll_and_drain(
    client:     genai.Client,
    active:     list[dict],
    state:      dict,
    state_path: Path,
) -> list[dict]:
    """
    Poll each job in `active`. For every one that reached a terminal state:
    download + apply results (or bump attempts on non-success), delete the
    uploaded input, remove from state["active_jobs"], and return its info.

    Callers remove drained infos from their own local `active` list and free
    the corresponding token budget.
    """
    terminal = ("SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "ERROR")
    drained: list[dict] = []

    for info in list(active):
        try:
            job = client.batches.get(name=info["job_name"])
        except Exception as e:
            tqdm.write(f"  [WARN] get job {info['batch_label']}: {e}")
            continue
        state_str = str(getattr(job, "state", "")).upper()
        if not any(s in state_str for s in terminal):
            continue

        knesset_num = state["knesset_num"]
        is_multi    = (info["phase"] == "p2")
        queue_key   = "multi_queue" if is_multi else "single_queue"
        queue       = state[queue_key]
        entries_by_key = {_make_key(e): e for e in queue}
        entries_in_batch = [entries_by_key[k] for k in info["entry_keys"] if k in entries_by_key]

        if any(s in state_str for s in ("SUCCEEDED", "COMPLETED")):
            try:
                results = _download_results(client, job)
            except Exception as e:
                tqdm.write(f"  [WARN] download {info['batch_label']}: {e} — leaving for retry")
                continue
            tqdm.write(f"  [drain] {info['batch_label']} SUCCEEDED  ({len(results)} results)")
            _process_results(results, entries_in_batch, state, is_multi=is_multi, knesset_num=knesset_num)
        else:
            tqdm.write(f"  [drain] {info['batch_label']} {state_str} — bumping attempts for {len(entries_in_batch)} entries")
            _process_results([], entries_in_batch, state, is_multi=is_multi, knesset_num=knesset_num)

        try:
            client.files.delete(name=info["uploaded_name"])
        except Exception:
            pass

        state["active_jobs"] = [j for j in state["active_jobs"] if j["job_name"] != info["job_name"]]
        _save_state(state, state_path)

        drained.append(info)

    return drained


def _run_pool(
    client:       genai.Client,
    sub_batches:  list[tuple[list[dict], list[dict], str]],
    phase:        str,
    tmp_dir:      Path,
    state:        dict,
    state_path:   Path,
    desc:         str,
) -> None:
    """
    Submit all `sub_batches` under an enqueued-token budget, then drain
    completions as they arrive. One-in-one-out: freed budget is refilled
    immediately from pending.

    On quota errors (RESOURCE_EXHAUSTED / QUOTA / 429) the submit loop
    pauses and waits for drain before trying again.
    """
    pending: list[tuple] = list(sub_batches)
    active:  list[dict]  = []
    budget_used = 0

    total_entries = sum(len(e) for _, e, _ in sub_batches)
    pbar = tqdm(
        total=total_entries,
        desc=desc,
        unit="req",
        dynamic_ncols=True,
    )

    while pending or active:
        # 1. Fill budget until full or we hit a quota error.
        submitted = 0
        while pending:
            reqs, entries, label = pending[0]
            est = sum(_estimate_tokens(r) for r in reqs)
            if active and (
                budget_used + est > ENQUEUE_CAP_TOKENS
                or len(active) + len(reqs) >= MAX_CONCURRENT_BATCH_REQUESTS
            ):
                if len(active) + len(reqs)>= MAX_CONCURRENT_BATCH_REQUESTS:
                    tqdm.write(
                        f"  [budget] pool full: {len(active)}/{MAX_CONCURRENT_BATCH_REQUESTS} jobs — draining"
                    )
                else:
                    tqdm.write(
                        f"  [budget] pool full: {budget_used/1_000_000:.1f}M used + "
                        f"{est/1_000_000:.1f}M next > {ENQUEUE_CAP_TOKENS/1_000_000:.0f}M cap — draining"
                    )
                break
            try:
                info = _submit_no_poll(
                    client, reqs, entries, label, tmp_dir, state, state_path, phase, est,
                )
            except Exception as e:
                if _is_quota_error(e):
                    tqdm.write(f"  [quota] server refused submit, waiting for drain: {e}")
                    break
                raise
            active.append(info)
            budget_used += est
            pending.pop(0)
            submitted += 1
            tqdm.write(
                f"  [submit] {label}  est={est/1_000_000:.2f}M tok  "
                f"pool={len(active)}  used={budget_used/1_000_000:.1f}/"
                f"{ENQUEUE_CAP_TOKENS/1_000_000:.0f}M  pending={len(pending)}"
            )

        # 2. Drain any completed jobs.
        drained = _poll_and_drain(client, active, state, state_path)
        for info in drained:
            active.remove(info)
            budget_used -= info["est_tokens"]
            pbar.update(len(info["entry_keys"]))

        # 3. Sleep if no progress this cycle.
        if not submitted and not drained and active:
            time.sleep(POLL_INTERVAL_S)

    pbar.close()


# ── Active-job resumption ─────────────────────────────────────────────────────

def _resume_active_jobs(client: genai.Client, state: dict, state_path: Path) -> None:
    """
    Drain every job listed in state["active_jobs"] (in-flight at last shutdown).
    Polls each to completion, downloads, processes results, cleans up.

    A job whose download fails is kept in active_jobs for retry on the next run.
    """
    jobs = list(state.get("active_jobs", []))
    if not jobs:
        return

    print(f"\nResuming {len(jobs)} active batch job(s) from prior run …")

    active = list(jobs)
    while active:
        drained = _poll_and_drain(client, active, state, state_path)
        for info in drained:
            active.remove(info)
        if not drained and active:
            tqdm.write(f"  [resume] {len(active)} job(s) still running, sleeping {POLL_INTERVAL_S}s …")
            time.sleep(POLL_INTERVAL_S)

    print("  Resume complete.\n")


# ── Scan phase ────────────────────────────────────────────────────────────────

def _scan_committees(
    knesset_num:     int,
    force_summarize: bool,
    skip_patterns:   list[str],
) -> dict:
    """
    Walk all committees. Download missing protocols.
    Categorise each unsummarised meeting into single_queue or multi_queue.
    Returns the initial state dict.
    """
    print(f"\n{'='*60}")
    print(f"Scan — Knesset {knesset_num}")
    print(f"{'='*60}")
    print(f"Fetching committee list …")
    committees = get_all_committees(knesset_num)
    if not committees:
        print("No committees found.")
        sys.exit(1)
    print(f"Found {len(committees)} committees.")

    if skip_patterns:
        before = len(committees)
        committees = [c for c in committees
                      if not any(p in c["Name"] for p in skip_patterns)]
        print(f"Skipping {before - len(committees)} committee(s) by name pattern.")

    single_queue: list[dict] = []
    multi_queue:  list[dict] = []
    cnt = {k: 0 for k in ("total", "classified", "cancelled",
                           "downloaded", "no_transcript", "already_done", "too_long")}

    for committee in tqdm(committees, desc="Scanning committees", unit="committee"):
        name         = committee["Name"]
        committee_id = committee["CommitteeID"]
        dirname      = _safe_dirname(name)
        proto_dir    = config.transcriptions_dir(knesset_num) / dirname
        summ_dir     = config.summaries_dir(knesset_num)      / dirname

        try:
            sessions = get_committee_sessions(committee_id, knesset_num)
        except Exception as e:
            tqdm.write(f"  [WARN] sessions fetch failed for {name}: {e}")
            continue
        if not sessions:
            continue

        proto_dir.mkdir(parents=True, exist_ok=True)
        summ_dir.mkdir(parents=True, exist_ok=True)

        for session in tqdm(sessions, desc=f"{name[:30]}", unit="session", leave=False):
            cnt["total"] += 1
            session_id = session["session_id"]
            date_iso   = session["date"]
            type_id    = session.get("type_id")
            status_id  = session.get("status_id")
            stem       = _session_filename(date_iso, session_id)
            proto_path = proto_dir / f"{stem}.json"
            summ_path  = summ_dir  / f"{stem}.txt"

            if type_id == SESSION_TYPE_CLASSIFIED:
                cnt["classified"] += 1
                continue
            if status_id in _CANCELLED_STATUS:
                cnt["cancelled"] += 1
                continue
            if summ_path.exists() and summ_path.stat().st_size > 0 and not force_summarize:
                cnt["already_done"] += 1
                continue

            if not proto_path.exists():
                try:
                    transcript = get_session_transcript(session_id)
                except Exception as e:
                    tqdm.write(f"  [WARN] transcript fetch failed for session {session_id}: {e}")
                    cnt["no_transcript"] += 1
                    continue
                if not transcript:
                    cnt["no_transcript"] += 1
                    continue
                payload = {
                    "meeting_id":  str(session_id),
                    "date":        date_iso,
                    "committee":   name,
                    "knesset_num": knesset_num,
                    **transcript,
                }
                proto_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                cnt["downloaded"] += 1

            try:
                meeting = load_meeting(proto_path)
                text    = build_transcript_text(meeting)
                chunks  = chunk_transcript(text, max_chars=GEMINI_CHUNK_CHARS)
            except Exception as e:
                tqdm.write(f"  [WARN] {proto_path.name}: {e}")
                continue

            if len(chunks) > MAX_SUMMARIZATION_CHUNKS:
                cnt["too_long"] += 1
                tqdm.write(f"  [skip-long] {proto_path.name} ({len(chunks)} chunks)")
                continue

            entry: dict = {
                "proto":        str(proto_path),
                "summ":         str(summ_path),
                "committee":    name,
                "date":         date_iso,
                "meeting_id":   str(session_id),
                "total_chunks": len(chunks),
                "attempts":     0,
            }

            if len(chunks) == 1:
                single_queue.append(entry)
            else:
                entry["chunk_index"]     = 0
                entry["partial_summary"] = None
                multi_queue.append(entry)

    print(f"\nScan complete:")
    print(f"  Total sessions    : {cnt['total']}")
    print(f"  Classified/cancel : {cnt['classified'] + cnt['cancelled']}")
    print(f"  Already done      : {cnt['already_done']}")
    print(f"  No transcript     : {cnt['no_transcript']}")
    print(f"  Too long (skip)   : {cnt['too_long']}")
    print(f"  Downloaded        : {cnt['downloaded']}")
    print(f"  → Single-chunk    : {len(single_queue)}")
    print(f"  → Multi-chunk     : {len(multi_queue)}")

    return {
        "knesset_num":   knesset_num,
        "scan_complete": True,
        "single_queue":  single_queue,
        "multi_queue":   multi_queue,
        "active_jobs":   [],
        "stats":         {"summarized": 0, "not_protocol": 0, "failed": 0},
    }


# ── Phase runners ─────────────────────────────────────────────────────────────

def _run_phase1(
    client:     genai.Client,
    state:      dict,
    state_path: Path,
    tmp_dir:    Path,
) -> None:
    knesset_num = state["knesset_num"]
    queue = state["single_queue"]

    print(f"\n{'='*60}")
    print(f"Phase 1 — {len(queue)} single-chunk meetings")
    print(f"{'='*60}")

    if not queue:
        print("  Nothing to process.")
        return

    all_reqs, all_entries = _build_requests_for_entries(queue, knesset_num, desc="P1 building")
    if not all_reqs:
        print("  No buildable requests.")
        return

    sub_batches_raw = _split_batches(all_reqs, all_entries)
    sub_batches = [
        (reqs, entries, f"knesset{knesset_num}-p1-{i+1:03d}")
        for i, (reqs, entries) in enumerate(sub_batches_raw)
    ]
    total_tok = sum(sum(_estimate_tokens(r) for r in reqs) for reqs, _, _ in sub_batches)
    print(f"  {len(all_reqs)} requests across {len(sub_batches)} sub-batch(es)  (~{total_tok/1_000_000:.1f}M tokens)")

    _run_pool(
        client, sub_batches,
        phase="p1",
        tmp_dir=tmp_dir,
        state=state,
        state_path=state_path,
        desc="Phase 1",
    )

    s = state["stats"]
    print(f"\nPhase 1 done — summarized={s['summarized']}  not_proto={s['not_protocol']}  failed={s['failed']}")


def _run_phase2(
    client:     genai.Client,
    state:      dict,
    state_path: Path,
    tmp_dir:    Path,
) -> None:
    knesset_num = state["knesset_num"]

    print(f"\n{'='*60}")
    print(f"Phase 2 — {len(state['multi_queue'])} multi-chunk meetings")
    print(f"{'='*60}")

    if not state["multi_queue"]:
        print("  Nothing to process.")
        return

    round_num = 0
    while state["multi_queue"]:
        round_num += 1
        pending = list(state["multi_queue"])
        print(f"\n  ── Round {round_num} ── {len(pending)} meeting(s) pending")

        all_reqs, all_entries = _build_requests_for_entries(
            pending, knesset_num, desc=f"P2 r{round_num:02d} building"
        )
        if not all_reqs:
            print("  No buildable requests — clearing queue.")
            state["multi_queue"] = []
            break

        sub_batches_raw = _split_batches(all_reqs, all_entries)
        sub_batches = [
            (reqs, entries, f"knesset{knesset_num}-p2-r{round_num:02d}-{i+1:03d}")
            for i, (reqs, entries) in enumerate(sub_batches_raw)
        ]
        total_tok = sum(sum(_estimate_tokens(r) for r in reqs) for reqs, _, _ in sub_batches)
        print(f"  {len(all_reqs)} requests across {len(sub_batches)} sub-batch(es)  (~{total_tok/1_000_000:.1f}M tokens)")

        _run_pool(
            client, sub_batches,
            phase="p2",
            tmp_dir=tmp_dir,
            state=state,
            state_path=state_path,
            desc=f"Phase 2 r{round_num:02d}",
        )

        s = state["stats"]
        print(f"  Round {round_num} done — remaining={len(state['multi_queue'])}  "
              f"summarized={s['summarized']}  not_proto={s['not_protocol']}  failed={s['failed']}")

    print(f"\nPhase 2 done.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--knesset",         type=int,  default=25)
    ap.add_argument("--state-file",      type=Path, default=None,
                    help="Resume/save state here (default: batch_state_k<N>.json in cwd)")
    ap.add_argument("--tmp-dir",         type=Path, default=None,
                    help="Directory for temporary JSONL upload files")
    ap.add_argument("--force-summarize", action="store_true",
                    help="Re-summarize even if a summary already exists")
    ap.add_argument("--skip",            nargs="*", default=[],
                    help="Committee name substrings to skip")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable not set.")
        sys.exit(1)

    client     = genai.Client(api_key=api_key)
    state_path = args.state_file or Path(f"batch_state_k{args.knesset}.json")
    tmp_dir    = args.tmp_dir or Path(tempfile.mkdtemp(prefix="knesset_batch_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)

    skip_patterns = [s.strip() for s in (args.skip or []) if s.strip()]

    state = _load_state(state_path)
    if state is None or not state.get("scan_complete"):
        state = _scan_committees(args.knesset, args.force_summarize, skip_patterns)
        _save_state(state, state_path)
        print(f"State saved: {state_path}")
    else:
        # Legacy schema migration: old files had singular "active_job".
        legacy = state.pop("active_job", None)
        if legacy:
            print(f"[WARN] legacy active_job field found — dropping (cannot safely migrate without entry_keys):")
            print(f"       {legacy}")
        state.setdefault("active_jobs", [])
        print(f"\n{'='*60}")
        print(f"Resuming — Knesset {state.get('knesset_num', args.knesset)}")
        print(f"{'='*60}")
        print(f"  State file             : {state_path}")
        print(f"  Single-chunk remaining : {len(state['single_queue'])}")
        print(f"  Multi-chunk  remaining : {len(state['multi_queue'])}")
        print(f"  Active jobs (in-flight): {len(state['active_jobs'])}")
        print(f"  Already summarized     : {state['stats']['summarized']}")

    # Reconnect to any jobs that were mid-flight at last shutdown.
    _resume_active_jobs(client, state, state_path)

    _run_phase1(client, state, state_path, tmp_dir)
    _run_phase2(client, state, state_path, tmp_dir)

    s = state["stats"]
    print(f"\n{'='*60}")
    print(f"DONE — Knesset {args.knesset}")
    print(f"{'='*60}")
    print(f"  Summarized     : {s['summarized']}")
    print(f"  Not protocol   : {s['not_protocol']}")
    print(f"  Failed         : {s['failed']}")
    print(f"  State file     : {state_path}")


if __name__ == "__main__":
    main()
