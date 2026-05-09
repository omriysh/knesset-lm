"""
base.py

LLMBackend abstract base class: abstracts one LLM deployment so that all
model-specific code (Qwen3 thinking tokens, XML tool-call fallback, etc.) is
isolated in a single implementation class.  Nothing else in the codebase
imports anything model-specific.

Context injection
-----------------
``stream()`` is a concrete method on the ABC that prepends a lightweight
system message with today's date and current Knesset context before every
LLM call.  Subclasses implement ``_stream_impl()`` with the raw network /
SDK logic.  The injection is idempotent — if a system message that already
contains "Today's date:" is present, it is left unchanged.
"""

from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Generator

import config
from config import MAX_TOKENS


# ── Streaming event types ─────────────────────────────────────────────────────

@dataclass
class TokenEvent:
    text: str


@dataclass
class ThinkingEvent:
    text: str   # one chunk of reasoning/thinking content


@dataclass
class ToolCallsEvent:
    calls: list[dict]   # complete, accumulated after the stream ends


@dataclass
class DoneEvent:
    # Set by GoogleBackend when the cloud call failed and we transparently
    # served the response from the local llama-server fallback instead.
    cloud_failed_used_local: bool = False


LLMEvent = TokenEvent | ThinkingEvent | ToolCallsEvent | DoneEvent


# ── Date-context injection ────────────────────────────────────────────────────

def _inject_date_context(messages: list[dict]) -> list[dict]:
    """Prepend a system message with today's date and Knesset context.

    Idempotent: if the first system message already contains "Today's date:"
    it is left unchanged (handles re-entrant calls such as the Google local
    fallback path).
    """
    knesset_num = getattr(config, "KNESSET_NUM", 25)
    today = datetime.date.today().isoformat()
    context = (
        f"Today's date: {today}. "
        f"The {knesset_num}th Knesset is currently active; "
        "its data is available up to today. "
        "Do not assume that data from the current year is unavailable or in the future."
    )

    if messages and messages[0].get("role") == "system":
        existing = messages[0].get("content", "")
        if "Today's date:" in existing:
            return messages  # already injected
        return [{"role": "system", "content": context + "\n\n" + existing}] + messages[1:]

    return [{"role": "system", "content": context}] + list(messages)


# ── Backend abstract base class ───────────────────────────────────────────────

class LLMBackend(ABC):
    """
    Abstracts one LLM deployment.

    All Qwen3/llama.cpp specifics live in Qwen3LlamaBackend.
    Adding a new model: subclass this, implement _stream_impl() and the
    remaining abstract methods; pass instance to MachineRunner.
    No changes needed in runner.py, context.py, or web/app.py.
    """

    supports_thinking: bool
    ctx_size:          int   # model context window in tokens
    max_chunk_chars:   int   # max chars per transcript chunk for summarization

    # ── Concrete stream wrapper (injects date context) ────────────────────

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> Generator[LLMEvent, None, None]:
        """Inject date/Knesset context then delegate to ``_stream_impl``."""
        return self._stream_impl(
            messages    = _inject_date_context(messages),
            tools       = tools,
            temperature = temperature,
            max_tokens  = max_tokens,
        )

    # ── Abstract implementation hook ──────────────────────────────────────

    @abstractmethod
    def _stream_impl(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int = MAX_TOKENS,
    ) -> Generator[LLMEvent, None, None]:
        """
        Stream one completion.

        Yields:
          TokenEvent(text)          — one content token
          ToolCallsEvent(calls)     — complete tool calls (emitted once, after stream)
          DoneEvent()               — generation complete
        """
        ...

    # ── Abstract helper methods ───────────────────────────────────────────

    @abstractmethod
    def prepare_messages(
        self,
        messages: list[dict],
        suppress_thinking: bool = False,
    ) -> list[dict]:
        """
        Apply model-specific message transformations before sending.
        For Qwen3: appends "/no_think" when suppress_thinking=True.
        For other models: may be a no-op.
        """
        ...

    @abstractmethod
    def extract_visible_content(self, raw_content: str) -> str:
        """
        Strip model-specific internal markup from the assembled content string.
        For Qwen3: removes <think>…</think> blocks.
        For other models: identity function.
        """
        ...

    @abstractmethod
    def extract_tool_calls(
        self,
        message: dict,
        raw_content: str,
    ) -> tuple[list[dict], str]:
        """
        Return (tool_calls, cleaned_content).
        For Qwen3/llama.cpp: falls back to XML <tool_call> parsing when
        message.get("tool_calls") is empty.
        For standard OpenAI API: returns (message.get("tool_calls") or [], content).
        """
        ...

    @abstractmethod
    def needs_thinking_retry(self, content: str, tool_calls: list) -> bool:
        """
        True if the model produced no usable output (thinking-only response).
        For Qwen3: content is blank after stripping <think>…</think> and no tool calls.
        For other models: typically always False.
        """
        ...

    @abstractmethod
    def extract_thinking(self, raw_content: str) -> str:
        """
        Extract the thinking/reasoning text from raw model output (before stripping).
        For Qwen3: returns concatenated <think>…</think> block contents.
        For other models: returns "".
        """
        ...
