"""Interactive Gemini calls of the MK subjects pipeline: JSON answers, one retry with lower thinking when the
thinking runs out of output tokens, a thread pool, and token usage with its cost."""

import json
import time
from concurrent.futures import ThreadPoolExecutor

from google import genai
from google.genai import types

import config


def call_gemini(client: genai.Client, model: str, thinking_level: str, system_prompt: str, user_text: str,
                response_schema: dict) -> tuple[dict | None, dict]:
    """The parsed JSON (None when the call or the parsing failed) and the token usage, billed either way."""
    generation_config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        response_schema=response_schema,
        max_output_tokens=config.SUBJECTS_MAX_OUTPUT_TOKENS,
        thinking_config=types.ThinkingConfig(thinking_level=thinking_level) if thinking_level != "none" else None,
    )
    started = time.monotonic()
    try:
        response = client.models.generate_content(model=model, contents=user_text, config=generation_config)
    except Exception as exc:
        print(f"[error] Gemini call failed: {exc}")
        return None, {"seconds": round(time.monotonic() - started, 1), "finish_reason": "ERROR",
                      "input_tokens": 0, "output_tokens": 0, "thinking_tokens": 0}
    finish_reason = response.candidates[0].finish_reason.name if response.candidates else "?"
    usage = response.usage_metadata
    usage_stats = {
        "seconds":         round(time.monotonic() - started, 1),
        "finish_reason":   finish_reason,
        "input_tokens":    usage.prompt_token_count or 0,
        "output_tokens":   usage.candidates_token_count or 0,
        "thinking_tokens": usage.thoughts_token_count or 0,
    }
    try:
        return json.loads(response.text), usage_stats
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"[error] could not parse the response (finish={finish_reason}): {exc}")
        return None, usage_stats


class GeminiCaller:
    """One client, model and thinking level for a run. A high-thinking call that runs out of output tokens is
    retried once with config.SUBJECTS_FALLBACK_THINKING_LEVEL."""

    def __init__(self, client: genai.Client, model: str, thinking_level: str):
        self.client, self.model, self.thinking_level = client, model, thinking_level

    def call(self, system_prompt: str, user_text: str, response_schema: dict) -> tuple[dict | None, list[dict]]:
        result, usage = call_gemini(self.client, self.model, self.thinking_level, system_prompt, user_text,
                                    response_schema)
        fallback_level = config.SUBJECTS_FALLBACK_THINKING_LEVEL
        if result is not None or usage["finish_reason"] != "MAX_TOKENS" or self.thinking_level != "high":
            return result, [usage]
        print(f"  thinking {self.thinking_level} ran out of tokens ({usage}), retrying with {fallback_level}")
        retry_result, retry_usage = call_gemini(self.client, self.model, fallback_level, system_prompt, user_text,
                                                response_schema)
        return retry_result, [usage, retry_usage]


def run_parallel(function, items: list) -> list:
    with ThreadPoolExecutor(config.SUBJECTS_PARALLEL_CALLS) as pool:
        return list(pool.map(function, items))


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = config.GEMINI_INTERACTIVE_PRICE_PER_M_TOKENS.get(model, (0.0, 0.0))
    return (input_tokens * input_price + output_tokens * output_price) / 1e6


def total_usage(usage_list: list[dict], model: str) -> dict:
    input_tokens = sum(usage["input_tokens"] for usage in usage_list)
    output_tokens = sum(usage["output_tokens"] + usage["thinking_tokens"] for usage in usage_list)
    return {"calls": len(usage_list), "input_tokens": input_tokens, "output_tokens_incl_thinking": output_tokens,
            "cost_usd": round(cost_usd(model, input_tokens, output_tokens), 4)}
