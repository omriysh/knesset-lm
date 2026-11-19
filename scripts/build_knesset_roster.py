"""
build_knesset_roster.py

The factions and MKs of a sitting Knesset (default: config.CURRENT_KNESSET_NUM) into
Data/knesset_roster/<k>/: roster.json, photos/<person_id>.jpg and logos/<faction_id>.png.

    python scripts/build_knesset_roster.py [--knesset 26]

MKs are the KNS_PersonToPosition "member of faction" rows of the Knesset (the sitting 120, Norwegian-law
replacements included). The photo is the MK's current one on the Knesset site. Each MK is joined to their entry
in the election's candidate lists (Data/candidates/<k>/lists.json, scripts/build_candidate_lists.py) for
personal details, Wikipedia page, list position and a fallback photo; MKs the lists don't know by person id are matched by name. A faction takes the logo of
the list most of its MKs ran on, and its MKs are ordered by their place on that list.
"""

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import config
import build_candidate_lists as lists_builder

_FACTION_MEMBER_POSITION_ID = 54
ODATA_SOURCE = "https://knesset.gov.il/OdataV4/ParliamentInfo"


def faction_rows(knesset_num: int) -> dict[int, dict]:
    from utils import knesset_db as kdb
    rows = kdb.odata_all_rows("KNS_Faction", {"$filter": f"KnessetNum eq {knesset_num}", "$orderby": "Id"})
    return {row["Id"]: row for row in rows}


def sitting_members(knesset_num: int) -> dict[int, int]:
    """PersonID → FactionID of the sitting MKs (an open "member of faction" row)."""
    from utils import knesset_db as kdb
    members = {}
    for row in kdb._fetch_person_positions(knesset_num):
        if row.get("PositionID") == _FACTION_MEMBER_POSITION_ID and not row.get("FinishDate") and row.get("FactionID"):
            members[row["PersonID"]] = row["FactionID"]
    return members


def candidates_by_person(knesset_num: int, people: dict[int, dict]) -> dict[int, tuple[dict, dict]]:
    """PersonID → (list, candidate entry) for each MK of `people` found on the election's candidate lists."""
    lists_path = config.candidate_lists_dir(knesset_num) / "lists.json"
    if not lists_path.exists():
        print(f"no candidate lists at {lists_path}; MKs get no list data")
        return {}
    parties = json.loads(lists_path.read_text(encoding="utf-8"))["parties"]
    found = {}
    for party in parties:
        for candidate in party["candidates"]:
            if candidate.get("person_id") in people:
                found[candidate["person_id"]] = (party, candidate)
    unmatched = [person for person_id, person in people.items() if person_id not in found]
    for party in parties:
        for candidate in party["candidates"]:
            if candidate.get("person_id") or not unmatched:
                continue
            person, note = lists_builder.match_mk(candidate["name_raw"], unmatched)
            if person:
                found[person["person_id"]] = (party, candidate)
                unmatched.remove(person)
            elif note:
                print(f"  [{party['id']}.{candidate['position']}] {candidate['name_raw']}: {note}")
    for person in unmatched:
        print(f"  not on the candidate lists: {person['first_name']} {person['last_name']} ({person['person_id']})")
    return found


def build(knesset_num: int) -> None:
    directory = config.knesset_roster_dir(knesset_num)
    (directory / "photos").mkdir(parents=True, exist_ok=True)
    (directory / "logos").mkdir(parents=True, exist_ok=True)
    lists_dir = config.candidate_lists_dir(knesset_num)

    factions = faction_rows(knesset_num)
    seats = sitting_members(knesset_num)
    every = lists_builder.every_mk()
    people = {}
    for person_id in seats:
        person = every.get(person_id)
        if person is None:
            print(f"  person {person_id} has no MK rows, skipped")
            continue
        people[person_id] = person
    listed = candidates_by_person(knesset_num, people)
    db_mk_ids = lists_builder.profile_mk_ids()

    members_by_faction: dict[int, list[dict]] = {}
    list_parties_by_faction: dict[int, Counter] = {}
    for person_id, person in people.items():
        party, candidate = listed.get(person_id, (None, None))
        candidate = candidate or {}
        entry = {"person_id": person_id, "mk_id": person.get("mk_id") or str(person_id),
                 "name": f"{person['first_name']} {person['last_name']}", "knessets": person["knessets"],
                 "site_id": person.get("site_id"), "profile": lists_builder.profile_depth(person, db_mk_ids),
                 "list_party_id": party["id"] if party else None, "list_position": candidate.get("position"),
                 "from_party": candidate.get("from_party", ""), "wikipedia": candidate.get("wikipedia"),
                 "details": candidate.get("details") or {}, "photo": None, "photo_source": None}
        photo_path = directory / "photos" / f"{person_id}.jpg"
        if entry["site_id"]:
            try:
                photo_url, details = lists_builder.knesset_site_details(entry["site_id"])
            except Exception as exc:
                print(f"  Knesset site details failed for {entry['name']} ({entry['site_id']}): {exc}")
                photo_url, details = None, {}
            entry["details"] = entry["details"] or details
            if photo_url and lists_builder.download(photo_url, photo_path):
                entry.update(photo=f"photos/{photo_path.name}", photo_source="knesset")
        if not entry["photo"] and candidate.get("photo") and (lists_dir / candidate["photo"]).exists():
            shutil.copyfile(lists_dir / candidate["photo"], photo_path)
            entry.update(photo=f"photos/{photo_path.name}", photo_source=candidate.get("photo_source"))
        faction_id = seats[person_id]
        members_by_faction.setdefault(faction_id, []).append(entry)
        if party:
            list_parties_by_faction.setdefault(faction_id, Counter())[party["id"]] += 1

    roster_factions = []
    for faction_id, members in members_by_faction.items():
        members.sort(key=lambda m: (m["list_position"] is None, m["list_position"] or 0, m["name"]))
        list_party_id = list_parties_by_faction[faction_id].most_common(1)[0][0] if faction_id in list_parties_by_faction else None
        logo = None
        if list_party_id is not None and (lists_dir / "logos" / f"{list_party_id}.png").exists():
            shutil.copyfile(lists_dir / "logos" / f"{list_party_id}.png", directory / "logos" / f"{faction_id}.png")
            logo = f"logos/{faction_id}.png"
        row = factions.get(faction_id, {})
        roster_factions.append({"id": faction_id, "name": (row.get("Name") or "").strip(), "list_party_id": list_party_id,
                                "logo": logo, "start_date": (row.get("StartDate") or "")[:10], "members": members})
        print(f"[{faction_id}] {roster_factions[-1]['name']}: {len(members)} MKs, "
              f"{sum(m['profile'] == 'full' for m in members)} full profiles, {sum(bool(m['photo']) for m in members)} photos")
    roster_factions.sort(key=lambda f: (-len(f["members"]), f["name"]))

    payload = {"knesset_num": knesset_num, "source": ODATA_SOURCE,
               "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "factions": roster_factions}
    (directory / "roster.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {directory / 'roster.json'}: {sum(len(f['members']) for f in roster_factions)} MKs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--knesset", type=int, default=config.CURRENT_KNESSET_NUM)
    build(parser.parse_args().knesset)


if __name__ == "__main__":
    main()
