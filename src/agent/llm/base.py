"""
base.py

LLMBackend protocol: abstracts one LLM deployment so that all model-specific
code (Qwen3 thinking tokens, XML tool-call fallback, etc.) is isolated in a
single implementation class.  Nothing else in the codebase imports anything
model-specific.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generator, Protocol, runtime_checkable
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


# ── Backend protocol ──────────────────────────────────────────────────────────

@runtime_checkable
class LLMBackend(Protocol):
    """
    Abstracts one LLM deployment.

    All Qwen3/llama.cpp specifics live in Qwen3LlamaBackend.
    Adding a new model: implement this protocol; pass to MachineRunner.
    No changes needed in runner.py, context.py, or web/app.py.
    """

    supports_thinking: bool
    ctx_size:          int   # model context window in tokens
    max_chunk_chars:   int   # max chars per transcript chunk for summarization

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,  # None → use backend's TEMPERATURE class constant
        max_tokens: int = MAX_TOKENS,    # override in implementations with config.MAX_TOKENS
    ) -> Generator[LLMEvent, None, None]:
        """
        Stream one completion.

        Yields:
          TokenEvent(text)          — one content token
          ToolCallsEvent(calls)     — complete tool calls (emitted once, after stream)
          DoneEvent()               — generation complete
        """
        ...

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

    def extract_visible_content(self, raw_content: str) -> str:
        """
        Strip model-specific internal markup from the assembled content string.
        For Qwen3: removes <think>…</think> blocks.
        For other models: identity function.
        """
        ...

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

    def needs_thinking_retry(self, content: str, tool_calls: list) -> bool:
        """
        True if the model produced no usable output (thinking-only response).
        For Qwen3: content is blank after stripping <think>…</think> and no tool calls.
        For other models: typically always False.
        """
        ...

    def extract_thinking(self, raw_content: str) -> str:
        """
        Extract the thinking/reasoning text from raw model output (before stripping).
        For Qwen3: returns concatenated <think>…</think> block contents.
        For other models: returns "".
        """
        ...
