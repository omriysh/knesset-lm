"""
Concurrency limits for agent runs in the web server: a fixed number of research runs execute at
once, a bounded number wait for a slot (for a bounded time), and a waiting run gives up as soon as
its client is gone.
"""

import sys
import threading
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config

SLOT_POLL_SECONDS = 0.5


class ResearchRunSlots:
    def __init__(self, max_running: int):
        self._max_running = max_running
        self._running = 0
        self._waiting = 0
        self._changed = threading.Condition()

    def can_admit(self) -> bool:
        """False when every slot is taken and WEB_RESEARCH_MAX_QUEUED_RUNS runs already wait (→ 503)."""
        with self._changed:
            return self._running < self._max_running or self._waiting < config.WEB_RESEARCH_MAX_QUEUED_RUNS

    def waiting_count(self) -> int:
        with self._changed:
            return self._waiting

    def acquire(self, stop_requested: threading.Event, on_queued: Callable[[], None]) -> bool:
        """Take a slot; when none is free call on_queued() and wait up to WEB_RESEARCH_SLOT_WAIT_SECONDS.
        False when the wait timed out or stop_requested was set meanwhile."""
        with self._changed:
            if self._running < self._max_running:
                self._running += 1
                return True
            self._waiting += 1
        try:
            on_queued()
            deadline = time.monotonic() + config.WEB_RESEARCH_SLOT_WAIT_SECONDS
            with self._changed:
                while self._running >= self._max_running:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0 or stop_requested.is_set():
                        return False
                    self._changed.wait(timeout=min(remaining_seconds, SLOT_POLL_SECONDS))
                self._running += 1
                return True
        finally:
            with self._changed:
                self._waiting -= 1

    def release(self) -> None:
        with self._changed:
            self._running -= 1
            self._changed.notify()
