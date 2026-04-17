"""
summarize_knesset_batches.py

Batch summarization for a Knesset using Google's Gemini Batch API (gemma-4-31b-it).

Phase 1: All single-chunk transcripts — one or more batch jobs.
Phase 2: Multi-chunk transcripts — one batch job per round; each round advances
         every pending meeting by one chunk using the running partial summary.

State is saved to a JSON file after every batch so runs can be safely interrupted
and resumed. If interrupted mid-poll, the active job is reconnected on resume
(no re-submission / double billing).

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

GEMMA_MODEL                    = "gemma-4-31b-it"
GEMMA_CTX_TOKENS               = 30_000                            # input tokens per request
GEMMA_CHUNK_CHARS              = GEMMA_CTX_TOKENS * CHARS_PER_TOK  # 60 000 chars
MAX_BATCH_INPUT_TOKENS         = 9_500_000                         # stay under 10M API limit
BATCH_METADATA_OVERHEAD_TOKENS = 100                               # per-line JSONL framing
POLL_INTERVAL_S                = 60
MAX_POLL_ATTEMPTS              = 180     # 3 hours max
MAX_ENTRY_ATTEMPTS             = 3       # per-entry retry budget before giving up


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

    for entry in entries:
        proto_path = Path(entry["proto"])
        try:
            meeting = load_meeting(proto_path)
        except Exception as e:
            tqdm.write(f"  [WARN] load failed {proto_path.name}: {e}")
            continue

        text   = build_transcript_text(meeting)
        chunks = chunk_transcript(text, max_chars=GEMMA_CHUNK_CHARS)

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
                f"Lower GEMMA_CHUNK_CHARS or skip this meeting."
            )
        if cur_reqs and cur_tokens + tok > MAX_BATCH_INPUT_TOKENS:
            batches.append((cur_reqs, cur_entries))
            cur_reqs, cur_entries, cur_tokens = [], [], 0
        cur_reqs.append(req)
        cur_entries.append(entry)
        cur_tokens += tok

    if cur_reqs:
        batches.append((cur_reqs, cur_entries))

    return batches


# ── Batch submission + result download ───────────────────────────────────────

def _poll_to_completion(client: genai.Client, job):
    """Poll `job` until a terminal state. Returns refreshed job object."""
    terminal = ("SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "ERROR")
    for attempt in range(1, MAX_POLL_ATTEMPTS + 1):
        time.sleep(POLL_INTERVAL_S)
        job   = client.batches.get(name=job.name)
        state = str(getattr(job, "state", "")).upper()
        print(f"  [{attempt:3d}/{MAX_POLL_ATTEMPTS}] {state}                    ", end="\r", flush=True)

        if any(s in state for s in terminal):
            print()
            return job

    raise TimeoutError(
        f"Batch job {job.name} did not complete within "
        f"{MAX_POLL_ATTEMPTS * POLL_INTERVAL_S // 60} minutes"
    )


def _submit_batch(
    client:     genai.Client,
    reqs:       list[dict],
    entries:    list[dict],
    label:      str,
    tmp_dir:    Path,
    state:      dict,
    state_path: Path,
    phase:      str,
) -> list[dict]:
    """
    Write JSONL → upload → submit → poll → download → return result dicts.

    Each JSONL line is `{"key": <make_key(entry)>, "request": <req>}`; the
    `key` field is echoed back on each result line so we can correlate by
    key rather than relying on output order (which Gemini does not guarantee).

    Active job is persisted to state *before* polling begins. If the run is
    interrupted, the next startup reconnects to the same job via
    `_resume_active_job` — no duplicate submission.
    """
    jsonl_path = tmp_dir / f"{label}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for entry, req in zip(entries, reqs):
            line = {"key": _make_key(entry), "request": req}
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    est = sum(_estimate_tokens(r) for r in reqs)
    print(f"  Uploading {len(reqs)} requests (~{est // 1_000}K input tokens) …")

    uploaded = client.files.upload(file=jsonl_path, config={"mime_type": "application/jsonl"})
    print(f"  File API: {uploaded.name}")

    try:
        job = client.batches.create(
            model  = GEMMA_MODEL,
            src    = uploaded.name,
            config = {"display_name": label},
        )
        print(f"  Batch job: {job.name}  (polling every {POLL_INTERVAL_S}s)")

        # Persist BEFORE polling so an interrupt lets us reconnect.
        state["active_job"] = {
            "job_name":      job.name,
            "uploaded_name": uploaded.name,
            "phase":         phase,
            "batch_label":   label,
        }
        _save_state(state, state_path)

        job       = _poll_to_completion(client, job)
        state_str = str(getattr(job, "state", "")).upper()
        if not any(s in state_str for s in ("SUCCEEDED", "COMPLETED")):
            raise RuntimeError(f"Batch job {job.name} ended in state: {state_str}")

        print("  Downloading results …")
        results = _download_results(client, job)
        print(f"  Received {len(results)} result lines.")
        return results
    finally:
        # Always clean up the uploaded input JSONL — job references its own
        # output on the server, so the input file is no longer needed.
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass
        # active_job is cleared by the caller AFTER _process_results succeeds,
        # so that an exception here leaves active_job set on disk for resume.


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


def _extract_text(response: dict) -> str | None:
    """Return generated text from a Gemini batch response dict, or None on error/empty."""
    if not response or "error" in response:
        return None
    candidates = response.get("candidates", [])
    if not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p["text"] for p in parts if "text" in p).strip() or None


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
        print(f"    → {proto_name}: flushed partial summary ({len(partial)} chars) after {entry['attempts']} attempts")
    else:
        state["stats"]["failed"] += 1
        print(f"    → {proto_name}: giving up after {entry['attempts']} attempts")
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

    Single-chunk: enrich attendance → save summary → mark done.
    Multi-chunk:  update partial_summary + advance chunk_index;
                  enrich + save when final chunk reached → mark done.
    NOT_PROTOCOL: delete transcript → mark done.
    """
    entries_by_key: dict[str, dict] = {_make_key(e): e for e in entries}
    unmatched_keys: set[str]        = set(entries_by_key.keys())
    done_protos:    set[str]        = set()
    queue_key = "multi_queue" if is_multi else "single_queue"

    for result in results:
        key   = result.get("key")
        entry = entries_by_key.get(key)
        if entry is None:
            print(f"  [WARN] result with unknown key {key!r} — skipping")
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
            print(f"  [ERROR] {proto_path.name}: {err}")
            entry["attempts"] = entry.get("attempts", 0) + 1
            if entry["attempts"] >= MAX_ENTRY_ATTEMPTS:
                _handle_terminal_failure(entry, state, is_multi, knesset_num, done_protos)
            continue

        if text.strip() == NOT_PROTOCOL:
            print(f"  [not-protocol] {proto_path.name}")
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

    # Entries that got no result this round — count as an attempt.
    for k in unmatched_keys:
        entry = entries_by_key[k]
        print(f"  [no-result] {Path(entry['proto']).name} (key={k})")
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


# ── Active-job resumption ─────────────────────────────────────────────────────

def _resume_active_job(client: genai.Client, state: dict, state_path: Path) -> None:
    """
    If a batch job was mid-flight at last shutdown, reconnect to it:
    poll to completion (if still running), download + process results,
    then clear active_job. Prevents duplicate submissions / double billing.

    If download fails we leave active_job set on disk so the next run
    retries the download rather than re-submitting.
    """
    aj = state.get("active_job")
    if not aj:
        return

    job_name      = aj["job_name"]
    uploaded_name = aj.get("uploaded_name")
    phase         = aj.get("phase", "p1")
    label         = aj.get("batch_label", job_name)

    print(f"\nResuming active batch job: {label} ({job_name})")

    try:
        job = client.batches.get(name=job_name)
    except Exception as e:
        print(f"  [WARN] Failed to fetch active job: {e}. Clearing.")
        state["active_job"] = None
        _save_state(state, state_path)
        return

    state_str = str(getattr(job, "state", "")).upper()
    terminal  = ("SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "ERROR")
    if not any(s in state_str for s in terminal):
        job       = _poll_to_completion(client, job)
        state_str = str(getattr(job, "state", "")).upper()

    if any(s in state_str for s in ("SUCCEEDED", "COMPLETED")):
        try:
            results = _download_results(client, job)
        except Exception as e:
            print(f"  [WARN] download failed: {e}. Leaving active_job set for retry.")
            return
        print(f"  Resumed job returned {len(results)} result lines.")

        is_multi = (phase == "p2")
        queue    = state["multi_queue" if is_multi else "single_queue"]
        keyset   = {_make_key(e) for e in queue}
        matched  = [r for r in results if r.get("key") in keyset]
        if len(matched) < len(results):
            print(f"  [WARN] {len(results) - len(matched)} result(s) had keys not in current queue — ignored.")
        _process_results(matched, queue, state, is_multi=is_multi, knesset_num=state["knesset_num"])
    else:
        print(f"  Active job ended in state {state_str}; nothing to apply.")

    if uploaded_name:
        try:
            client.files.delete(name=uploaded_name)
        except Exception:
            pass
    state["active_job"] = None
    _save_state(state, state_path)


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
    print(f"Fetching committee list for Knesset {knesset_num} …")
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

    for committee in tqdm(committees, desc="Scanning committees", unit="committee", total=len(committees)):
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

        for session in tqdm(sessions, desc="Scanning sessions", unit="session", total=len(sessions), leave=False):
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

            # Download protocol if missing
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

            # Determine chunk count (using batch chunk size, not default)
            try:
                meeting = load_meeting(proto_path)
                text    = build_transcript_text(meeting)
                chunks  = chunk_transcript(text, max_chars=GEMMA_CHUNK_CHARS)
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
        "active_job":    None,
        "stats":         {"summarized": 0, "not_protocol": 0, "failed": 0},
    }


# ── Phase runners ─────────────────────────────────────────────────────────────

def _run_phase1(
    client:        genai.Client,
    state:         dict,
    state_path:    Path,
    tmp_dir:       Path,
    batch_counter: list[int],
) -> None:
    knesset_num = state["knesset_num"]
    queue = state["single_queue"]
    if not queue:
        print("\nPhase 1: nothing to process.")
        return

    print(f"\n{'='*60}")
    print(f"Phase 1 — {len(queue)} single-chunk meetings")
    print(f"{'='*60}")

    all_reqs, all_entries = _build_requests_for_entries(queue, knesset_num)
    if not all_reqs:
        print("  No buildable requests.")
        return

    sub_batches = _split_batches(all_reqs, all_entries)
    print(f"  → {len(sub_batches)} batch job(s)")

    for reqs, entries in sub_batches:
        batch_counter[0] += 1
        label = f"knesset{knesset_num}-p1-{batch_counter[0]:03d}"
        print(f"\n  [{label}]  {len(reqs)} requests")

        results = _submit_batch(client, reqs, entries, label, tmp_dir, state, state_path, phase="p1")
        _process_results(results, entries, state, is_multi=False, knesset_num=knesset_num)

        state["active_job"] = None
        _save_state(state, state_path)
        s = state["stats"]
        print(f"  summarized={s['summarized']}  not_proto={s['not_protocol']}  failed={s['failed']}")

    print("\nPhase 1 done.")


def _run_phase2(
    client:        genai.Client,
    state:         dict,
    state_path:    Path,
    tmp_dir:       Path,
    batch_counter: list[int],
) -> None:
    knesset_num = state["knesset_num"]
    if not state["multi_queue"]:
        print("\nPhase 2: nothing to process.")
        return

    print(f"\n{'='*60}")
    print(f"Phase 2 — {len(state['multi_queue'])} multi-chunk meetings")
    print(f"{'='*60}")

    round_num = 0
    while state["multi_queue"]:
        round_num += 1
        # Snapshot for this round — list copy but same dict objects,
        # so in-place mutations in _process_results propagate to state["multi_queue"].
        pending = list(state["multi_queue"])
        print(f"\n  Round {round_num}: {len(pending)} meetings pending")

        all_reqs, all_entries = _build_requests_for_entries(pending, knesset_num)
        if not all_reqs:
            print("  No buildable requests — clearing queue.")
            state["multi_queue"] = []
            break

        sub_batches = _split_batches(all_reqs, all_entries)
        print(f"  → {len(sub_batches)} batch job(s) this round")

        for reqs, entries in sub_batches:
            batch_counter[0] += 1
            label = f"knesset{knesset_num}-p2-r{round_num:02d}-{batch_counter[0]:03d}"
            print(f"\n  [{label}]  {len(reqs)} requests")

            results = _submit_batch(client, reqs, entries, label, tmp_dir, state, state_path, phase="p2")
            _process_results(results, entries, state, is_multi=True, knesset_num=knesset_num)

            state["active_job"] = None
            _save_state(state, state_path)
            s = state["stats"]
            print(f"  summarized={s['summarized']}  not_proto={s['not_protocol']}  failed={s['failed']}")

        print(f"  Round {round_num} done.  Remaining: {len(state['multi_queue'])}")

    print("\nPhase 2 done.")


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
        print(f"State saved: {state_path}\n")
    else:
        state.setdefault("active_job", None)
        print(f"Resuming from {state_path}:")
        print(f"  Single-chunk remaining : {len(state['single_queue'])}")
        print(f"  Multi-chunk  remaining : {len(state['multi_queue'])}")
        print(f"  Already summarized     : {state['stats']['summarized']}\n")

    # Reconnect to a job that was mid-flight at last shutdown.
    _resume_active_job(client, state, state_path)

    batch_counter = [0]

    _run_phase1(client, state, state_path, tmp_dir, batch_counter)
    _run_phase2(client, state, state_path, tmp_dir, batch_counter)

    s = state["stats"]
    print(f"\n{'='*60}")
    print(f"DONE — Knesset {args.knesset}")
    print(f"{'='*60}")
    print(f"  Summarized     : {s['summarized']}")
    print(f"  Not protocol   : {s['not_protocol']}")
    print(f"  Failed         : {s['failed']}")
    print(f"State file       : {state_path}")


if __name__ == "__main__":
    main()
