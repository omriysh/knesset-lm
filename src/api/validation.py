"""
Input validation at the public API boundary. Every rejection is an ApiInputError, rendered as
400 {"error_code": "invalid_<param>", "message": ...} by the handlers installed on the app.
"""

import re
import unicodedata
from datetime import date

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import config

RESPONSE_FORMATS = ("json", "md")

_ASCII_DIGITS = re.compile(r"[0-9]+")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ApiInputError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def clean_text(value: str | None, name: str, max_chars: int) -> str:
    """Control characters (incl. NUL) become spaces, whitespace runs collapse; longer than max_chars → 400."""
    without_controls = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in value or "")
    cleaned = " ".join(without_controls.split())
    if len(cleaned) > max_chars:
        raise ApiInputError(f"invalid_{name}", f"{name} is longer than {max_chars} characters")
    return cleaned


def search_text(value: str | None) -> str:
    return clean_text(value, "query", config.API_MAX_QUERY_CHARS)


def keyword_query(value: str | None) -> str:
    """Free text for FTS5: capped in length and word count, and must contain a letter or digit."""
    query = search_text(value)
    if not query:
        return query
    if not any(ch.isalnum() for ch in query):
        raise ApiInputError("invalid_query", "q has no searchable words (letters or digits)")
    if len(query.split()) > config.API_MAX_QUERY_WORDS:
        raise ApiInputError("invalid_query", f"q has more than {config.API_MAX_QUERY_WORDS} words")
    return query


def name_filter(value: str | None, name: str) -> str | None:
    return clean_text(value, name, config.API_MAX_NAME_CHARS) or None


def numeric_id(value: str | None, name: str) -> str | None:
    """ASCII digits only (str.isdigit also accepts '²' and Arabic-Indic digits)."""
    if value is None or value == "":
        return None
    if not _ASCII_DIGITS.fullmatch(value) or len(value) > config.API_MAX_ID_DIGITS:
        raise ApiInputError(f"invalid_{name}", f"{name} must be a number of up to {config.API_MAX_ID_DIGITS} digits")
    return value


def iso_date(value: str | None, name: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        if not _ISO_DATE.fullmatch(value):
            raise ValueError("not YYYY-MM-DD")
        date.fromisoformat(value)
    except ValueError as exc:
        raise ApiInputError(f"invalid_{name}", f"{name} must be a date YYYY-MM-DD ({exc})") from exc
    return value


def knesset_num(value: int) -> int:
    low, high = config.API_KNESSET_NUM_RANGE
    if not low <= value <= high:
        raise ApiInputError("invalid_knesset_num", f"knesset_num must be between {low} and {high}")
    return value


def offset(value: int) -> int:
    if not 0 <= value <= config.API_MAX_OFFSET:
        raise ApiInputError("invalid_offset", f"offset must be between 0 and {config.API_MAX_OFFSET}")
    return value


def list_param(values: list[str], name: str) -> list[str]:
    if len(values) > config.API_MAX_LIST_ITEMS:
        raise ApiInputError(f"invalid_{name}", f"at most {config.API_MAX_LIST_ITEMS} {name} values")
    return values


def response_format(value: str) -> str:
    if value not in RESPONSE_FORMATS:
        raise ApiInputError("invalid_format", f"format must be one of {', '.join(RESPONSE_FORMATS)}")
    return value


def _is_api_path(request: Request) -> bool:
    return request.url.path.startswith("/v1/")


async def _api_input_error(request: Request, exc: ApiInputError) -> JSONResponse:
    return JSONResponse({"error_code": exc.error_code, "message": exc.message}, status_code=400)


async def _request_validation_error(request: Request, exc: RequestValidationError):
    if not _is_api_path(request):
        return await request_validation_exception_handler(request, exc)
    problems = [f"{'.'.join(str(part) for part in error.get('loc', ())[1:])}: {error.get('msg', '')}"
                for error in exc.errors()]
    return JSONResponse({"error_code": "invalid_parameter", "message": "; ".join(problems)[:500]}, status_code=400)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiInputError, _api_input_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
