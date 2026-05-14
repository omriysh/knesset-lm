"""LLMBridge — unified LLM adapter with built-in SubgraphEvent emission.

Wraps LLMBackend instances (one per model, lazily cached) and provides:

  * ``__call__``     — synchronous call returning text or tool-call dict;
                       emits llm_start / llm_done events into a thread-local
                       buffer accessible via ``drain_events()``.
  * ``stream``       — generator that yields SubgraphEvents (llm_start,
                       llm_thinking, llm_token, llm_done) directly; used by
                       callers that run on the main generator thread.
  * ``drain_events`` — pop and return all buffered events; used by worker
                       threads (executor) whose llm_call results are collected
                       on the main thread via _dispatch_step.
  * ``stream_raw``   — raw LLMEvent generator, retained for callers that
                       need the backend events directly (kept for backward
                       compat; prefer ``stream``).

Thread safety: ``__call__`` stores events in a ``threading.local`` deque so
worker-thread executor calls don't race with each other or with the main
thread's ``stream()`` path.

Routing rules (model param):
  * ``"local"``     → GemmaLlamaBackend (llama-server)
  * ``"gemma-4-*"`` / ``"gemini-*"`` and anything else → GoogleBackend
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Generator

import config
from agent.llm.base import DoneEvent, ThinkingEvent, TokenEvent, ToolCallsEvent
from agent.subgraph.base import SubgraphEvent


# ---------------------------------------------------------------------------
# Thread-local event buffer and immediate-emit sink
# ---------------------------------------------------------------------------

_tl = threading.local()


def _get_buffer() -> deque:
    if not hasattr(_tl, "events"):
        _tl.events = deque()
    return _tl.events


def set_thread_event_sink(sink) -> None:
    """Register a per-thread sink callable(SubgraphEvent) → None.

    When set, LLMBridge.__call__ emits events immediately via the sink
    instead of buffering them for drain_events().  Pass None to clear.

    Workers that need to propagate the caller's sink should call
    get_thread_event_sink() on the spawning thread and call
    set_thread_event_sink(captured_sink) at the top of the worker body.
    """
    _tl.sink = sink


def get_thread_event_sink():
    """Return the sink registered on this thread, or None."""
    return getattr(_tl, "sink", None)


# ---------------------------------------------------------------------------
# Internal drain-stream helper (identical to what agent.py had before)
# ---------------------------------------------------------------------------


def _drain_stream(stream) -> tuple[str, list[dict], str]:
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    thinking_parts: list[str] = []
    for ev in stream:
        if isinstance(ev, ThinkingEvent):
            thinking_parts.append(ev.text)
        elif isinstance(ev, TokenEvent):
            text_parts.append(ev.text)
        elif isinstance(ev, ToolCallsEvent):
            tool_calls = list(ev.calls or [])
        elif isinstance(ev, DoneEvent):
            break
    return ("".join(text_parts), tool_calls, "".join(thinking_parts))


def _normalize_tool_calls(tcs: list[dict]) -> list[dict]:
    return tcs


# ---------------------------------------------------------------------------
# LLMBridge
# ---------------------------------------------------------------------------


class LLMBridge:
    """Wraps LLMBackend factories with automatic SubgraphEvent emission.

    Construction is cheap — no network or API-key validation happens until
    the first actual LLM call is attempted.
    """

    def __init__(self, fallback_to_local: bool = True):
        self._fallback_to_local = bool(fallback_to_local)
        self._cache: dict[tuple[str, str], Any] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    def __call__(
        self,
        *,
        model: str,
        prompt: str | list[dict] | None = None,
        messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        phase: str = "",
    ) -> dict:
        """Synchronous LLM call.

        Emits llm_start and llm_done into the thread-local event buffer so
        caller can drain them with ``drain_events()`` on its own thread.

        Returns either a plain text string (no tools) or a dict with
        ``tool_calls`` and ``content`` (tools path).
        """
        backend = self._backend_for(model)
        msgs = self._build_messages(prompt, messages, response_format)

        # Build a compact user-facing prompt preview (first 500 chars of
        # the last user message, or the full prompt if short).
        prompt_preview = _prompt_preview(msgs)

        sink = get_thread_event_sink()
        buf  = _get_buffer()

        def _emit(ev: SubgraphEvent) -> None:
            if sink is not None:
                sink(ev)
            else:
                buf.append(ev)

        phase_name = phase or model
        _emit(SubgraphEvent(
            kind="llm_start",
            name=phase_name,
            payload={"phase": phase, "model": model, "prompt": {"user": prompt_preview}},
        ))
        t0 = time.monotonic()

        kwargs: dict[str, Any] = {"messages": msgs}
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        try:
            text, tool_calls, thinking = _drain_stream(backend.stream(**kwargs))
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            _emit(SubgraphEvent(
                kind="llm_done",
                name=phase_name,
                payload={"content": "", "elapsed_ms": elapsed, "error": str(exc)},
            ))
            raise RuntimeError(f"llm_call({model!r}) failed: {exc}") from exc

        if thinking:
            _emit(SubgraphEvent(
                kind="llm_thinking",
                name=phase_name,
                payload={"text": thinking},
            ))

        elapsed = int((time.monotonic() - t0) * 1000)
        _emit(SubgraphEvent(
            kind="llm_done",
            name=phase_name,
            payload={"content": text or "", "elapsed_ms": elapsed},
        ))

        if tools:
            return {
                "content": text,
                "tool_calls": _normalize_tool_calls(tool_calls),
            }
        return text

    def stream(
        self,
        *,
        model: str,
        prompt: str | list[dict] | None = None,
        messages: list[dict] | None = None,
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        phase: str = "",
    ) -> Generator[SubgraphEvent, None, None]:
        """Generator that yields SubgraphEvents while streaming from the LLM.

        Yields: llm_start → llm_thinking* → llm_token* → llm_done

        Use this on the main generator thread where you can yield events
        directly. After the generator is exhausted, the full text is
        available via the ``result`` attribute of the last llm_done event
        — but it is more ergonomic to collect tokens inside the loop.
        """
        backend = self._backend_for(model)
        msgs = self._build_messages(prompt, messages, response_format)
        prompt_preview = _prompt_preview(msgs)

        phase_name = phase or model
        yield SubgraphEvent(
            kind="llm_start",
            name=phase_name,
            payload={"phase": phase, "model": model, "prompt": {"user": prompt_preview}},
        )
        t0 = time.monotonic()

        kwargs: dict[str, Any] = {"messages": msgs}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        text_parts: list[str] = []
        try:
            for ev in backend.stream(**kwargs):
                if isinstance(ev, ThinkingEvent):
                    yield SubgraphEvent(kind="llm_thinking", name=phase_name,
                                        payload={"text": ev.text})
                elif isinstance(ev, TokenEvent):
                    text_parts.append(ev.text)
                    yield SubgraphEvent(kind="llm_token", name=phase_name,
                                        payload={"text": ev.text})
                elif isinstance(ev, DoneEvent):
                    break
        except Exception as exc:
            elapsed = int((time.monotonic() - t0) * 1000)
            yield SubgraphEvent(
                kind="llm_done",
                name=phase_name,
                payload={"content": "", "elapsed_ms": elapsed, "error": str(exc)},
            )
            return

        content = "".join(text_parts)
        elapsed = int((time.monotonic() - t0) * 1000)
        yield SubgraphEvent(
            kind="llm_done",
            name=phase_name,
            payload={"content": content, "elapsed_ms": elapsed},
        )

    def drain_events(self) -> list[SubgraphEvent]:
        """Pop and return all events buffered by ``__call__`` on this thread."""
        buf = _get_buffer()
        out: list[SubgraphEvent] = []
        while buf:
            out.append(buf.popleft())
        return out

    def stream_raw(
        self,
        *,
        model: str,
        prompt: str | list[dict] | None = None,
        messages: list[dict] | None = None,
        response_format: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        """Raw LLMEvent generator — retained for backward compat."""
        backend = self._backend_for(model)
        msgs = self._build_messages(prompt, messages, response_format)
        kwargs: dict[str, Any] = {"messages": msgs}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return backend.stream(**kwargs)

    # ── Internal ────────────────────────────────────────────────────────────

    def _build_messages(
        self,
        prompt: str | list[dict] | None,
        messages: list[dict] | None,
        response_format: dict | None,
    ) -> list[dict]:
        if messages is not None:
            out = list(messages)
        elif isinstance(prompt, list):
            out = list(prompt)
        elif isinstance(prompt, str):
            out = [{"role": "user", "content": prompt}]
        else:
            out = []

        if response_format and response_format.get("type") == "json_object":
            if out and out[-1].get("role") == "user":
                hint = "\n\nReply with ONE JSON object (no markdown fences, no prose)."
                out[-1] = {
                    "role":    out[-1].get("role"),
                    "content": (out[-1].get("content") or "") + hint,
                }
        return out

    def _backend_for(self, model: str):
        kind = "local" if model == "local" else "google"
        cache_key = (kind, model)
        if cache_key in self._cache:
            return self._cache[cache_key]

        if kind == "local":
            from agent.llm.gemma import GemmaLlamaBackend
            backend = GemmaLlamaBackend()
        else:
            from agent.llm.google import GoogleBackend
            backend = GoogleBackend(model=model)

        self._cache[cache_key] = backend
        return backend


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prompt_preview(msgs: list[dict]) -> str:
    """Return a short preview of the last user message in msgs."""
    for msg in reversed(msgs):
        if msg.get("role") == "user":
            content = msg.get("content") or ""
            return content if isinstance(content, str) else str(content)
    return ""


__all__ = ["LLMBridge", "set_thread_event_sink", "get_thread_event_sink"]
