"""
session.py

ResearchSession dataclass, disk I/O helpers, and TTL-based cleanup.

Session files are stored as {session_id}.json in the sessions directory.
Each file is a JSON-serialised ResearchSession.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields as dc_fields, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class ResearchSession:
    session_id: str
    status: str                         # "running" | "awaiting_user" | "done" | "error"
    original_question: str
    created_at: str                     # ISO timestamp (UTC, ends with "Z")
    updated_at: str                     # ISO timestamp (UTC, ends with "Z")
    machine_checkpoint: dict | None = None  # checkpoint dict from runner
    final_answer: str | None = None
    error: str | None = None
    workspace_data: dict | None = None  # {selected_chunks}
    event_log: list | None = None       # selective SSE event log for reconnect replay


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _session_path(session_id: str, sessions_dir: Path) -> Path:
    return sessions_dir / f"{session_id}.json"


# ── Disk I/O ──────────────────────────────────────────────────────────────────

def save_session(session: ResearchSession, sessions_dir: Path) -> None:
    """Persist session to disk as pretty-printed JSON (atomic write)."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    data = asdict(session)
    target = _session_path(session.session_id, sessions_dir)
    # Write to a unique temp file then rename for atomicity.
    # Unique suffix avoids WinError 32 when two threads save the same session concurrently.
    tmp = target.with_name(target.stem + f"_{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # Windows antivirus/indexer may hold a transient lock on the target; retry briefly.
        for _attempt in range(5):
            try:
                tmp.replace(target)
                break
            except PermissionError as exc:
                if _attempt == 4:
                    raise
                print(f"[session] {target.name} is locked, retrying: {exc}", flush=True)
                time.sleep(0.05 * (_attempt + 1))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[session] could not remove temp file {tmp.name}: {exc}", flush=True)
        raise


def load_session(session_id: str, sessions_dir: Path) -> ResearchSession | None:
    """Load session from disk. Returns None if the file does not exist."""
    path = _session_path(session_id, sessions_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in dc_fields(ResearchSession)}
        return ResearchSession(**{k: v for k, v in data.items() if k in known})
    except Exception as exc:
        print(f"[session] load_session failed for {session_id!r}: {exc}", flush=True)
        return None


_status_transition_lock = threading.Lock()


def mark_running_if_awaiting_user(session_id: str, sessions_dir: Path) -> ResearchSession | None:
    """Move an awaiting_user session to running and return it as it was; None when it is not awaiting
    the user (so of two parallel responses only the first resumes the run)."""
    with _status_transition_lock:
        session = load_session(session_id, sessions_dir)
        if session is None or session.status != "awaiting_user":
            return None
        save_session(replace(session, status="running", updated_at=_now_iso()), sessions_dir)
        return session


_last_cleanup_monotonic: float | None = None
_cleanup_lock = threading.Lock()


def maybe_cleanup_stale_sessions(sessions_dir: Path) -> None:
    """Run cleanup_stale_sessions at most once per WEB_SESSION_CLEANUP_INTERVAL_SECONDS while serving."""
    global _last_cleanup_monotonic
    now = time.monotonic()
    with _cleanup_lock:
        if (_last_cleanup_monotonic is not None
                and now - _last_cleanup_monotonic < config.WEB_SESSION_CLEANUP_INTERVAL_SECONDS):
            return
        _last_cleanup_monotonic = now
    try:
        cleanup_stale_sessions(sessions_dir, max_age_hours=config.WEB_SESSION_MAX_AGE_HOURS)
    except Exception as exc:
        print(f"[session] periodic cleanup of {sessions_dir} failed: {exc}", flush=True)


def cleanup_stale_sessions(sessions_dir: Path, max_age_hours: float = 2.0) -> int:
    """
    Remove session files older than max_age_hours.
    Returns the number of files deleted.
    """
    if not sessions_dir.exists():
        return 0

    cutoff_seconds = max_age_hours * 3600
    now = datetime.now(timezone.utc).timestamp()
    removed = 0

    for path in sessions_dir.glob("*.json"):
        try:
            age = now - path.stat().st_mtime
            if age > cutoff_seconds:
                path.unlink()
                removed += 1
        except OSError as exc:
            print(f"[session] could not remove stale session {path.name}: {exc}", flush=True)

    if removed:
        print(f"[session] Cleaned up {removed} stale session(s).", flush=True)
    return removed
