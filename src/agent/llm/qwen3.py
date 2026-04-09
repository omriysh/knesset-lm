"""
qwen3.py

Qwen3 / llama-server implementation of LLMBackend.

All Qwen3-specific behaviour is isolated here:
  - Appending "/no_think" to suppress the extended-thinking chain
  - Stripping <think>…</think> blocks from content
  - Retrying when the model produces only a thinking block (no visible output)
  - Parsing XML <tool_call> fallback emitted by llama.cpp in some configurations
"""

from __future__ import annotations

import json
import re
from typing import Generator

import requests

import config
from agent.llm.base import DoneEvent, LLMEvent, TokenEvent, ToolCallsEvent


_XML_TOOL_RE = re.compile(
    r"<tool_call>\s*<function=(\w+)>(.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class Qwen3LlamaBackend:
    """
    LLMBackend implementation for Qwen3 served by llama-server
    (OpenAI-compatible /v1/chat/completions endpoint).
    """

    supports_thinking = True

    def __init__(
        self,
        url: str = config.LLAMA_SERVER,
        timeout: int = 300,
    ):
        self._url     = url.rstrip("/")
        self._timeout = timeout

    # ── Protocol methods ───────────────────────────────────────────────────────

    def prepare_messages(
        self,
        messages: list[dict],
        suppress_thinking: bool = True,
    ) -> list[dict]:
        """Append '/no_think' to the last user message when suppress_thinking=True."""
        if not suppress_thinking or not messages:
            return messages
        out = list(messages)
        last = out[-1]
        if last.get("role") == "user":
            out[-1] = {**last, "content": (last.get("content") or "") + " /no_think"}
        return out

    def extract_visible_content(self, raw_content: str) -> str:
        """Strip <think>…</think> blocks; return the remainder stripped."""
        return _THINK_RE.sub("", raw_content).strip()

    def extract_tool_calls(
        self,
        message: dict,
        raw_content: str,
    ) -> tuple[list[dict], str]:
        """
        Primary: use message["tool_calls"] if present.
        Fallback: parse XML <tool_call> tags from raw_content (llama.cpp quirk).
        Returns (tool_calls, cleaned_content).
        """
        tc = message.get("tool_calls") or []
        if tc:
            return tc, self.extract_visible_content(raw_content)

        xml_calls = self._parse_xml_tool_calls(raw_content)
        if xml_calls:
            # Remove the XML blocks from visible content
            cleaned = _XML_TOOL_RE.sub("", raw_content).strip()
            return xml_calls, self.extract_visible_content(cleaned)

        return [], self.extract_visible_content(raw_content)

    def needs_thinking_retry(self, content: str, tool_calls: list) -> bool:
        """True when model produced only a <think> block and no tool calls."""
        return not content.strip() and not tool_calls

    def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 16384,
    ) -> Generator[LLMEvent, None, None]:
        """
        Stream one completion from llama-server.

        Yields TokenEvent per content token, then ToolCallsEvent if any tool
        calls were accumulated, then DoneEvent.
        """
        body: dict = {
            "messages":    self.prepare_messages(messages),
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      True,
        }
        if tools:
            body["tools"] = tools

        resp = requests.post(
            f"{self._url}/v1/chat/completions",
            json=body,
            stream=True,
            timeout=self._timeout,
        )
        resp.raise_for_status()

        tc_acc: dict[int, dict] = {}

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
                chunk  = json.loads(payload)
                choice = chunk["choices"][0]
                delta  = choice.get("delta", {})

                text = delta.get("content") or ""
                if text:
                    yield TokenEvent(text)

                for tcd in delta.get("tool_calls") or []:
                    idx = tcd.get("index", 0)
                    if idx not in tc_acc:
                        tc_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    tc = tc_acc[idx]
                    if tcd.get("id"):
                        tc["id"] = tcd["id"]
                    fn = tcd.get("function", {})
                    tc["name"]      += fn.get("name",      "")
                    tc["arguments"] += fn.get("arguments", "")
            except (json.JSONDecodeError, KeyError):
                continue

        if tc_acc:
            yield ToolCallsEvent([
                {
                    "id":   tc["id"] or f"tc_{i}",
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for i, tc in sorted(tc_acc.items())
            ])
        yield DoneEvent()

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_xml_tool_calls(content: str) -> list[dict]:
        """
        Parse llama.cpp's occasional <tool_call><function=name>…</function></tool_call>
        format into the standard OpenAI tool_calls list.
        """
        calls = []
        for i, m in enumerate(_XML_TOOL_RE.finditer(content)):
            fn_name  = m.group(1)
            fn_body  = m.group(2).strip()
            # Try to parse the body as JSON; fall back to a single-key object
            try:
                args = json.loads(fn_body)
            except json.JSONDecodeError:
                # Try extracting <parameter=key>value</parameter> pattern
                params = re.findall(
                    r"<parameter=(\w+)>(.*?)</parameter>", fn_body, re.DOTALL
                )
                args = {k: v.strip() for k, v in params} if params else {"input": fn_body}
            calls.append({
                "id":   f"xml_{i}",
                "type": "function",
                "function": {
                    "name":      fn_name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
        return calls
