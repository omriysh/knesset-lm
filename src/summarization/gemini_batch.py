"""
Gemini Batch API plumbing shared by the batch scripts: request token estimates, sub-batch splits,
JSONL upload and submission, a pool that keeps jobs under the enqueued-token budget, draining
finished jobs, and resuming jobs that were in flight when a previous run stopped.

Results are correlated by request key (batch output order is not guaranteed). The caller supplies
process_results(results, keys_in_batch, state): results is the parsed output JSONL of a finished
job ([] for a failed job), keys_in_batch every key that job carried.
State is a JSON-serializable dict saved after every submission and drain; in-flight jobs live in
state["active_jobs"].
"""

import json
import time
from pathlib import Path
from typing import Callable

from google import genai
from tqdm import tqdm

from config import CHARS_PER_TOK

BATCH_METADATA_OVERHEAD_TOKENS = 100   # per-line JSONL framing
TERMINAL_JOB_STATES  = ("SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "ERROR", "EXPIRED")
SUCCEEDED_JOB_STATES = ("SUCCEEDED", "COMPLETED")
_QUOTA_ERROR_MARKERS = ("RESOURCE_EXHAUSTED", "QUOTA", "429", "RATE LIMIT")

ProcessResults = Callable[[list[dict], list[str], dict], None]


def is_quota_error(exc: BaseException) -> bool:
    msg = str(exc).upper()
    return any(marker in msg for marker in _QUOTA_ERROR_MARKERS)


def estimate_request_tokens(req: dict) -> int:
    total = 0
    for part in req.get("systemInstruction", {}).get("parts", []):
        total += len(part.get("text", ""))
    for content in req.get("contents", []):
        for part in content.get("parts", []):
            total += len(part.get("text", ""))
    return total // CHARS_PER_TOK + BATCH_METADATA_OVERHEAD_TOKENS


def split_batches(items: list[tuple[dict, str]], max_input_tokens: int,
                  max_requests: int) -> list[list[tuple[dict, str]]]:
    """Partition [(request, key), ...] into sub-batches under max_input_tokens / max_requests."""
    batches: list[list] = []
    current: list = []
    current_tokens = 0
    for req, key in items:
        tok = estimate_request_tokens(req)
        if tok > max_input_tokens:
            raise ValueError(f"Request {key} estimated at {tok:,} tokens exceeds per-batch cap {max_input_tokens:,}")
        if current and (current_tokens + tok > max_input_tokens or len(current) == max_requests):
            batches.append(current)
            current, current_tokens = [], 0
        current.append((req, key))
        current_tokens += tok
    if current:
        batches.append(current)
    return batches


def extract_text(response: dict) -> tuple[str | None, str]:
    """(answer text without thoughts, finishReason) of a batch response."""
    if not response or "error" in response:
        return None, ""
    candidates = response.get("candidates", [])
    if not candidates:
        return None, ""
    finish_reason = str(candidates[0].get("finishReason") or "")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought")).strip()
    return text or None, finish_reason


def describe_failure(result: dict, answer_text: str | None = None) -> str:
    """Why a batch result gave no usable answer: its error, the prompt block reason, or the finish reason
    and the start of the answer."""
    response = result.get("response") or {}
    error = result.get("error") or response.get("error")
    if error:
        return error.get("message", str(error)) if isinstance(error, dict) else str(error)
    block_reason = (response.get("promptFeedback") or {}).get("blockReason")
    if block_reason:
        return f"prompt blocked: {block_reason}"
    candidates = response.get("candidates") or []
    if not candidates:
        return "no candidates in the response"
    finish_reason = candidates[0].get("finishReason") or "unknown"
    if not answer_text:
        return f"empty answer (finishReason {finish_reason})"
    return f"unparsable answer (finishReason {finish_reason}): {answer_text[:120]!r}"


def download_results(client: genai.Client, job) -> list[dict]:
    dest = getattr(job, "dest", None)
    if dest is None:
        raise RuntimeError(f"Cannot locate output file on completed job {job.name}; check the google-genai SDK version")
    dest_name = getattr(dest, "file_name", None) or str(dest)
    raw = client.files.download(file=dest_name)
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


def save_state(state: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[WARN] Corrupted state file {path} ({exc}), starting fresh.")
        return None


def write_jsonl(items: list[tuple[dict, str]], label: str, tmp_dir: Path) -> Path:
    jsonl_path = tmp_dir / f"{label}.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for req, key in items:
            f.write(json.dumps({"key": key, "request": req}, ensure_ascii=False) + "\n")
    return jsonl_path


def submit_job(client, model: str, items: list[tuple[dict, str]], label: str, tmp_dir: Path,
               state: dict, state_path: Path, est_tokens: int) -> dict:
    """Write JSONL, upload, submit. Adds the job to state["active_jobs"] and saves state."""
    jsonl_path = write_jsonl(items, label, tmp_dir)
    tqdm.write(f"  [upload] {label}: {len(items)} requests, ~{est_tokens/1_000_000:.2f}M tokens est, "
               f"{jsonl_path.stat().st_size // 1024} KB")

    uploaded = client.files.upload(file=jsonl_path, config={"mime_type": "application/jsonl"})
    try:
        job = client.batches.create(model=model, src=uploaded.name, config={"display_name": label})
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
    save_state(state, state_path)
    return info


def poll_and_drain(client, active: list[dict], state: dict, state_path: Path,
                   process_results: ProcessResults) -> list[dict]:
    """Poll active jobs; process every terminal one and return their infos."""
    drained: list[dict] = []

    for info in list(active):
        try:
            job = client.batches.get(name=info["job_name"])
        except Exception as exc:
            tqdm.write(f"  [WARN] get job {info['batch_label']}: {exc}")
            continue
        state_str = str(getattr(job, "state", "")).upper()
        if not any(s in state_str for s in TERMINAL_JOB_STATES):
            continue

        if any(s in state_str for s in SUCCEEDED_JOB_STATES):
            try:
                results = download_results(client, job)
            except Exception as exc:
                tqdm.write(f"  [WARN] download {info['batch_label']}: {exc}, leaving for retry")
                continue
            tqdm.write(f"  [drain] {info['batch_label']} SUCCEEDED ({len(results)} results)")
            process_results(results, info["keys"], state)
        else:
            tqdm.write(f"  [drain] {info['batch_label']} {state_str}, bumping attempts for {len(info['keys'])} requests")
            process_results([], info["keys"], state)

        try:
            client.files.delete(name=info["uploaded_name"])
        except Exception as exc:
            tqdm.write(f"  [WARN] delete upload {info['uploaded_name']}: {exc}")

        state["active_jobs"] = [j for j in state["active_jobs"] if j["job_name"] != info["job_name"]]
        save_state(state, state_path)
        drained.append(info)

    return drained


def run_pool(client, model: str, sub_batches: list[tuple[list, str]], tmp_dir: Path, state: dict,
             state_path: Path, desc: str, process_results: ProcessResults, *, enqueue_cap: int,
             max_concurrent_jobs: int, poll_interval_s: int) -> None:
    """
    Submit sub-batches under the enqueued-token budget, drain completions as
    they arrive, refill freed budget from pending. Quota errors pause submission
    until something drains.
    """
    pending = list(sub_batches)
    active: list[dict] = []
    budget_used = 0

    pbar = tqdm(total=sum(len(items) for items, _ in sub_batches), desc=desc, unit="req", dynamic_ncols=True)

    while pending or active:
        submitted = 0
        while pending:
            items, label = pending[0]
            est = sum(estimate_request_tokens(r) for r, _ in items)
            if active and (budget_used + est > enqueue_cap or len(active) >= max_concurrent_jobs):
                tqdm.write(f"  [budget] pool full ({len(active)} jobs, {budget_used/1_000_000:.1f}M used), draining")
                break
            try:
                info = submit_job(client, model, items, label, tmp_dir, state, state_path, est)
            except Exception as exc:
                if is_quota_error(exc):
                    tqdm.write(f"  [quota] server refused submit, waiting for drain: {exc}")
                    break
                raise
            active.append(info)
            budget_used += est
            pending.pop(0)
            submitted += 1
            tqdm.write(f"  [submit] {label}  est={est/1_000_000:.2f}M tok  pool={len(active)}  "
                       f"used={budget_used/1_000_000:.1f}/{enqueue_cap/1_000_000:.0f}M  pending={len(pending)}")

        drained = poll_and_drain(client, active, state, state_path, process_results)
        for info in drained:
            active.remove(info)
            budget_used -= info["est_tokens"]
            pbar.update(len(info["keys"]))

        if not submitted and not drained and active:
            time.sleep(poll_interval_s)

    pbar.close()


def resume_active_jobs(client, state: dict, state_path: Path, process_results: ProcessResults,
                       poll_interval_s: int) -> None:
    """Drain every job that was in flight when the previous run stopped."""
    active = list(state.get("active_jobs", []))
    if not active:
        return
    print(f"\nResuming {len(active)} active batch job(s) from prior run …")
    while active:
        for info in poll_and_drain(client, active, state, state_path, process_results):
            active.remove(info)
        if active:
            tqdm.write(f"  [resume] {len(active)} job(s) still running, sleeping {poll_interval_s}s …")
            time.sleep(poll_interval_s)
    print("  Resume complete.\n")
