"""
Input validation at the public API boundary. Every rejection is an ApiInputError, rendered as
400 {"error_code": "invalid_<param>", "message": ...} by the handlers installed on the app.
"""

import re
import unicodedata
from datetime import date

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import config

RESPONSE_FORMATS = ("json", "md")

_ASCII_DIGITS = re.compile(r"[0-9]+")
_MEETING_ID = re.compile(rf"{re.escape(config.PLENUM_MEETING_ID_PREFIX)}?[0-9]+")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ApiInputError(Exception):
    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def clean_text(value: str | None, name: str, max_chars: int) -> str:
    """Lone surrogates → 400; NFKC; format characters (bidi marks and overrides, zero-width, BOM) are
    dropped; control characters (incl. NUL) become spaces; whitespace runs collapse; longer than
    max_chars → 400. Combining marks (Hebrew niqqud) are kept."""
    text = value or ""
    if any(unicodedata.category(ch) == "Cs" for ch in text):
        raise ApiInputError(f"invalid_{name}", f"{name} is not valid Unicode text (lone surrogate)")
    normalized = unicodedata.normalize("NFKC", text)
    without_invisible = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in normalized
                                if unicodedata.category(ch) != "Cf")
    cleaned = " ".join(without_invisible.split())
    if len(cleaned) > max_chars:
        raise ApiInputError(f"invalid_{name}", f"{name} is longer than {max_chars} characters")
    return cleaned


_VERBATIM_KEPT_CONTROLS = frozenset("\n\t")


def verbatim_text(value: str | None, name: str, max_chars: int) -> str:
    """For text that must still match its source (a transcript excerpt): lone surrogates → 400; format
    characters and control characters other than newline and tab are dropped; no NFKC, whitespace kept
    as is; longer than max_chars → 400."""
    text = value or ""
    if any(unicodedata.category(ch) == "Cs" for ch in text):
        raise ApiInputError(f"invalid_{name}", f"{name} is not valid Unicode text (lone surrogate)")
    kept = "".join(ch for ch in text if ch in _VERBATIM_KEPT_CONTROLS or unicodedata.category(ch) not in ("Cc", "Cf"))
    if len(kept) > max_chars:
        raise ApiInputError(f"invalid_{name}", f"{name} is longer than {max_chars} characters")
    return kept


def search_text(value: str | None, max_chars: int | None = None) -> str:
    return clean_text(value, "query", max_chars or config.API_MAX_QUERY_CHARS)


def keyword_query(value: str | None, max_chars: int | None = None, max_words: int | None = None) -> str:
    """Free text for FTS5: capped in length and word count, and must contain a letter or digit."""
    query = search_text(value, max_chars)
    if not query:
        return query
    if not any(ch.isalnum() for ch in query):
        raise ApiInputError("invalid_query", "the search text has no searchable words (letters or digits)")
    max_words = max_words or config.API_MAX_QUERY_WORDS
    if len(query.split()) > max_words:
        raise ApiInputError("invalid_query", f"the search text has more than {max_words} words")
    return query


def name_filter(value: str | None, name: str, max_chars: int | None = None) -> str | None:
    return clean_text(value, name, max_chars or config.API_MAX_NAME_CHARS) or None


def numeric_id(value: str | None, name: str) -> str | None:
    """ASCII digits only (str.isdigit also accepts '²' and Arabic-Indic digits)."""
    if value is None or value == "":
        return None
    if not _ASCII_DIGITS.fullmatch(value) or len(value) > config.API_MAX_ID_DIGITS:
        raise ApiInputError(f"invalid_{name}", f"{name} must be a number of up to {config.API_MAX_ID_DIGITS} digits")
    return value


def meeting_id(value: str | None, name: str = "meeting_id") -> str | None:
    """A committee meeting id (ASCII digits) or a plenum session id ("p" + digits)."""
    if value is None or value == "":
        return None
    digits = value.removeprefix(config.PLENUM_MEETING_ID_PREFIX)
    if not _MEETING_ID.fullmatch(value) or len(digits) > config.API_MAX_ID_DIGITS:
        raise ApiInputError(f"invalid_{name}", f"{name} must be a number of up to {config.API_MAX_ID_DIGITS} digits, "
                                               f"or p + digits for a plenum session")
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


def protocol_knesset_num(value: int | None) -> int | None:
    """None (every processed Knesset) or one of config.PROTOCOL_KNESSET_NUMS."""
    if value is None or value in config.PROTOCOL_KNESSET_NUMS:
        return value
    processed = ", ".join(str(k) for k in config.PROTOCOL_KNESSET_NUMS)
    raise ApiInputError("invalid_knesset_num",
                        f"only Knesset {processed} protocols are processed; knesset_num must be one of them")


def offset(value: int, maximum: int | None = None) -> int:
    maximum = maximum or config.API_MAX_OFFSET
    if not 0 <= value <= maximum:
        raise ApiInputError("invalid_offset", f"offset must be between 0 and {maximum}")
    return value


def list_param(values: list[str], name: str, max_items: int | None = None) -> list[str]:
    max_items = max_items or config.API_MAX_LIST_ITEMS
    if len(values) > max_items:
        raise ApiInputError(f"invalid_{name}", f"at most {max_items} {name} values")
    return values


def one_of(value: str, allowed_values: tuple[str, ...], name: str) -> str:
    if value not in allowed_values:
        raise ApiInputError(f"invalid_{name}", f"{name} must be one of {', '.join(allowed_values)}")
    return value


def response_format(value: str) -> str:
    return one_of(value, RESPONSE_FORMATS, "format")


# ── JSON tool arguments (MCP sends typed JSON; the REST routes send strings and ints) ──

_SIGNED_INTEGER = re.compile(r"-?[0-9]{1,9}")
_BOOLEAN_WORDS = {"true": True, "1": True, "false": False, "0": False}


def as_text(value, name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ApiInputError(f"invalid_{name}", f"{name} must be a string")
    return str(value)


def as_int(value, name: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and _SIGNED_INTEGER.fullmatch(value.strip()):
        return int(value)
    raise ApiInputError(f"invalid_{name}", f"{name} must be an integer")


def as_bool(value, name: str) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _BOOLEAN_WORDS:
        return _BOOLEAN_WORDS[value.strip().lower()]
    raise ApiInputError(f"invalid_{name}", f"{name} must be true or false")


def as_text_list(value, name: str, split_commas: bool, max_items: int | None = None) -> list[str]:
    """A list or a single value; with split_commas, 'a,b' items become ['a', 'b']."""
    if value is None:
        return []
    items = [as_text(item, name) for item in (value if isinstance(value, list) else [value])]
    if split_commas:
        items = [part for item in items for part in item.split(",")]
    return list_param([item.strip() for item in items if item.strip()], name, max_items)


def _is_api_path(request: Request) -> bool:
    return request.url.path.startswith("/v1/")


async def _api_input_error(request: Request, exc: ApiInputError) -> JSONResponse:
    body = {"error_code": exc.error_code, "message": exc.message}
    if not _is_api_path(request):
        body["error"] = exc.message
    return JSONResponse(body, status_code=400)


async def _request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """400 without echoing the rejected input back; web routes also get the {"error": ...} key the UI reads."""
    problems = [f"{'.'.join(str(part) for part in error.get('loc', ())[1:])}: {error.get('msg', '')}"
                for error in exc.errors()]
    message = "; ".join(problems)[:500]
    body = {"error_code": "invalid_parameter", "message": message}
    if not _is_api_path(request):
        body["error"] = message
    return JSONResponse(body, status_code=400)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiInputError, _api_input_error)
    app.add_exception_handler(RequestValidationError, _request_validation_error)
