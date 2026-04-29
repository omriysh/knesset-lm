"""
gemma.py

Gemma-specific LLMBackend.

llama-server extracts Gemma's <think>...</think> blocks into
``reasoning_content`` (ThinkingEvents) and puts the actual answer in
``content`` (TokenEvents).  Without a ``max_thinking_tokens`` budget the
total budget (``max_tokens``) is consumed by thinking and no answer is ever
produced.  Sending ``max_thinking_tokens`` gives thinking its own lane.

Overrides:
  * ``supports_thinking = True``  — ThinkingEvents are accumulated, not
                                    discarded, so callers can surface them.
  * ``needs_thinking_retry``      — always False; if the answer still lands
                                    in thinking (older llama-server builds),
                                    the runner falls back to thinking text
                                    instead of spinning in a retry loop.
"""

from __future__ import annotations

import config
from agent.llm.llama_server import LlamaServerBackend


class GemmaLlamaBackend(LlamaServerBackend):
    """LLMBackend for Gemma served by llama-server."""

    supports_thinking = True
    _log_prefix       = "gemma"
    TEMPERATURE       = 1.0
    TOP_K             = 65

    def __init__(
        self,
        url:                 str = config.LLAMA_SERVER,
        timeout:             int = 300,
        max_thinking_tokens: int = config.MAX_THINKING_TOKENS,
    ):
        super().__init__(url=url, timeout=timeout, max_thinking_tokens=max_thinking_tokens)

    def needs_thinking_retry(self, content: str, tool_calls: list) -> bool:
        # Gemma's full response lands in reasoning_content; content is always
        # empty.  Never retry — the runner uses thinking text as the answer.
        return False
