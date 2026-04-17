"""
gemini.py

GeminiBackend — LLMBackend implementation for Google's Gemini API.

Uses google.genai SDK (google-genai package) with gemini-2.5-flash-lite.
API key read from GEMINI_API_KEY env var.
"""

from __future__ import annotations

import json
import os
import time
from typing import Generator

from google import genai
from google.genai import types

import config
from agent.llm.base import DoneEvent, LLMEvent, ThinkingEvent, TokenEvent, ToolCallsEvent


def _convert_tools_genai(tools: list[dict]) -> list[types.Tool] | None:
    """Convert OpenAI-style tool schema to google.genai Tool objects."""
    if not tools:
        return None
    declarations = [
        types.FunctionDeclaration(
            name        = t["function"]["name"],
            description = t["function"].get("description", ""),
            parameters  = t["function"].get("parameters", {}),
        )
        for t in tools
        if t.get("type") == "function"
    ]
    return [types.Tool(function_declarations=declarations)] if declarations else None


def _build_tool_call_map(messages: list[dict]) -> dict[str, str]:
    """Build tool_call_id → function_name map from assistant messages."""
    mapping: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                mapping[tc["id"]] = tc["function"]["name"]
    return mapping


def _convert_messages(messages: list[dict]) -> tuple[list[dict], str | None]:
    """
    Convert OpenAI-style messages to Gemini contents format.
    Returns (contents, system_instruction_text).
    """
    system_parts: list[str] = []
    contents: list[dict] = []
    tc_id_to_name = _build_tool_call_map(messages)

    for msg in messages:
        role    = msg.get("role", "")
        content = msg.get("content") or ""

        if role == "system":
            system_parts.append(content)
            continue

        if role == "user":
            contents.append({"role": "user", "parts": [{"text": content}]})

        elif role == "assistant":
            parts: list[dict] = []
            if content:
                parts.append({"text": content})
            for tc in msg.get("tool_calls") or []:
                fn   = tc["function"]
                args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                parts.append({"functionCall": {"name": fn["name"], "args": args}})
            if parts:
                contents.append({"role": "model", "parts": parts})

        elif role == "tool":
            fn_name = tc_id_to_name.get(msg.get("tool_call_id", ""), "unknown")
            contents.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name":     fn_name,
                        "response": {"content": content},
                    }
                }],
            })

    system_text = "\n\n".join(system_parts) if system_parts else None
    return contents, system_text


_CTX_SIZE: dict[str, int] = {
    "gemini-2.5-flash-lite": 500_000,
    "gemini-2.5-flash":      500_000,
    "gemini-2.5-pro":        500_000,
    "gemma-4-31b-it":        15_000,
    "gemma-4-12b-it":        15_000,
}
_CTX_SIZE_DEFAULT = 500_000


class GeminiBackend:
    """LLMBackend for Google Gemini API / Gemma via google.genai SDK."""

    MODEL             = "gemini-2.5-flash-lite"
    supports_thinking = False
    TEMPERATURE       = 1.0
    _log_prefix       = "gemini"

    @property
    def max_chunk_chars(self) -> int:
        return self.ctx_size * config.CHARS_PER_TOK

    def __init__(
        self,
        model:      str | None = None,
        api_key:    str | None = None,
        timeout:    int        = 300,
        max_tokens: int        = config.MAX_TOKENS,
    ):
        api_key          = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._model      = model or self.MODEL
        self.ctx_size    = _CTX_SIZE.get(self._model, _CTX_SIZE_DEFAULT)
        self._client     = genai.Client(api_key=api_key)
        self._timeout    = timeout
        self._max_tokens = max_tokens

    # ── Protocol methods ──────────────────────────────────────────────────────

    def prepare_messages(
        self,
        messages:          list[dict],
        suppress_thinking: bool = False,
    ) -> list[dict]:
        return messages

    def extract_visible_content(self, raw_content: str) -> str:
        return raw_content.strip()

    def extract_tool_calls(
        self,
        message:     dict,
        raw_content: str,
    ) -> tuple[list[dict], str]:
        # Tool calls are emitted as ToolCallsEvent during stream(); nothing to extract here.
        return [], raw_content

    def needs_thinking_retry(self, content: str, tool_calls: list) -> bool:
        return False

    def extract_thinking(self, raw_content: str) -> str:
        return ""

    # ── Streaming ─────────────────────────────────────────────────────────────

    def stream(
        self,
        messages:    list[dict],
        tools:       list[dict] | None = None,
        temperature: float | None      = None,
        max_tokens:  int               = config.MAX_TOKENS,
    ) -> Generator[LLMEvent, None, None]:
        contents, system_text = _convert_messages(messages)

        gen_config = types.GenerateContentConfig(
            max_output_tokens  = max_tokens,
            temperature        = temperature if temperature is not None else self.TEMPERATURE,
            system_instruction = system_text,
            tools              = _convert_tools_genai(tools) if tools else None,
        )

        tc_list:    list[dict] = []
        t0                     = time.monotonic()
        ttft:       float      = 0.0
        token_count            = 0

        for chunk in self._client.models.generate_content_stream(
            model    = self._model,
            contents = contents,
            config   = gen_config,
        ):
            for candidate in chunk.candidates or []:
                if not candidate.content:
                    continue
                for part in candidate.content.parts or []:
                    if getattr(part, "thought", False):
                        if part.text:
                            if not ttft:
                                ttft = time.monotonic() - t0
                            yield ThinkingEvent(part.text)

                    elif part.function_call:
                        fc = part.function_call
                        tc_list.append({
                            "id":   f"gemini_{len(tc_list)}",
                            "type": "function",
                            "function": {
                                "name":      fc.name,
                                "arguments": json.dumps(dict(fc.args), ensure_ascii=False),
                            },
                        })

                    elif part.text:
                        if not ttft:
                            ttft = time.monotonic() - t0
                        token_count += 1
                        yield TokenEvent(part.text)

        total = time.monotonic() - t0
        gen   = total - ttft
        tps   = token_count / gen if gen > 0 else 0.0
        print(
            f"[{self._log_prefix}] ttft={ttft:.2f}s tokens={token_count} "
            f"gen={gen:.2f}s tps={tps:.1f}",
            flush=True,
        )

        if tc_list:
            yield ToolCallsEvent(tc_list)
        yield DoneEvent()
