"""
tests/test_text_cleaning.py

Free-text input hygiene (api.validation.clean_text) on every text field of the public API and the
web routes: lone surrogates are a 400 (never a 500 from the UTF-8 encoder), invisible format
characters (bidi overrides, zero-width, BOM) are dropped after NFKC, Hebrew letters, niqqud,
geresh and gershayim survive. Also utils.tools.dispatch never raises on such arguments.
Real data: conftest.sample_db.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from agent.subgraph.evidence import ToolEnvelope
from api import validation as valid
from api.routes import validated_tool_args
from tests.conftest import ROLES, SAMPLE
from tests.test_api import client, network  # noqa: F401
from tests.test_web_gemini_key import KEY_HEADER, google_answers, web  # noqa: F401
from tests.test_web_security import load, save
from utils.tools import ToolSpec, dispatch

M2 = ROLES["M2"]
LONE_SURROGATE = "\ud800"


class TestCleanText:
    def test_lone_surrogate_is_rejected(self):
        with pytest.raises(valid.ApiInputError) as raised:
            valid.clean_text(f"abc {LONE_SURROGATE}", "query", 200)
        assert raised.value.error_code == "invalid_query"

    def test_format_characters_are_dropped(self):
        assert valid.clean_text("a‮b​﻿c", "query", 200) == "abc"
        assert valid.clean_text("‏ח\"כ‎ ⁦כהן⁩", "query", 200) == "ח\"כ כהן"

    @pytest.mark.parametrize("hebrew", [
        "שָׁלוֹם עֲלֵיכֶם",
        "ח\"כ",
        "צ׳ק",
        "רמטכ״ל",
        "ועדת החוקה, חוק ומשפט",
        "ה־30",
    ])
    def test_hebrew_is_unchanged(self, hebrew):
        assert valid.clean_text(hebrew, "query", 200) == hebrew

    def test_nfkc_folds_presentation_and_width_forms(self):
        assert valid.clean_text("ﬡﬢ", "query", 200) == "אד"
        assert valid.clean_text("ＡＢＣ１２", "query", 200) == "ABC12"

    def test_control_characters_still_become_spaces(self):
        assert valid.clean_text("a\x00b\tc\n d", "query", 200) == "a b c d"


class TestVerbatimText:
    def test_lone_surrogate_is_rejected(self):
        with pytest.raises(valid.ApiInputError) as raised:
            valid.verbatim_text(f"abc {LONE_SURROGATE}", "text", 200)
        assert raised.value.error_code == "invalid_text"

    def test_layout_and_forms_are_kept(self):
        text = "שורה  ראשונה\n\tשורה שנייה\r\nﬡ ＡＢＣ שָׁלוֹם"
        assert valid.verbatim_text(text, "text", 200) == "שורה  ראשונה\n\tשורה שנייה\nﬡ ＡＢＣ שָׁלוֹם"

    def test_format_and_other_control_characters_are_dropped(self):
        assert valid.verbatim_text("a\u202eb\u200b\ufeffc\x00d\x07", "text", 200) == "abcd"

    def test_length_is_capped(self):
        with pytest.raises(valid.ApiInputError):
            valid.verbatim_text("א" * 11, "text", 10)


class TestPublicApiText:
    @pytest.mark.parametrize("args,error_code", [
        ({"query": f"ביטחון {LONE_SURROGATE}"}, "invalid_query"),
        ({"query": "ביטחון", "party": LONE_SURROGATE}, "invalid_party"),
        ({"query": "ביטחון", "committees": [LONE_SURROGATE]}, "invalid_committee"),
    ])
    def test_tool_argument_surrogates_are_refused(self, args, error_code):
        with pytest.raises(valid.ApiInputError) as raised:
            validated_tool_args("query_protocols", args)
        assert raised.value.error_code == error_code

    def test_invisible_characters_do_not_change_results(self, client):
        plain = client.get("/v1/protocols", params={"q": "ביטחון"}).json()["results"]
        hidden = client.get("/v1/protocols", params={"q": "בי​טחון‮"}).json()["results"]
        assert plain == hidden and any(plain.values())


def post_json(web, url: str, body: dict, headers: dict | None = None):
    """ASCII JSON (lone surrogates as escapes), the way a JSON client sends them."""
    return web.client.post(url, content=json.dumps(body).encode("ascii"),
                           headers={"Content-Type": "application/json", **(headers or {})})


def browse(web, **body):
    return post_json(web, "/api/browse/search", body)


class TestWebText:
    @pytest.mark.parametrize("body,error_code", [
        ({"query": f"abc {LONE_SURROGATE}"}, "invalid_query"),
        ({"filters": {"parties": [LONE_SURROGATE]}}, "invalid_parties"),
        ({"filters": {"mks": [LONE_SURROGATE]}}, "invalid_mks"),
        ({"filters": {"committees": [LONE_SURROGATE]}}, "invalid_committees"),
        ({"filters": {"guest": LONE_SURROGATE}}, "invalid_guest"),
    ])
    def test_browse_surrogates_are_400(self, web, body, error_code):
        response = browse(web, **body)
        assert response.status_code == 400
        assert response.json()["error_code"] == error_code

    def test_browse_ignores_invisible_characters(self, web):
        plain = browse(web, query="העלייה").json()["meetings"]
        hidden = browse(web, query="הע‍לייה﻿").json()["meetings"]
        assert plain and [m["meeting_id"] for m in plain] == [m["meeting_id"] for m in hidden]

    def test_research_start_surrogate_is_400(self, web):
        response = post_json(web, "/api/research/start", {"question": f"מה {LONE_SURROGATE}"}, KEY_HEADER)
        assert response.status_code == 400

    def test_workspace_select_surrogate_is_400_and_not_stored(self, web):
        sid = save(web, status="done", workspace_data={"selected_chunks": []})
        response = post_json(web, f"/api/research/{sid}/workspace/select",
                             {"chunk_id": "3", "text": f"קטע {LONE_SURROGATE}", "source_meeting_id": M2})
        assert response.status_code == 400
        assert load(web, sid).workspace_data == {"selected_chunks": []}

    def test_workspace_select_drops_invisible_characters(self, web):
        sid = save(web, status="done", workspace_data={"selected_chunks": []})
        response = web.client.post(f"/api/research/{sid}/workspace/select",
                                   json={"chunk_id": "3", "text": "ק‮טע", "source_meeting_id": M2})
        assert response.status_code == 200
        assert load(web, sid).workspace_data["selected_chunks"][0]["text"] == "קטע"

    def test_workspace_select_keeps_a_real_transcript_chunk_verbatim(self, web):
        speeches = [row for row in SAMPLE["speeches"] if row["meeting_id"] == M2][:3]
        chunk = "\n".join(f"{row['speaker']}:\n\t{row['text']}" for row in speeches)
        sid = save(web, status="done", workspace_data={"selected_chunks": []})
        response = web.client.post(f"/api/research/{sid}/workspace/select",
                                   json={"chunk_id": "3", "text": chunk, "source_meeting_id": M2})
        assert response.status_code == 200
        assert load(web, sid).workspace_data["selected_chunks"][0]["text"] == chunk

    def test_workspace_ask_surrogate_is_400(self, web):
        sid = save(web, status="done")
        response = post_json(web, f"/api/research/{sid}/workspace/ask", {"question": f"מה {LONE_SURROGATE}"}, KEY_HEADER)
        assert response.status_code == 400


class TestDispatchNeverRaises:
    def test_surrogate_arguments_reach_the_handler(self):
        seen = []

        def echo(args):
            seen.append(args)
            return ToolEnvelope(summary="ok", full="", metadata={}, provenance={})

        registry = [ToolSpec(name="echo", schema={}, handler=echo)]
        envelope = dispatch(registry, "echo", {"query": f"x {LONE_SURROGATE}"})
        assert envelope.error is None
        assert seen == [{"query": f"x {LONE_SURROGATE}"}]
