"""
tests/test_knesset_db_roster.py

Offline tests for the Knesset roster assembly in utils/knesset_db.py.
The oknesset /members payload ships ``factions: [null]`` in production, so the
official OData positions table has to carry the roster on its own.
"""

import pytest

from utils import knesset_db


OKNESSET_MEMBER = {
    "mk_individual_id": 7,
    "mk_individual_first_name": "אביחי אברהם",
    "mk_individual_name": "בוארון",
    "PersonID": 30880,
    "IsCurrent": True,
    "altnames": ["אביחי אברהם בוארון"],
    "committee_positions": [None],
    "factions": [None],
    "faction_chairpersons": [None],
    "govministries": [None],
}

ODATA_POSITION_ROWS = (
    {
        "PersonID": 30880, "PositionID": 54, "KnessetNum": 25,
        "FactionID": 1090, "FactionName": "הליכוד",
        "StartDate": "2023-01-01T00:00:00+02:00", "FinishDate": None,
        "KNS_Person": {"Id": 30880, "FirstName": "אביחי אברהם", "LastName": "בוארון",
                       "IsCurrent": True, "Email": None},
    },
    {
        "PersonID": 30880, "PositionID": 42, "KnessetNum": 25,
        "FactionID": 1090, "FactionName": "הליכוד",
        "StartDate": "2023-01-01T00:00:00+02:00", "FinishDate": None,
        "KNS_Person": {"Id": 30880, "FirstName": "אביחי אברהם", "LastName": "בוארון",
                       "IsCurrent": True, "Email": None},
    },
    {
        "PersonID": 31001, "PositionID": 39, "KnessetNum": 25,
        "FactionID": None, "FactionName": None,
        "StartDate": "2023-02-01T00:00:00+02:00", "FinishDate": None,
        "KNS_Person": {"Id": 31001, "FirstName": "רון", "LastName": "דרמר",
                       "IsCurrent": False, "Email": None},
    },
)


@pytest.fixture()
def patched_sources(monkeypatch):
    """Replace both network sources with in-memory fixtures."""
    def install(members, position_rows):
        monkeypatch.setattr(
            knesset_db, "_fetch_members",
            lambda is_current: list(members) if is_current else [],
        )
        monkeypatch.setattr(
            knesset_db, "_fetch_person_positions",
            lambda knesset_num: tuple(position_rows),
        )
    return install


def _full_names(members: list[dict]) -> list[str]:
    return [
        f"{m.get('mk_individual_first_name', '')} {m.get('mk_individual_name', '')}".strip()
        for m in members
    ]


def test_odata_fills_roster_when_oknesset_factions_are_null(patched_sources):
    patched_sources([OKNESSET_MEMBER], ODATA_POSITION_ROWS)

    members = knesset_db._get_all_members_raw(25)

    assert _full_names(members) == ["אביחי אברהם בוארון", "רון דרמר"]


def test_oknesset_identity_fields_survive_the_merge(patched_sources):
    patched_sources([OKNESSET_MEMBER], ODATA_POSITION_ROWS)

    boaron = next(m for m in knesset_db._get_all_members_raw(25) if m["PersonID"] == 30880)

    assert boaron["mk_individual_id"] == 7
    assert boaron["altnames"] == ["אביחי אברהם בוארון"]
    assert knesset_db._most_recent_faction(boaron["factions"], 25)["faction_name"] == "הליכוד"


def test_member_without_faction_is_still_in_the_roster(patched_sources):
    patched_sources([OKNESSET_MEMBER], ODATA_POSITION_ROWS)

    dermer = next(m for m in knesset_db._get_all_members_raw(25) if m["PersonID"] == 31001)

    assert dermer["factions"] == []
    assert dermer["mk_individual_id"] == 31001


def test_oknesset_faction_records_are_used_when_present(patched_sources):
    healthy_member = dict(OKNESSET_MEMBER)
    healthy_member["factions"] = [
        {"faction_id": 1090, "faction_name": "הליכוד", "knesset": 25,
         "start_date": "2022-11-15", "finish_date": None}
    ]
    patched_sources([healthy_member], ODATA_POSITION_ROWS)

    members = knesset_db._get_all_members_raw(25)

    assert len(members) == 2
    boaron = next(m for m in members if m["PersonID"] == 30880)
    assert boaron["factions"][0]["start_date"] == "2022-11-15"


def test_empty_sources_report_loudly(patched_sources, capsys):
    patched_sources([], ())

    members = knesset_db._get_all_members_raw(25)

    assert members == []
    assert "EMPTY roster for knesset 25" in capsys.readouterr().out


def test_odata_failure_is_printed_and_does_not_raise(monkeypatch, capsys):
    monkeypatch.setattr(knesset_db, "_fetch_members", lambda is_current: [])

    def _boom(knesset_num):
        raise RuntimeError("odata down")

    monkeypatch.setattr(knesset_db, "_fetch_person_positions", _boom)

    assert knesset_db._get_all_members_raw(25) == []
    assert "odata down" in capsys.readouterr().out


@pytest.mark.parametrize("first,last,expected", [
    ("אביחי אברהם", "בוארון", ["אביחי אברהם בוארון", "אביחי בוארון", "אברהם בוארון"]),
    ("ששון ששי", "גואטה", ["ששון ששי גואטה", "ששון גואטה", "ששי גואטה"]),
    ("יפעת", "שאשא ביטון", ["יפעת שאשא ביטון"]),
    ("", "", []),
])
def test_mk_name_variants(first, last, expected):
    assert knesset_db.mk_name_variants(first, last) == expected
