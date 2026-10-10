"""
tests/test_source_links.py

Links to the reading tab (utils.source_links, web/static/url_state.js):
  - protocol_url: a meeting, a speech, a character range in the speech (a range needs its speech);
  - the speech list the page shows (utils.meeting.format_meeting_chunks) is the one knesset.db stores, so a
    speech_idx and a quote_offset/quote_length range point at the same text on the page;
  - /v1/protocols rows carry `url` instead of the raw quote location;
  - /protocols and /research serve the page; browse search opens a linked meeting by meeting_ids.
Real data: Data/knesset.db and the transcripts under Data/raw_transcriptions.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import config
from summarization.output_parsing import normalize_for_match
from utils.meeting import format_meeting_chunks, get_transcript_path_from_id, load_meeting
from utils.source_links import protocol_url

SAMPLE_MEETINGS_PER_FORMAT = 15


def _link_params(url: str) -> dict:
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == config.PUBLIC_SITE_URL
    assert parts.path == config.PROTOCOLS_PAGE_PATH
    return {key: values[0] for key, values in parse_qs(parts.query).items()}


class TestProtocolUrl:
    def test_meeting(self):
        assert _link_params(protocol_url("2245272")) == {"meeting": "2245272"}

    def test_speech(self):
        assert _link_params(protocol_url("p2245272", 7)) == {"meeting": "p2245272", "speech": "7"}

    def test_quote_range(self):
        assert _link_params(protocol_url("2245272", 7, 0, 42)) == {
            "meeting": "2245272", "speech": "7", "offset": "0", "length": "42"}

    def test_range_without_speech_is_dropped(self):
        assert _link_params(protocol_url("2245272", None, 10, 42)) == {"meeting": "2245272"}

    def test_offset_without_length_is_dropped(self):
        assert _link_params(protocol_url("2245272", 3, 10, None)) == {"meeting": "2245272", "speech": "3"}


def _sample_meeting_ids(real_conn, meeting_format: str) -> list[str]:
    return [row[0] for row in real_conn.execute(
        "SELECT m.meeting_id FROM meetings m WHERE m.knesset_num = 25 AND m.format = ? "
        "AND EXISTS (SELECT 1 FROM speeches s WHERE s.meeting_id = m.meeting_id) "
        "ORDER BY m.meeting_id LIMIT ?", (meeting_format, SAMPLE_MEETINGS_PER_FORMAT))]


def _page_speeches(meeting_id: str) -> dict[int, str]:
    path = get_transcript_path_from_id(meeting_id)
    assert path and path.exists(), meeting_id
    return {int(chunk["chunk_id"]): chunk["text"] for chunk in format_meeting_chunks(load_meeting(path))}


class TestPageAndDbShareSpeeches:
    @pytest.mark.parametrize("meeting_format", ["structured", "full_text"])
    def test_stored_speech_is_the_page_speech(self, real_conn, meeting_format):
        meeting_ids = _sample_meeting_ids(real_conn, meeting_format)
        if not meeting_ids:
            pytest.skip(f"no {meeting_format} meetings with speeches")
        for meeting_id in meeting_ids:
            page = _page_speeches(meeting_id)
            for row in real_conn.execute("SELECT idx, text FROM speeches WHERE meeting_id = ?", (meeting_id,)):
                assert page.get(row["idx"]) == row["text"], (meeting_id, row["idx"])

    def test_quote_range_is_the_quote_on_the_page(self, real_conn):
        rows = real_conn.execute(
            "SELECT meeting_id, speech_idx, quote_offset, quote_length, quote FROM opinions "
            "WHERE knesset_num = 25 AND quote_length IS NOT NULL ORDER BY meeting_id, idx LIMIT 300").fetchall()
        if not rows:
            pytest.skip("knesset.db built before quote_length; rebuild the summaries target")
        page_by_meeting: dict[str, dict[int, str]] = {}
        for row in rows:
            page = page_by_meeting.setdefault(row["meeting_id"], _page_speeches(row["meeting_id"]))
            shown = page[row["speech_idx"]][row["quote_offset"]:row["quote_offset"] + row["quote_length"]]
            assert normalize_for_match(shown) == normalize_for_match(row["quote"]), dict(row)


class TestApiRowUrls:
    def _protocols(self, client, real_db, scope: str) -> list[dict]:
        response = client.get("/v1/protocols", params={"meeting_id": real_db.meeting_id, "search_in": scope})
        assert response.status_code == 200, response.text
        rows = response.json()["results"][scope]
        assert rows
        return rows

    def test_topic_links_the_meeting(self, client, real_db):
        for row in self._protocols(client, real_db, "topics"):
            assert _link_params(row["url"]) == {"meeting": real_db.meeting_id}

    def test_speech_links_the_speech(self, client, real_db):
        for row in self._protocols(client, real_db, "speeches"):
            assert _link_params(row["url"]) == {"meeting": real_db.meeting_id, "speech": str(row["speech_idx"])}

    def test_opinion_links_the_quote(self, client, real_db, real_conn):
        located = {row["idx"]: row for row in real_conn.execute(
            "SELECT idx, speech_idx, quote_offset, quote_length FROM opinions WHERE meeting_id = ?",
            (real_db.meeting_id,))}
        for row in self._protocols(client, real_db, "opinions"):
            assert "quote_offset" not in row and "quote_length" not in row
            expected = located[row["idx"]]
            params = _link_params(row["url"])
            if expected["speech_idx"] is None:
                assert params == {"meeting": real_db.meeting_id}
            elif expected["quote_length"]:
                assert params == {"meeting": real_db.meeting_id, "speech": str(expected["speech_idx"]),
                                  "offset": str(expected["quote_offset"]),
                                  "length": str(expected["quote_length"])}
            else:
                assert params == {"meeting": real_db.meeting_id, "speech": str(expected["speech_idx"])}

    def test_attendance_links_the_meeting(self, client, real_db):
        response = client.get(f"/v1/meetings/{real_db.meeting_id}/attendance")
        assert response.status_code == 200, response.text
        assert _link_params(response.json()["results"]["url"]) == {"meeting": real_db.meeting_id}

    def test_markdown_rows_link_their_source(self, client, real_db):
        response = client.get("/v1/protocols", params={"meeting_id": real_db.meeting_id, "search_in": "topics",
                                                       "format": "md"})
        assert response.status_code == 200
        assert f"([source]({config.PUBLIC_SITE_URL}{config.PROTOCOLS_PAGE_PATH}?meeting={real_db.meeting_id}))" \
            in response.text


@pytest.fixture()
def web_client(real_db, tmp_path):
    import web.app as webapp
    import web.settings as settings
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = sessions
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    return TestClient(webapp.app)


class TestWebPages:
    @pytest.mark.parametrize("path", ["/", config.CHAT_PAGE_PATH, config.RESEARCH_PAGE_PATH, config.PROTOCOLS_PAGE_PATH])
    def test_every_tab_path_serves_the_page(self, web_client, path):
        response = web_client.get(path, params={"meeting": "123", "speech": "4"})
        assert response.status_code == 200
        assert "/static/url_state.js" in response.text

    @pytest.mark.parametrize("tab", ["", *config.ABOUT_SUBPAGE_TABS])
    def test_every_about_path_serves_the_page_with_the_legal_texts(self, web_client, tab):
        response = web_client.get(f"{config.ABOUT_PAGE_PATH}/{tab}".rstrip("/"))
        assert response.status_code == 200
        for text in ("מדיניות פרטיות", "Privacy policy", "Terms of use", "omri@meorav.com", "https://meorav.com/mcp"):
            assert text in response.text

    def test_about_title_follows_tab_and_language(self, web_client):
        assert "<title>תנאי שימוש · מעורב ירושלמי</title>" in web_client.get(f"{config.ABOUT_PAGE_PATH}/terms").text
        english = web_client.get(f"{config.ABOUT_PAGE_PATH}/privacy", params={"lang": "en"}).text
        assert "<title>Privacy policy · Meorav Yerushalmi</title>" in english
        assert '<article class="prose-content" lang="en" dir="ltr">' in english

    def test_unknown_about_tab_is_404(self, web_client):
        assert web_client.get(f"{config.ABOUT_PAGE_PATH}/nope").status_code == 404

    @pytest.mark.parametrize("path", [config.CHAT_PAGE_PATH, config.RESEARCH_PAGE_PATH, config.PROTOCOLS_PAGE_PATH])
    def test_mcp_host_does_not_serve_the_page(self, web_client, path):
        assert web_client.get(path, headers={"Host": "mcp.meorav.com"}).status_code == 404


class TestBrowseByMeetingId:
    def test_linked_meeting_opens_alone(self, web_client, real_db):
        response = web_client.post("/api/browse/search",
                                   json={"query": "", "filters": {"meeting_ids": [real_db.meeting_id]}})
        assert response.status_code == 200, response.text
        body = response.json()
        assert [m["meeting_id"] for m in body["meetings"]] == [real_db.meeting_id]
        assert body["session_id"]

    def test_other_filters_still_apply(self, web_client, real_db):
        response = web_client.post("/api/browse/search", json={"query": "", "filters": {
            "meeting_ids": [real_db.meeting_id], "committees": [real_db.other_committee]}})
        assert response.status_code == 200
        assert response.json()["meetings"] == []

    def test_invalid_meeting_id_is_rejected(self, web_client, real_db):
        response = web_client.post("/api/browse/search",
                                   json={"query": "", "filters": {"meeting_ids": ["1; DROP TABLE"]}})
        assert response.status_code == 400
        assert response.json()["error_code"] == "invalid_meeting_ids"
