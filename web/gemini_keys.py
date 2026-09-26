"""
Visitor Gemini keys for the auto-research tab.

The browser sends the key per request as the X-Gemini-Api-Key header. The server checks it with
one free Gemini models.list call before any LLM work (cached in memory by SHA-256 of the key),
passes it down to GoogleBackend, and never writes it anywhere.
"""

import hashlib
import json
import os
import re
import threading
import time

import requests
from fastapi import Request
from fastapi.responses import JSONResponse

import config

GEMINI_KEY_HEADER = "X-Gemini-Api-Key"
GEMINI_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{30,100}$")
GEMINI_KEY_REJECTED_PATTERN = re.compile(
    r"API_KEY_INVALID|API key not valid|API key expired|API key was reported as leaked", re.IGNORECASE)
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_KEY_REJECTED_STATUSES = (400, 401, 403)
GEMINI_KEY_REJECTED_MESSAGE = "מפתח ה-Gemini נדחה על ידי Google. הזינו מפתח תקין ושאלו שוב."

_verdict_by_key_hash: dict[str, tuple[bool, float]] = {}
_verdict_lock = threading.Lock()


def forget_server_gemini_keys() -> None:
    if not config.WEB_REQUIRE_USER_GEMINI_KEY:
        return
    for name in (config.GOOGLE_API_KEY_ENV, "GEMINI_API_KEY"):
        if os.environ.pop(name, None) is not None:
            print(f"[web] ignoring server {name}: agent requests use the visitor's Gemini key", flush=True)


def gemini_key_from_request(request: Request) -> str | None:
    key = request.headers.get(GEMINI_KEY_HEADER, "").strip()
    return key if GEMINI_KEY_PATTERN.match(key) else None


def forget_gemini_key_verdicts() -> None:
    with _verdict_lock:
        _verdict_by_key_hash.clear()


def ask_google_whether_key_is_valid(key: str) -> bool | None:
    """True/False from Google's answer; None when Google could not be asked."""
    try:
        response = requests.get(GEMINI_MODELS_URL, headers={"x-goog-api-key": key}, params={"pageSize": 1},
                                timeout=config.GEMINI_KEY_CHECK_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        print(f"[gemini_keys] key check request failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    if response.status_code == 200:
        return True
    if response.status_code in GEMINI_KEY_REJECTED_STATUSES:
        return False
    print(f"[gemini_keys] key check got unexpected status {response.status_code}", flush=True)
    return None


def gemini_key_is_valid(key: str) -> bool | None:
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    now = time.monotonic()
    with _verdict_lock:
        cached = _verdict_by_key_hash.get(key_hash)
    if cached and cached[1] > now:
        return cached[0]
    is_valid = ask_google_whether_key_is_valid(key)
    if is_valid is not None:
        with _verdict_lock:
            if len(_verdict_by_key_hash) >= config.GEMINI_KEY_CHECK_CACHE_MAX_ENTRIES:
                _verdict_by_key_hash.clear()
            _verdict_by_key_hash[key_hash] = (is_valid, now + config.GEMINI_KEY_CHECK_CACHE_SECONDS)
    return is_valid


def key_error_response(error_code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": error_code, "message": message}, status_code=status_code)


def visitor_gemini_key_or_error(request: Request) -> tuple[str | None, JSONResponse | None]:
    """Blocking (may call Google): run it in a worker thread from async routes."""
    key = gemini_key_from_request(request)
    if key is None:
        if not config.WEB_REQUIRE_USER_GEMINI_KEY:
            return None, None
        return None, key_error_response("gemini_key_required",
                                        "נדרש מפתח Gemini API תקין (כותרת X-Gemini-Api-Key)", 401)
    is_valid = gemini_key_is_valid(key)
    if is_valid is False:
        return None, key_error_response("gemini_key_invalid", GEMINI_KEY_REJECTED_MESSAGE, 401)
    if is_valid is None:
        return None, key_error_response("gemini_key_unverified",
                                        "לא ניתן לאמת את מפתח ה-Gemini כרגע. נסו שוב בעוד רגע.", 503)
    return key, None


def stop_on_rejected_gemini_key(events):
    """Relay runner events; when Gemini rejects the key mid-run, flag it and stop the run there."""
    for event in events:
        yield event
        if GEMINI_KEY_REJECTED_PATTERN.search(json.dumps(event, ensure_ascii=False, default=str)):
            yield ("gemini_key_invalid", {})
            yield ("error", GEMINI_KEY_REJECTED_MESSAGE)
            try:
                events.close()
            except Exception as exc:
                print(f"[gemini_keys] closing the stopped run failed: {type(exc).__name__}: {exc}", flush=True)
            return
