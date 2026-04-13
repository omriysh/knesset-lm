"""
qwen3.py

Qwen3-specific LLMBackend — extends LlamaServerBackend with:
  - /no_think token to suppress extended thinking
  - <think>…</think> stripping from content
  - reasoning_content streaming (handled in base) + XML <think> fallback
  - max_thinking_tokens sent to llama-server
"""

from __future__ import annotations

import re

import config
from agent.llm.llama_server import LlamaServerBackend


_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


class Qwen3LlamaBackend(LlamaServerBackend):
    """LLMBackend for Qwen3 served by llama-server."""

    supports_thinking = True
    _log_prefix       = "qwen3"
    TEMPERATURE       = 0.6
    TOP_K             = 20

    def __init__(
        self,
        url:                str = config.LLAMA_SERVER,
        timeout:            int = 300,
        max_thinking_tokens: int = config.MAX_THINKING_TOKENS,
    ):
        super().__init__(url=url, timeout=timeout, max_thinking_tokens=max_thinking_tokens)

    def prepare_messages(
        self,
        messages: list[dict],
        suppress_thinking: bool = True,
    ) -> list[dict]:
        """Append '/no_think' to the last user message when suppress_thinking=True."""
        if not suppress_thinking or not messages:
            return messages
        out  = list(messages)
        last = out[-1]
        if last.get("role") == "user":
            out[-1] = {**last, "content": (last.get("content") or "") + " /no_think"}
        return out

    def extract_visible_content(self, raw_content: str) -> str:
        """Strip <think>…</think> blocks; return remainder stripped."""
        return _THINK_RE.sub("", raw_content).strip()

    def extract_thinking(self, raw_content: str) -> str:
        """Return concatenated contents of all <think>…</think> blocks (XML fallback)."""
        blocks = _THINK_RE.findall(raw_content)
        return "\n---\n".join(b.strip() for b in blocks if b.strip())
