"""
tests/test_protocol_download.py

utils/protocol_download.py: file naming shared by the downloader, the summarizer and the
database builder, the skip / failure bookkeeping of a download run (with the Knesset API
replaced by recorded answers), and the live plenum document listing (network).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

import config
import utils.protocol_download as download


def test_safe_dirname_collapses_whitespace_and_unsafe_characters():
    assert download.safe_dirname("ועדת  החוקה, חוק ומשפט ") == "ועדת_החוקה,_חוק_ומשפט"
    assert download.safe_dirname('הוועדה המיוחדת לתיקונים לחוק יסוד: הממשלה') == "הוועדה_המיוחדת_לתיקונים_לחוק_יסוד_הממשלה"
    assert download.safe_dirname(config.PLENUM_COMMITTEE_NAME) == "מליאת_הכנסת"


def test_plenum_file_names_round_trip_to_the_prefixed_meeting_id():
    meeting_id = download.plenum_meeting_id(2245272)
    stem = download.meeting_file_stem("2026-07-28", meeting_id)
    assert (meeting_id, stem) == ("p2245272", "28_07_2026_p2245272")
    assert download.meeting_id_from_stem(stem) == "p2245272"


@pytest.fixture()
def fake_api(tmp_path, monkeypatch):
    """Two committee sessions (one already on disk under a renamed folder) and two plenum sessions."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    renamed = tmp_path / "raw_transcriptions" / "25" / "ועדה_בשם_קודם" / "01_01_2025_101.json"
    renamed.parent.mkdir(parents=True)
    renamed.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(download, "get_all_committees", lambda knesset_num: [{"CommitteeID": 1, "Name": "ועדה  חדשה"}])
    monkeypatch.setattr(download, "get_committee_sessions", lambda committee_id, knesset_num: [
        {"session_id": 101, "date": "2025-01-01", "type_id": None, "status_id": None},
        {"session_id": 102, "date": "2025-01-02", "type_id": None, "status_id": None},
        {"session_id": 103, "date": "2025-01-03", "type_id": download.SESSION_TYPE_CLASSIFIED, "status_id": None},
    ])
    monkeypatch.setattr(download, "get_session_transcript",
                        lambda session_id: {"full_text": 'היו"ר א:\nשלום.', "source_url": "u"})
    monkeypatch.setattr(download, "get_plenum_protocol_documents", lambda knesset_num: [
        {"session_id": 7, "session_number": 1, "date": "2026-07-08", "document_id": 70, "format": "DOC",
         "url": "https://fs.knesset.gov.il/25/Plenum/25_ptm_70.doc", "updated": "2026-07-08"},
        {"session_id": 8, "session_number": 2, "date": "2026-07-09", "document_id": 80, "format": "DOC",
         "url": "https://fs.knesset.gov.il/25/Plenum/25_ptm_80.doc", "updated": "2026-07-09"},
    ])

    def plenum_transcript(document):
        if document["session_id"] == 8:
            raise ConnectionError("download timed out")
        return {"full_text": 'היו"ר אמיר אוחנה:\nשלום.', "source_url": document["url"]}
    monkeypatch.setattr(download, "get_plenum_session_transcript", plenum_transcript)
    return tmp_path


def test_download_skips_meetings_on_disk_and_reports_failures(fake_api):
    totals = download.download_missing_protocols(25)
    assert {key: totals[key] for key in ("downloaded", "on_disk", "classified", "failed")} == \
        {"downloaded": 2, "on_disk": 1, "classified": 1, "failed": 1}
    assert len(totals["failures"]) == 1 and "p8" in totals["failures"][0]

    root = fake_api / "raw_transcriptions" / "25"
    assert (root / "ועדה_חדשה" / "02_01_2025_102.json").exists()
    plenum = json.loads((root / "מליאת_הכנסת" / "08_07_2026_p7.json").read_text(encoding="utf-8"))
    assert plenum["meeting_id"] == "p7" and plenum["committee"] == config.PLENUM_COMMITTEE_NAME
    assert plenum["full_text"].startswith('היו"ר אמיר אוחנה:')


def test_second_run_downloads_only_what_failed(fake_api, monkeypatch):
    download.download_missing_protocols(25)
    monkeypatch.setattr(download, "get_plenum_session_transcript",
                        lambda document: {"full_text": "טקסט", "source_url": document["url"]})
    totals = download.download_missing_protocols(25)
    assert (totals["downloaded"], totals["failed"], totals["failures"]) == (1, 0, [])


@pytest.mark.network
def test_live_plenum_document_listing_has_one_protocol_per_session():
    from utils.knesset_db import get_plenum_protocol_documents
    documents = get_plenum_protocol_documents(25)
    assert len(documents) >= 418
    assert len({d["session_id"] for d in documents}) == len(documents)
    assert all(d["date"] and d["url"].startswith("https://fs.knesset.gov.il/25/Plenum/") for d in documents)
