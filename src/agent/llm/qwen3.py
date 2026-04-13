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
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Generator

import requests

import config
from agent.llm.base import DoneEvent, LLMEvent, ThinkingEvent, TokenEvent, ToolCallsEvent


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

    def extract_thinking(self, raw_content: str) -> str:
        """Return concatenated contents of all <think>…</think> blocks."""
        blocks = _THINK_RE.findall(raw_content)
        return "\n---\n".join(b.strip() for b in blocks if b.strip())

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
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      True,
        }
        if tools:
            body["tools"] = tools

        import time as _time

        resp = requests.post(
            f"{self._url}/v1/chat/completions",
            json=body,
            stream=True,
            timeout=self._timeout,
        )
        resp.raise_for_status()

        # ── Optional raw dump ─────────────────────────────────────────────────
        _dump_fh = None
        if os.environ.get("KNESSET_LLM_DUMP"):
            _dump_dir = Path.home() / ".knesset_debug"
            _dump_dir.mkdir(exist_ok=True)
            _dump_path = _dump_dir / f"llm_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.txt"
            _dump_fh = _dump_path.open("w", encoding="utf-8")
            # Write request body first so we can correlate input↔output
            _dump_fh.write("=== REQUEST ===\n")
            _dump_fh.write(json.dumps(body, ensure_ascii=False, indent=2))
            _dump_fh.write("\n\n=== RESPONSE (raw SSE) ===\n")
            print(f"[qwen3] dumping to {_dump_path}", flush=True)

        tc_acc: dict[int, dict] = {}
        _t0            = _time.monotonic()
        _ttft: float   = 0.0
        _token_count   = 0

        try:
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if _dump_fh:
                    _dump_fh.write(line + "\n")
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    chunk  = json.loads(payload)
                    choice = chunk["choices"][0]
                    delta  = choice.get("delta", {})

                    thinking_text = delta.get("reasoning_content") or ""
                    if thinking_text:
                        if not _ttft:
                            _ttft = _time.monotonic() - _t0
                        yield ThinkingEvent(thinking_text)

                    text = delta.get("content") or ""
                    if text:
                        if not _ttft:
                            _ttft = _time.monotonic() - _t0
                        _token_count += 1
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
        finally:
            if _dump_fh:
                _dump_fh.close()

        _total = _time.monotonic() - _t0
        _gen   = _total - _ttft
        _tps   = _token_count / _gen if _gen > 0 else 0
        print(
            f"[qwen3] ttft={_ttft:.2f}s tokens={_token_count} "
            f"gen={_gen:.2f}s tps={_tps:.1f}",
            flush=True,
        )

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
