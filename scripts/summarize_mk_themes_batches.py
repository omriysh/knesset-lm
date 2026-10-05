"""
summarize_mk_themes_batches.py

Per-MK themes for a Knesset using Google's Gemini Batch API: one request per MK holding every opinion of
that MK in Data/knesset.db (prompt and request layout: summarization/prompts.py SYSTEM_PROMPT_MK_THEMES,
summarization/mk_themes.py). Each answer is written to Data/mk_themes/<knesset>/<mk_id>.json:

    {"mk_id", "knesset_num", "mk_name", "model", "thinking_level", "generated_at", "opinion_count",
     "themes": [{"rank", "title", "summary", "model_rank", "persistence_rank",
                 "opinions": [[meeting_id, idx], ...]}, ...]}

`build_knesset_db.py --target mk_themes` loads these files into the mk_themes / mk_theme_opinions tables.

An MK is summarized when they have no theme file yet or their opinion count in knesset.db changed since
their file was written (--force: every MK). Reads knesset.db, so run it after the summaries are in the db.
State is saved to Data/summary_batches/mk_themes_state_k<N>.json after every batch; in-flight jobs are
reconnected on resume instead of resubmitted.

Usage
-----
    cd knesset-lm
    python scripts/summarize_mk_themes_batches.py --knesset 25 --dry-run      # cost estimate, submits nothing
    python scripts/summarize_mk_themes_batches.py --knesset 25
    python scripts/summarize_mk_themes_batches.py --knesset 25 --mk-id 30807 --mk-id 30480 --force
    # scripts/process_knesset.py runs this step for you

Exits 1 when an MK failed every attempt.
"""

import argparse
import json
import os
import sqlite3
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
from summarization import mk_themes
from summarization.gemini_batch import (estimate_request_tokens, extract_text, load_state, resume_active_jobs,
                                        run_pool, save_state, split_batches, write_jsonl)
from summarization.prompts import MK_THEMES_RESPONSE_SCHEMA, SYSTEM_PROMPT_MK_THEMES

MAX_BATCH_INPUT_TOKENS = 10_000_000
MAX_REQUESTS_PER_BATCH = 500
MAX_CONCURRENT_JOBS    = 100
ENQUEUE_CAP_TOKENS     = 380_000_000
POLL_INTERVAL_S        = 60
MAX_ATTEMPTS           = 3
STATE_VERSION          = 1
TRUNCATED_FINISH_REASON = "MAX_TOKENS"

BATCH_PRICE_PER_M = {"gemini-3.8-flash": (0.375, 1.875)}   # dry-run estimate only
ESTIMATED_OUTPUT_TOKENS_PER_OPINION = 8                      # answer + thinking, from the Exp9 runs

RUN_COUNTERS = ("summarized", "failed")


def _open_db_readonly() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(config.KNESSET_DB).as_posix()}?mode=ro", uri=True, timeout=30)


def _theme_path(knesset_num: int, mk_id: str) -> Path:
    return config.mk_themes_dir(knesset_num) / f"{mk_id}.json"


def _stored_opinion_count(theme_path: Path) -> int | None:
    if not theme_path.exists():
        return None
    try:
        return json.loads(theme_path.read_text(encoding="utf-8")).get("opinion_count")
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  [scan] unreadable {theme_path.name}, summarizing again: {exc}")
        return None


def _scan_stale_mks(conn, knesset_num: int, force: bool, only_mk_ids: list[str]) -> tuple[list[str], int]:
    """(MKs to summarize, MKs whose theme file is up to date)."""
    counts = mk_themes.opinion_counts_by_mk(conn, knesset_num)
    if only_mk_ids:
        missing = [mk_id for mk_id in only_mk_ids if mk_id not in counts]
        if missing:
            print(f"  [scan] no opinions for MK id(s) {missing}")
        counts = {mk_id: n for mk_id, n in counts.items() if mk_id in only_mk_ids}
    stale = [mk_id for mk_id, n in sorted(counts.items())
             if force or _stored_opinion_count(_theme_path(knesset_num, mk_id)) != n]
    return stale, len(counts) - len(stale)


def _build_request(user_text: str, settings: dict) -> dict:
    generation_config = {
        "maxOutputTokens":  config.MK_THEMES_MAX_OUTPUT_TOKENS,
        "responseMimeType": "application/json",
        "responseSchema":   MK_THEMES_RESPONSE_SCHEMA,
    }
    if settings["thinking_level"] != "none":
        generation_config["thinkingConfig"] = {"thinkingLevel": settings["thinking_level"]}
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT_MK_THEMES}]},
        "contents":          [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig":  generation_config,
    }


def _build_requests(conn, state: dict) -> list[tuple[dict, str]]:
    """One request per queued MK, keyed by mk_id. Each entry keeps the (meeting_id, idx, date) of every local id
    it was sent with, so its answer resolves even if knesset.db changes while the job runs."""
    items = []
    for entry in tqdm(state["queue"], desc="Building requests", unit="MK", leave=False):
        entry.pop("opinions", None)
        opinions = mk_themes.load_mk_opinions(conn, entry["mk_id"], state["knesset_num"])
        if not opinions:
            print(f"  [build] {entry['mk_id']} has no opinions anymore, skipping")
            continue
        entry["mk_name"] = mk_themes.load_mk_name(conn, entry["mk_id"], state["knesset_num"])
        entry["opinions"] = [[o["meeting_id"], o["idx"], o["date"]] for o in opinions]
        items.append((_build_request(mk_themes.build_user_text(entry["mk_name"], opinions), state), entry["mk_id"]))
    return items


def _opinions_of_entry(entry: dict) -> list[dict]:
    return [{"local_id": local_id, "meeting_id": meeting_id, "idx": idx, "date": date}
            for local_id, (meeting_id, idx, date) in enumerate(entry["opinions"], start=1)]


def _write_themes(entry: dict, answer_text: str, state: dict) -> None:
    """Raises ValueError on an answer that is not the themes JSON."""
    themes, unknown_ids = mk_themes.parse_themes(answer_text, _opinions_of_entry(entry))
    if unknown_ids:
        tqdm.write(f"  [themes] {entry['mk_id']}: dropped {len(unknown_ids)} ids that were not in the request")
    payload = mk_themes.themes_file_payload(entry["mk_id"], state["knesset_num"], entry["mk_name"], state["model"],
                                            state["thinking_level"], len(entry["opinions"]),
                                            mk_themes.order_themes(themes))
    theme_path = _theme_path(state["knesset_num"], entry["mk_id"])
    theme_path.parent.mkdir(parents=True, exist_ok=True)
    theme_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _process_results(results: list[dict], keys_in_batch: list[str], state: dict) -> None:
    """Write a theme file per good answer. An error, a cut-off or unparsable answer, or a missing result
    counts as an attempt; MAX_ATTEMPTS drops the MK from the queue (no file, so a later run retries)."""
    entries_by_mk = {entry["mk_id"]: entry for entry in state["queue"]}
    pending = set(keys_in_batch)
    done_mk_ids: set[str] = set()

    def fail(entry: dict, reason: str) -> None:
        entry["attempts"] += 1
        tqdm.write(f"  [ERROR] {entry['mk_id']}: {reason}  attempt {entry['attempts']}/{MAX_ATTEMPTS}")
        if entry["attempts"] >= MAX_ATTEMPTS:
            state["stats"]["failed"] += 1
            done_mk_ids.add(entry["mk_id"])

    for result in results:
        mk_id = result.get("key")
        entry = entries_by_mk.get(mk_id)
        if entry is None or mk_id not in pending:
            tqdm.write(f"  [WARN] result with unknown key {mk_id!r}, skipping")
            continue
        pending.discard(mk_id)
        response = result.get("response") or {}
        text, finish_reason = extract_text(response)
        if finish_reason == TRUNCATED_FINISH_REASON:
            fail(entry, f"answer cut off at the output cap ({config.MK_THEMES_MAX_OUTPUT_TOKENS:,} tokens)")
            continue
        if not text:
            error = result.get("error") or response.get("error") or {}
            fail(entry, error.get("message", "empty response") if isinstance(error, dict) else str(error))
            continue
        try:
            _write_themes(entry, text, state)
        except ValueError as exc:
            fail(entry, str(exc))
            continue
        state["stats"]["summarized"] += 1
        done_mk_ids.add(mk_id)

    for mk_id in pending:
        if mk_id in entries_by_mk and mk_id not in done_mk_ids:
            fail(entries_by_mk[mk_id], "no result in batch output")

    state["queue"] = [entry for entry in state["queue"] if entry["mk_id"] not in done_mk_ids]


def _sub_batches(items: list[tuple[dict, str]], state: dict, tag: str) -> list[tuple[list, str]]:
    return [(batch, f"mk-themes-k{state['knesset_num']}-{tag}-{i + 1:03d}")
            for i, batch in enumerate(split_batches(items, MAX_BATCH_INPUT_TOKENS, MAX_REQUESTS_PER_BATCH))]


def _run(client, conn, state: dict, state_path: Path, tmp_dir: Path) -> None:
    round_num = 0
    while state["queue"]:
        round_num += 1
        print(f"\nRound {round_num}: {len(state['queue'])} MK(s)")
        items = _build_requests(conn, state)
        if not items:
            state["queue"] = []
            break
        state["queue"] = [entry for entry in state["queue"] if "opinions" in entry]
        save_state(state, state_path)
        run_pool(client, state["model"], _sub_batches(items, state, f"r{round_num:02d}"), tmp_dir, state,
                 state_path, f"Round {round_num}", _process_results, enqueue_cap=ENQUEUE_CAP_TOKENS,
                 max_concurrent_jobs=MAX_CONCURRENT_JOBS, poll_interval_s=POLL_INTERVAL_S)


def _dry_run(conn, state: dict, tmp_dir: Path) -> None:
    items = _build_requests(conn, state)
    total_in = sum(estimate_request_tokens(request) for request, _ in items)
    total_out = sum(len(entry["opinions"]) * ESTIMATED_OUTPUT_TOKENS_PER_OPINION
                    for entry in state["queue"] if "opinions" in entry)
    for batch, label in _sub_batches(items, state, "dry"):
        path = write_jsonl(batch, label, tmp_dir)
        print(f"  wrote {path.name}: {len(batch)} requests, {path.stat().st_size // 1024} KB")
    if items:
        largest_request, largest_mk = max(items, key=lambda item: estimate_request_tokens(item[0]))
        print(f"  Largest request  : MK {largest_mk}, ~{estimate_request_tokens(largest_request):,} tokens")
    print(f"  Model            : {state['model']} (thinking={state['thinking_level']})")
    print(f"  Est. input tokens: {total_in / 1e6:.1f}M   output: {total_out / 1e6:.2f}M")
    price = BATCH_PRICE_PER_M.get(state["model"])
    if price:
        print(f"  Est. batch cost  : ${total_in / 1e6 * price[0] + total_out / 1e6 * price[1]:,.2f}")
    print(f"  JSONL files      : {tmp_dir}")


def _usable_state(state: dict | None, knesset_num: int, state_path: Path) -> dict | None:
    if state is None:
        return None
    if state.get("version") != STATE_VERSION or state.get("knesset_num") != knesset_num:
        if state.get("active_jobs"):
            print(f"[WARN] {state_path} lists {len(state['active_jobs'])} in-flight job(s) of another version or "
                  f"Knesset; they are NOT resumed. Cancel them in AI Studio if they are not needed.")
        return None
    return state


def summarize_mk_themes(knesset_num: int, *, force: bool = False, mk_ids: list[str] = (), dry_run: bool = False,
                        state_path: Path | None = None, tmp_dir: Path | None = None,
                        model: str = config.MK_THEMES_MODEL,
                        thinking_level: str = config.MK_THEMES_THINKING_LEVEL) -> dict:
    """
    Summarize every MK whose theme file is missing or out of date. Returns this run's counters
    (RUN_COUNTERS plus "up_to_date" and "queued"). Raises RuntimeError when no Gemini API key is set.
    """
    state_path = state_path or config.mk_themes_batch_state_path(knesset_num)
    tmp_dir = tmp_dir or Path(tempfile.mkdtemp(prefix="mk_themes_batch_"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    print(f"State file       : {state_path}")
    print(f"JSONL debug files: {tmp_dir}")
    if not Path(config.KNESSET_DB).exists():
        raise RuntimeError(f"{config.KNESSET_DB} does not exist; build it first")

    state = _usable_state(load_state(state_path), knesset_num, state_path) or {
        "version": STATE_VERSION, "knesset_num": knesset_num, "queue": [], "active_jobs": []}
    state.update(model=model, thinking_level=thinking_level, stats=dict.fromkeys(RUN_COUNTERS, 0))

    client = None
    if not dry_run:
        api_key = os.environ.get(config.GOOGLE_API_KEY_ENV) or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(f"set {config.GOOGLE_API_KEY_ENV} or GEMINI_API_KEY to summarize MK themes")
        client = genai.Client(api_key=api_key)
        resume_active_jobs(client, state, state_path, _process_results, POLL_INTERVAL_S)

    conn = _open_db_readonly()
    try:
        stale_mk_ids, up_to_date = _scan_stale_mks(conn, knesset_num, force, list(mk_ids))
        attempts_so_far = {entry["mk_id"]: entry["attempts"] for entry in state["queue"]}
        state["queue"] = [{"mk_id": mk_id, "attempts": attempts_so_far.get(mk_id, 0)} for mk_id in stale_mk_ids]
        print(f"  {len(stale_mk_ids)} MK(s) to summarize, {up_to_date} up to date")
        if dry_run:
            _dry_run(conn, state, tmp_dir)
        else:
            save_state(state, state_path)
            _run(client, conn, state, state_path, tmp_dir)
            save_state(state, state_path)
    finally:
        conn.close()
    return {**state["stats"], "up_to_date": up_to_date, "queued": len(state["queue"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--knesset",    type=int, default=25)
    ap.add_argument("--mk-id",      action="append", default=[], help="Only this MK (repeatable)")
    ap.add_argument("--force",      action="store_true", help="Summarize even MKs whose theme file is up to date")
    ap.add_argument("--model",      default=config.MK_THEMES_MODEL)
    ap.add_argument("--thinking-level", default=config.MK_THEMES_THINKING_LEVEL,
                    choices=["none", "minimal", "low", "medium", "high"])
    ap.add_argument("--state-file", type=Path, default=None,
                    help="Resume/save state here (default: Data/summary_batches/mk_themes_state_k<N>.json)")
    ap.add_argument("--tmp-dir",    type=Path, default=None, help="Directory for temporary JSONL upload files")
    ap.add_argument("--dry-run",    action="store_true", help="Build the requests and print a cost estimate; submit nothing")
    args = ap.parse_args()

    try:
        stats = summarize_mk_themes(args.knesset, force=args.force, mk_ids=args.mk_id, dry_run=args.dry_run,
                                    state_path=args.state_file, tmp_dir=args.tmp_dir, model=args.model,
                                    thinking_level=args.thinking_level)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"\nDONE: Knesset {args.knesset}")
    for key, value in stats.items():
        print(f"  {key:<12}: {value}")
    if stats["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
