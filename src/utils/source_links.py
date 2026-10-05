"""Links to the reading tab (web/static/url_state.js reads the same parameters)."""

from urllib.parse import urlencode

import config


def protocol_url(meeting_id: str, speech_idx: int | None = None, offset: int | None = None,
                 length: int | None = None) -> str:
    """A meeting, one of its speeches, or a character range in that speech; a range needs the speech."""
    params = {"meeting": meeting_id}
    if speech_idx is not None:
        params["speech"] = speech_idx
        if offset is not None and length:
            params["offset"] = offset
            params["length"] = length
    return f"{config.PUBLIC_SITE_URL}{config.PROTOCOLS_PAGE_PATH}?{urlencode(params)}"


def protocol_row_url(scope: str, row: dict) -> str:
    """Topics link to the meeting, speeches to the speech, opinions to the quote (or its speech, or the meeting)."""
    if scope == "speeches":
        return protocol_url(row["meeting_id"], row.get("speech_idx"))
    if scope == "opinions":
        return protocol_url(row["meeting_id"], row.get("speech_idx"), row.get("quote_offset"), row.get("quote_length"))
    return protocol_url(row["meeting_id"])
