"""
gemma.py

Gemma-specific LLMBackend — extends LlamaServerBackend with no overrides.

Gemma served via llama-server uses the same OpenAI-compatible endpoint as
Qwen3 but has no thinking tokens, no /no_think suppression, and no <think>
XML blocks.  All base-class behaviour is correct as-is.
"""

from __future__ import annotations

import config
from agent.llm.llama_server import LlamaServerBackend


class GemmaLlamaBackend(LlamaServerBackend):
    """LLMBackend for Gemma served by llama-server."""

    supports_thinking = False
    _log_prefix       = "gemma"
    TEMPERATURE       = 1.0
    TOP_K             = 65

    def __init__(
        self,
        url:     str = config.LLAMA_SERVER,
        timeout: int = 300,
    ):
        super().__init__(url=url, timeout=timeout, max_thinking_tokens=0)
