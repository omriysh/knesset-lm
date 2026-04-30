"""
google.py

GoogleBackend — LLMBackend implementation for Google's Gemini / Gemma cloud
models via the google.genai SDK.

Auth: reads API key from `os.environ.get(config.GOOGLE_API_KEY_ENV)` first,
then falls back to `os.environ.get("GEMINI_API_KEY", "")` for backwards
compat with environments still using the old variable name.

Retry / fallback policy (see plan-and-execute design §11):
  - HTTP 429:  one retry with 2 s wait, then fall back.
  - HTTP 5xx / connection errors: 3 retries with 2 s / 5 s / 15 s backoff,
                                  then fall back.
  - Fallback: if `config.GOOGLE_API_FALLBACK_TO_LOCAL` is True, delegate
              `stream()` to `LlamaServerBackend` and mark the final
              `DoneEvent.cloud_failed_used_local = True`.
              Otherwise re-raise the last exception.
"""

from __future__ import annotations

import json
import os
import time
from typing import Generator

import requests
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
    "gemma-4-31b-it":        16_000,
    "gemma-4-12b-it":        16_000,
}
_CTX_SIZE_DEFAULT = 500_000


def _extract_status_code(exc: Exception) -> int | None:
    """
    Best-effort extraction of an HTTP status code from a google.genai or
    requests-style exception. Returns None when no code is available
    (e.g. plain connection error).
    """
    for attr in ("status_code", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        val = getattr(response, "status_code", None)
        if isinstance(val, int):
            return val
    # google.genai sometimes embeds {"error": {"code": 429}} in str(exc)
    msg = str(exc)
    for code in (429, 500, 502, 503, 504):
        if f" {code}" in msg or f"code: {code}" in msg or f"code={code}" in msg:
            return code
    return None


def _is_connection_error(exc: Exception) -> bool:
    """True for transport-level errors that warrant retry like a 5xx."""
    return isinstance(exc, (
        requests.ConnectionError,
        requests.Timeout,
        ConnectionError,
        TimeoutError,
    ))


def _model_supports_thinking(model: str) -> bool:
    """True for Gemini 2.5+ models that can emit thought tokens."""
    return model.startswith("gemini-2.5")


class GoogleBackend:
    """LLMBackend for Google Gemini / Gemma cloud models via google.genai SDK."""

    MODEL             = "gemini-2.5-flash-lite"
    TEMPERATURE       = 1.0
    _log_prefix       = "google"

    @property
    def supports_thinking(self) -> bool:
        return _model_supports_thinking(self._model)

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
        env_key = os.environ.get(config.GOOGLE_API_KEY_ENV) or os.environ.get("GEMINI_API_KEY", "")
        api_key          = api_key or env_key
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
        """
        Stream one completion from Google's API with retry + local fallback.

        Retries are applied around the *initial connection* to the API. If a
        stream is interrupted mid-flight after tokens have already been
        yielded, we do not retry — the partial output has already been
        consumed downstream.
        """
        last_exc: Exception | None = None

        # Backoff schedule: index 0 = first retry wait, etc.
        # 429 path uses [2.0]; 5xx/connection path uses [2.0, 5.0, 15.0].
        # We try the request once before any sleeps.
        for attempt in range(4):  # 1 initial + up to 3 retries
            try:
                yield from self._stream_once(
                    messages    = messages,
                    tools       = tools,
                    temperature = temperature,
                    max_tokens  = max_tokens,
                )
                return
            except Exception as exc:  # noqa: BLE001 — categorize below
                last_exc = exc
                status   = _extract_status_code(exc)

                if status == 429:
                    # 429: at most one retry, 2 s wait, then fall back.
                    if attempt >= 1:
                        break
                    print(f"[{self._log_prefix}] 429 quota — retrying in 2.0s", flush=True)
                    time.sleep(2.0)
                    continue

                if (status is not None and 500 <= status < 600) or _is_connection_error(exc):
                    # 5xx / connection: up to 3 retries with 2 / 5 / 15 s backoff.
                    backoff = (2.0, 5.0, 15.0)
                    if attempt >= 3:
                        break
                    wait = backoff[attempt]
                    label = f"{status}" if status is not None else type(exc).__name__
                    print(
                        f"[{self._log_prefix}] {label} — retrying in {wait:.0f}s "
                        f"(attempt {attempt + 1}/3)",
                        flush=True,
                    )
                    time.sleep(wait)
                    continue

                # Anything else: don't retry.
                raise

        # Retries exhausted — fall back or re-raise.
        if config.GOOGLE_API_FALLBACK_TO_LOCAL:
            print(
                f"[{self._log_prefix}] retries exhausted ({type(last_exc).__name__}: "
                f"{last_exc}); falling back to llama-server",
                flush=True,
            )
            yield from self._stream_local_fallback(
                messages    = messages,
                tools       = tools,
                temperature = temperature,
                max_tokens  = max_tokens,
            )
            return

        assert last_exc is not None
        raise last_exc

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _stream_once(
        self,
        messages:    list[dict],
        tools:       list[dict] | None,
        temperature: float | None,
        max_tokens:  int,
    ) -> Generator[LLMEvent, None, None]:
        """One attempt at the Google API stream, no retry logic."""
        contents, system_text = _convert_messages(messages)

        thinking_config = None
        if self.supports_thinking:
            try:
                thinking_config = types.ThinkingConfig(
                    thinking_budget=config.MAX_THINKING_TOKENS
                )
            except Exception:  # noqa: BLE001 — SDK version may not support it
                pass

        gen_config = types.GenerateContentConfig(
            max_output_tokens  = max_tokens,
            temperature        = temperature if temperature is not None else self.TEMPERATURE,
            system_instruction = system_text,
            tools              = _convert_tools_genai(tools) if tools else None,
            thinking_config    = thinking_config,
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

    def _stream_local_fallback(
        self,
        messages:    list[dict],
        tools:       list[dict] | None,
        temperature: float | None,
        max_tokens:  int,
    ) -> Generator[LLMEvent, None, None]:
        """Delegate to LlamaServerBackend and mark the final DoneEvent."""
        # Imported lazily to avoid a hard dependency at module load.
        from agent.llm.llama_server import LlamaServerBackend

        local = LlamaServerBackend()
        for event in local.stream(
            messages    = messages,
            tools       = tools,
            temperature = temperature,
            max_tokens  = max_tokens,
        ):
            if isinstance(event, DoneEvent):
                event.cloud_failed_used_local = True
            yield event
