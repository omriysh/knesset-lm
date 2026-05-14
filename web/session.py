"""
session.py

ResearchSession dataclass, disk I/O helpers, and TTL-based cleanup.

Session files are stored as {session_id}.json in the sessions directory.
Each file is a JSON-serialised ResearchSession.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class ResearchSession:
    session_id: str
    status: str                         # "awaiting_user" | "done" | "error"
    original_question: str
    created_at: str                     # ISO timestamp (UTC, ends with "Z")
    updated_at: str                     # ISO timestamp (UTC, ends with "Z")
    machine_checkpoint: dict | None = None  # checkpoint dict from runner
    final_answer: str | None = None
    error: str | None = None
    workspace_data: dict | None = None  # {meeting_paths, selected_chunks}
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
            except PermissionError:
                if _attempt == 4:
                    raise
                time.sleep(0.05 * (_attempt + 1))
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
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


def delete_session(session_id: str, sessions_dir: Path) -> bool:
    """Delete session file. Returns True if deleted, False if not found."""
    path = _session_path(session_id, sessions_dir)
    if path.exists():
        path.unlink()
        return True
    return False


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
        except OSError:
            pass  # file may have been removed concurrently

    if removed:
        print(f"[session] Cleaned up {removed} stale session(s).", flush=True)
    return removed
