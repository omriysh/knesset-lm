"""
tests/test_game.py

The "היכרות" game: name redaction, party-balanced card picking and results (profiles/game.py), and the
/api/game routes plus /api/profiles/theme/{id} (web/game.py, web/profiles.py) on the real Data/knesset.db
with a small lists.json of three 25th Knesset MKs who have themes, in two parties.
"""

import json
import random
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from fastapi.testclient import TestClient

import config
from profiles import game
from profiles.game import REDACTED, PoolTheme
from web.game import CARD_EVIDENCE


class TestRedact:
    def test_name_with_prefixes_is_hidden_and_prefix_kept(self):
        names = game.redaction_names(["יולי אדלשטיין", "אדלשטיין", "יולי"])
        assert game.redact("ואדלשטיין אמר שיולי אדלשטיין צודק", names) == f"ו{REDACTED} אמר ש{REDACTED} צודק"

    def test_name_inside_a_longer_word_is_kept(self):
        assert game.redact("הלוויין שוגר", game.redaction_names(["לוי"])) == "הלוויין שוגר"

    def test_name_starting_with_a_prefix_letter(self):
        assert game.redact("לוי ובלוי", game.redaction_names(["לוי"])) == f"{REDACTED} וב{REDACTED}"

    def test_no_names(self):
        assert game.redact("טקסט", []) == "טקסט"


def _pool(themes_per_party: dict[int, int], candidates_per_party: int = 1, subjects: int = 1) -> dict[int, PoolTheme]:
    """Each party's themes go round-robin over `subjects` subjects (ids 1…), with no approach."""
    pool, theme_id = {}, 1
    for party_id, count in themes_per_party.items():
        for n in range(count):
            position = n % candidates_per_party + 1
            subject_id = n % subjects + 1
            pool[theme_id] = PoolTheme(theme_id, party_id, position, f"{party_id}-{position}", f"t{theme_id}", 5, subject_id)
            theme_id += 1
    return pool


class TestPick:
    def test_new_subject_until_every_subject_was_shown(self):
        pool = _pool({1: 6, 2: 6, 3: 6}, subjects=6)
        for seed in range(10):
            picked = game.pick_cards(pool, [], 6, random.Random(seed))
            assert len({pool[i].subject_id for i in picked}) == 6

    def test_seen_subjects_count(self):
        pool = _pool({1: 4, 2: 4}, subjects=2)
        seen = [i for i in pool if pool[i].subject_id == 1][:1]
        for seed in range(10):
            assert pool[game.pick_cards(pool, seen, 1, random.Random(seed))[0]].subject_id == 2

    def test_least_shown_party_is_picked(self):
        pool = _pool({1: 5, 2: 5, 3: 5}, subjects=5)
        seen = [1, 6]
        for seed in range(10):
            assert pool[game.pick_cards(pool, seen, 1, random.Random(seed))[0]].party_id == 3

    def test_parties_tied_on_shows_are_randomized(self):
        pool = _pool({1: 3, 2: 3})
        firsts = {pool[game.pick_cards(pool, [], 1, random.Random(seed))[0]].party_id for seed in range(30)}
        assert firsts == {1, 2}

    def test_parties_take_turns_not_weighted_by_theme_count(self):
        pool = _pool({1: 200, 2: 10, 3: 10}, candidates_per_party=20, subjects=10)
        for seed in range(10):
            counts = Counter(pool[i].party_id for i in game.pick_cards(pool, [], 30, random.Random(seed)))
            assert min(counts[p] for p in (1, 2, 3)) >= 8, counts

    def test_seen_never_returned_and_exhaustion(self):
        pool = _pool({1: 3, 2: 2}, subjects=2)
        seen = []
        while picked := game.pick_cards(pool, seen, 2, random.Random(len(seen))):
            assert not set(picked) & set(seen)
            seen += picked
        assert sorted(seen) == sorted(pool)

    def test_only_chosen_parties_and_their_subjects(self):
        pool = _pool({1: 4, 2: 4, 3: 4}, subjects=4)
        for i in (1, 2):
            pool[i] = PoolTheme(i, 1, 1, "1-1", "t", 5, 9)
        picked = game.pick_cards(pool, [], 20, random.Random(0), parties={2, 3})
        assert sorted(picked) == sorted(i for i, t in pool.items() if t.party_id in (2, 3))
        assert 9 not in {pool[i].subject_id for i in picked}

    def test_themes_without_a_subject_or_with_few_opinions_are_not_played(self):
        pool = {1: PoolTheme(1, 1, 1, "a", "t", game.MIN_THEME_OPINIONS - 1, 1, 1), 2: PoolTheme(2, 1, 1, "a", "t", 9)}
        assert game.pick_cards(pool, [], 3) == []


class TestResults:
    def test_order_and_best_party_without_shared_approaches(self):
        pool = {i: PoolTheme(i, party_id, 1, str(party_id), "t", 5) for i, party_id in enumerate([1, 1, 1, 1, 2, 2, 2, 2, 3, 3], 1)}
        results = game.game_results(pool, agreed=[1, 5, 6], disagreed=[2, 3, 7, 9])
        assert [p["party_id"] for p in results["parties"]] == [2, 1, 3]
        assert results["best_party_id"] == 2
        assert results["parties"][0] == {"party_id": 2, "agreed": [{"theme_id": 5, "swiped_id": 5}, {"theme_id": 6, "swiped_id": 6}],
                                         "disagreed": [{"theme_id": 7, "swiped_id": 7}]}

    def test_a_swipe_counts_for_every_party_in_the_approach(self):
        pool = {1: PoolTheme(1, 1, 1, "a", "t", 5, 10, 100), 2: PoolTheme(2, 2, 1, "b", "t", 3, 10, 100),
                3: PoolTheme(3, 2, 2, "c", "t", 8, 10, 100), 4: PoolTheme(4, 3, 1, "d", "t", 5, 10, 101)}
        results = game.game_results(pool, agreed=[1], disagreed=[])
        by_party = {p["party_id"]: p for p in results["parties"]}
        assert set(by_party) == {1, 2}
        assert by_party[1]["agreed"] == [{"theme_id": 1, "swiped_id": 1}]
        assert by_party[2]["agreed"] == [{"theme_id": 3, "swiped_id": 1}]

    def test_tie_goes_to_fewer_disagreed(self):
        pool = _pool({1: 4, 2: 4}, subjects=4)
        assert game.game_results(pool, agreed=[1, 5], disagreed=[2])["best_party_id"] == 2

    def test_nothing_agreed(self):
        assert game.game_results(_pool({1: 2}), agreed=[], disagreed=[1])["best_party_id"] is None


# ── routes ───────────────────────────────────────────────────────────────────

def _mks_with_themes(db_path: Path, count: int) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        return [mk_id for (mk_id,) in conn.execute(
            """SELECT mk_id FROM mk_themes WHERE knesset_num = 25 AND opinion_count >= ?
               GROUP BY mk_id HAVING COUNT(*) >= 4 ORDER BY mk_id LIMIT ?""", (game.MIN_THEME_OPINIONS, count))]
    finally:
        conn.close()


def _candidate(position: int, mk_id: str | None, profile: str = "full") -> dict:
    return {"position": position, "name_raw": f"מועמד {position}", "name": f"מועמד {position}", "from_party": "",
            "mk_id": mk_id, "person_id": int(mk_id) if mk_id else None, "knessets": [25] if mk_id else [],
            "profile": profile, "photo": None, "photo_source": None, "wikipedia": None, "details": {}}


@pytest.fixture()
def mk_ids(real_db):
    ids = _mks_with_themes(real_db.db_path, 3)
    if len(ids) < 3:
        pytest.skip("the db has fewer than 3 MKs with themes")
    return ids


@pytest.fixture()
def client(real_db, mk_ids, tmp_path, monkeypatch):
    party = lambda party_id, candidates: {"id": party_id, "letters": "א", "name": f"רשימה {party_id}", "submitted_by": "",
                                          "gov_url": "", "ballot": None, "leader": "", "candidates": candidates}
    lists = {"parties": [party(1, [_candidate(1, mk_ids[0]), _candidate(2, mk_ids[1]), _candidate(3, None, "none")]),
                         party(2, [_candidate(1, mk_ids[2])])]}
    (tmp_path / "lists.json").write_text(json.dumps(lists, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "candidate_lists_dir", lambda election_knesset_num=26: tmp_path)
    import web.app as webapp
    import web.settings as settings
    webapp.app.state.settings = settings
    webapp.app.state.sessions_dir = tmp_path
    webapp.app.state.machine = SimpleNamespace(name="test_machine", version=2)
    webapp.rate_limiter.reset()
    return TestClient(webapp.app)


def ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def _pool_ids(client) -> list[int]:
    from web.profiles import theme_pool
    return sorted(theme_pool())


class TestNextRoute:
    def test_parties_lists_playable_cards(self, client):
        from web.profiles import theme_pool
        parties = ok(client.get("/api/game/parties"))["parties"]
        pool = theme_pool()
        assert parties and [p["cards"] for p in parties] == sorted((p["cards"] for p in parties), reverse=True)
        for p in parties:
            assert p["cards"] == sum(1 for t in pool.values() if t.party_id == p["party"]["id"] and game.is_playable(t))

    def test_cards_of_chosen_parties_only(self, client):
        from web.profiles import theme_pool
        party_id = ok(client.get("/api/game/parties"))["parties"][-1]["party"]["id"]
        cards = ok(client.post("/api/game/next", json={"count": 3, "parties": [party_id]}))["cards"]
        assert cards and {theme_pool()[card["id"]].party_id for card in cards} == {party_id}

    def test_cards_are_redacted_and_anonymous(self, client):
        cards = ok(client.post("/api/game/next", json={"count": 3}))["cards"]
        assert len(cards) == 3
        for card in cards:
            assert set(card) == {"id", "subject", "approach_id", "title", "summary", "evidence"}
            assert set(card["subject"]) == {"id", "name", "umbrella"}
            assert len(card["evidence"]) <= CARD_EVIDENCE and set(card["evidence"][0]) == {"quote", "opinion", "date", "committee"}
        assert any(REDACTED in card["summary"] for card in cards)

    def test_plays_through_the_pool_without_repeats(self, client):
        seen = []
        while cards := ok(client.post("/api/game/next", json={"seen": seen, "count": 3}))["cards"]:
            ids = [card["id"] for card in cards]
            assert not set(ids) & set(seen)
            seen += ids
        assert len(seen) == len(set(seen)) > 3

    @pytest.mark.parametrize("body", [
        {"seen": ["12"]}, {"seen": [1.5]}, {"seen": [True]}, {"seen": [0]}, {"seen": [-3]},
        {"seen": [1, 1]}, {"count": 0}, {"count": config.GAME_MAX_CARDS_PER_FETCH + 1}, {"count": "2"},
        {"seen": list(range(1, config.GAME_MAX_IDS + 2))}, {"seen": "1,2"},
    ])
    def test_bad_input_is_400(self, client, body):
        assert client.post("/api/game/next", json=body).status_code == 400

    def test_above_the_largest_id_is_400(self, client):
        top = _pool_ids(client)[-1]
        assert client.post("/api/game/next", json={"seen": [top + 1]}).status_code == 400

    def test_in_range_unknown_ids_are_dropped(self, client):
        ids = _pool_ids(client)
        unknown = next(i for i in range(1, ids[-1]) if i not in set(ids))
        body = ok(client.post("/api/game/next", json={"seen": [unknown, ids[0]]}))
        assert body["dropped"] == [unknown] and ids[0] not in [card["id"] for card in body["cards"]]


class TestResultsRoute:
    def test_results_by_party(self, client):
        ids = _pool_ids(client)
        body = ok(client.post("/api/game/results", json={"agreed": ids[:2], "disagreed": ids[-1:]}))
        assert body["best_party_id"] in (1, 2) and body["dropped"] == []
        best = body["parties"][0]
        assert best["party"]["id"] == body["best_party_id"] and best["agreed"]
        ref = best["agreed"][0]
        assert REDACTED not in ref["title"] and "via_approach" in ref
        assert ref["candidate"]["profile_url"].startswith(f"/profiles/party/{best['party']['id']}/candidate/")

    @pytest.mark.parametrize("body", [{"agreed": [1], "disagreed": [1]}, {"agreed": ["1"]}, {"disagreed": [0]}])
    def test_bad_input_is_400(self, client, body):
        assert client.post("/api/game/results", json=body).status_code == 400


class TestThemeRoute:
    def test_same_shape_as_the_profile_themes(self, client):
        theme_id = _pool_ids(client)[0]
        body = ok(client.get(f"/api/profiles/theme/{theme_id}"))
        profile = ok(client.get(f"/api/profiles/party/{body['party_id']}/candidate/{body['candidate_id']}/themes"))
        index = next(i for i, t in enumerate(profile["themes"]) if t["id"] == theme_id)
        assert body["theme"] == profile["themes"][index] and body["color_index"] == index
        assert body["quarters"] == profile["quarters"]

    def test_unknown_and_out_of_range(self, client):
        ids = _pool_ids(client)
        unknown = next(i for i in range(1, ids[-1]) if i not in set(ids))
        assert client.get(f"/api/profiles/theme/{unknown}").status_code == 404
        assert client.get(f"/api/profiles/theme/{ids[-1] + 1}").status_code == 400
        assert client.get("/api/profiles/theme/0").status_code == 400
        assert client.get("/api/profiles/theme/abc").status_code == 400
