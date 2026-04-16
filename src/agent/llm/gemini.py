"""
gemini.py

GeminiBackend — LLMBackend implementation for Google's Gemini API.

Uses gemini-2.5-flash-lite via the v1beta REST endpoint with SSE streaming.
API key read from GEMINI_API_KEY env var.
"""

from __future__ import annotations

import json
import os
import time
from typing import Generator

import requests

import config
from agent.llm.base import DoneEvent, LLMEvent, ThinkingEvent, TokenEvent, ToolCallsEvent


_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _convert_tools(tools: list[dict]) -> list[dict]:
    """Convert OpenAI-style tool schema to Gemini functionDeclarations."""
    return [
        {
            "name":        t["function"]["name"],
            "description": t["function"].get("description", ""),
            "parameters":  t["function"].get("parameters", {}),
        }
        for t in tools
        if t.get("type") == "function"
    ]


def _build_tool_call_map(messages: list[dict]) -> dict[str, str]:
    """
    Build a map of tool_call_id → function_name by scanning assistant messages.
    Used to resolve function names when converting tool-result messages.
    """
    mapping: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                mapping[tc["id"]] = tc["function"]["name"]
    return mapping


def _convert_messages(messages: list[dict]) -> tuple[list[dict], dict | None]:
    """
    Convert OpenAI-style messages to Gemini contents format.

    Returns (contents, system_instruction) where system_instruction is a
    Gemini systemInstruction dict or None.
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

    system_instruction = (
        {"parts": [{"text": "\n\n".join(system_parts)}]}
        if system_parts else None
    )
    return contents, system_instruction


class GeminiBackend:
    """LLMBackend for Google Gemini API (gemini-2.5-flash-lite)."""

    MODEL             = "gemini-2.5-flash-lite"
    supports_thinking = False
    TEMPERATURE       = 1.0
    _log_prefix       = "gemini"
    ctx_size          = 500_000   # gemini-2.5-flash-lite: 1M context; use 500k conservatively

    @property
    def max_chunk_chars(self) -> int:
        # Reserve space for system prompt, partial summary, and model output
        reserved = config.MAX_TOKENS + 4096
        return (self.ctx_size - reserved) * config.CHARS_PER_TOK

    def __init__(
        self,
        api_key:    str | None = None,
        timeout:    int        = 300,
        max_tokens: int        = config.MAX_TOKENS,
    ):
        self._api_key   = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._timeout   = timeout
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
        contents, system_instruction = _convert_messages(messages)

        body: dict = {
            "contents":        contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature":     temperature if temperature is not None else self.TEMPERATURE,
            },
        }
        if system_instruction:
            body["systemInstruction"] = system_instruction
        if tools:
            body["tools"] = [{"functionDeclarations": _convert_tools(tools)}]

        url = (
            f"{_GEMINI_BASE}/models/{self.MODEL}:streamGenerateContent"
            f"?alt=sse&key={self._api_key}"
        )

        resp = requests.post(url, json=body, stream=True, timeout=self._timeout)
        resp.raise_for_status()

        tc_list:    list[dict] = []
        t0                     = time.monotonic()
        ttft:       float      = 0.0
        token_count            = 0

        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                chunk      = json.loads(payload)
                candidates = chunk.get("candidates") or []
                if not candidates:
                    continue
                candidate = candidates[0]
                parts     = (candidate.get("content") or {}).get("parts") or []

                for part in parts:
                    if part.get("thought"):
                        thinking = part.get("text", "")
                        if thinking:
                            if not ttft:
                                ttft = time.monotonic() - t0
                            yield ThinkingEvent(thinking)

                    elif "text" in part:
                        text = part["text"]
                        if text:
                            if not ttft:
                                ttft = time.monotonic() - t0
                            token_count += 1
                            yield TokenEvent(text)

                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        tc_list.append({
                            "id":   f"gemini_{len(tc_list)}",
                            "type": "function",
                            "function": {
                                "name":      fc["name"],
                                "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                            },
                        })

            except (json.JSONDecodeError, KeyError):
                continue

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
