"""
session.py

ResearchSession dataclass, disk I/O helpers, and TTL-based cleanup.

Session files are stored as {session_id}.json in the sessions directory.
Each file is a JSON-serialised ResearchSession.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
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
    # Write to a temp file then rename for atomicity
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def load_session(session_id: str, sessions_dir: Path) -> ResearchSession | None:
    """Load session from disk. Returns None if the file does not exist."""
    path = _session_path(session_id, sessions_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ResearchSession(**data)
    except Exception:
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
