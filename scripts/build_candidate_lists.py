"""
build_candidate_lists.py

The 26th Knesset candidate lists, as published by the Central Elections Committee on gov.il, into
Data/candidates/26/: lists.json, ballots/<party_id>.png and photos/<party_id>_<position>.jpg.

    python scripts/build_candidate_lists.py scrape   # gov.il pages → raw_pages.json (resumable)
    python scripts/build_candidate_lists.py logos    # Wikipedia / party sites → logos/<party_id>.png
    python scripts/build_candidate_lists.py build    # raw_pages.json → lists.json, ballots, photos

gov.il sits behind a Cloudflare challenge that only a real browser passes, so `scrape` drives Chrome
through Playwright (pip install playwright), with a fresh browser per page: a reused one gets challenged.
Party ids are the row order of the index page and candidate ids their list position, so
/profiles/party/<id>/candidate/<id> stays stable for candidates who were never MKs.

`build` matches candidates to MKs of every Knesset (OData KNS_PersonToPosition), takes photos and
personal details from the Knesset site API, falls back to the Hebrew Wikipedia lead image, and prints
the former-MK matches for manual review.

`logos` takes each list's logo from its party's Hebrew Wikipedia infobox (PARTY_WIKIPEDIA_LOGOS, curated by
hand: gov.il publishes only the ballot), or from the party's website when it has a newer or sharper one
(PARTY_SITE_LOGOS, through Playwright since some sites need a browser). Lists with neither keep the ballot.
"""

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import config

INDEX_URL = "https://www.gov.il/he/pages/candidates-lists-26"
KNESSET_SITE_API = "https://knesset.gov.il/WebSiteApi/knessetapi"
WIKIPEDIA_API = "https://he.wikipedia.org/w/api.php"
_MK_POSITION_IDS = (43, 61)
_EXIF_ARTIST, _EXIF_COPYRIGHT = 315, 33432
_HEADERS = {"User-Agent": "KnessetLM/1.0 (candidate lists; https://github.com/omriysh)"}
_WIKIPEDIA_POLITICS_WORDS = ("כנסת", "פוליטיק", "מפלג", "מועמד", "ראש עיר", "ראש מועצ", "שר ", "שרה ")
_MAX_NAME_TOKENS = 6
_NUMBERED_LINE_RE = re.compile(r"^\s*1\s*\.", re.M)
_NUMBERED_CELL_RE = re.compile(r"^(\d{1,3})\s*\.\s*(.+)$")
_FROM_PARTY_RE = re.compile(r"^\s*מטעם\s+(.+?)\s*$")

# The official website of each list's party as its Hebrew Wikipedia page links it (infobox or {{אתר רשמי}}),
# when it is still up. A list whose party page links none, or only an archived or dead site, gets no link.
PARTY_WEBSITES = {
    1: "https://democrats.org.il/",
    2: "https://www.ozma-yeudit.co.il/",
    8: "https://www.themiluimnikim.org.il/",
    9: "https://yasharwitheisenkot.com/",
    11: "https://jointlist.org.il/he/",
    15: "https://zionutdatit.org.il/",
    18: "https://amchaisrael.co.il/",
    19: "https://kachollavan.org.il/",
    20: "https://beytenu.org.il/",
    21: "https://www.likud.org.il/",
    22: "https://noam.org.il/",
    31: "https://piratim.org/",
    37: "https://be-yahad.org.il/",
}
# The logo in each party's Hebrew Wikipedia infobox, a File: name on he.wikipedia or Commons.
PARTY_WIKIPEDIA_LOGOS = {
    1: "The Democrats led by Yair Golan.svg",
    2: "עוצמה יהודית לוגו 2021.svg",
    6: "יהדות התורה לוגו 2019.svg",
    9: "Yashar2025.png",
    11: "Joint List logo 2019.svg",
    15: "לוגו הציונות הדתית 2026.png",
    18: "עמך-ישראל-סמל.png",
    20: "לוגו ישראל ביתנו 2022.svg",
    21: "Likud Logo.svg",
    27: "הרשימה הערבית המאוחדת לוגו 2021.svg",
    31: "Piratim Party Logo.png",
    38: "Shas logo.svg",
}
# Lists whose website shows a newer or sharper logo than Wikipedia's: the file itself, on its brand's background
# when it is white, or else (None) a screenshot of the site's header logo.
PARTY_SITE_LOGOS = {
    8: ("https://www.themiluimnikim.org.il/wp-content/uploads/2026/03/לוגו-המילואמניקים-600x115.png", "#1c1c1c"),
    19: ("https://kachollavan.org.il/wp-content/uploads/2025/07/CahoLavan-PNG.png", "#0f2350"),
    22: None,
    37: ("https://be-yahad.org.il/wp-content/uploads/2026/09/LOGO_RAK-02.svg", None),
}
_WIKIPEDIA_LOGO_WIDTH_PX = 1280
PNG_SIGNATURE = bytes.fromhex("89504e47")
_LOGO_FILE_HEIGHT_PX = 120
_LOGO_FINDER_JS = """() => {
  const visible = el => { const r = el.getBoundingClientRect(); return r.width >= 40 && r.height >= 16 && r.top < 260 && r.bottom > 0; };
  const selectors = ['.custom-logo', 'header [class*=logo] img', 'header [class*=logo] svg', '[class*=logo] img', 'img[src*=logo]',
                     'img[alt*=logo i]', '[id*=logo] img', 'header a[href="/"] img', 'header img', 'header svg'];
  for (const selector of selectors) {
    const found = [...document.querySelectorAll(selector)].find(visible);
    if (found) { found.setAttribute('data-knesset-lm-logo', '1'); return selector; }
  }
  return null;
}"""


def out_dir() -> Path:
    return config.candidate_lists_dir(26)


# ── scrape ───────────────────────────────────────────────────────────────────

def _browser_page_text(playwright, url: str, ready) -> tuple[str, list[str], list[str], dict]:
    """(body text, image urls, gov.il attachment urls, extra) once ready(text) holds; one fresh browser per call."""
    browser = playwright.chromium.launch(channel="chrome", headless=False)
    try:
        page = browser.new_context(locale="he-IL").new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        text = ""
        for _ in range(20):
            page.wait_for_timeout(1000)
            text = page.inner_text("body")
            if ready(text):
                break
        images = page.evaluate("[...document.querySelectorAll('img')].map(i => i.src)")
        files = page.evaluate("[...document.querySelectorAll('a')].map(a => a.href)"
                              ".filter(h => h.includes('BlobFolder/generalpage'))")
        rows = page.evaluate("[...document.querySelectorAll('tr')].map(tr => "
                             "[[...tr.querySelectorAll('td')].map(td => td.innerText.trim()), "
                             "tr.querySelector('a') ? tr.querySelector('a').href : null])")
        ballot = None
        ballot_url = next((src for src in images if "/BlobFolder/generalpage/" in src and src.endswith(".png")), None)
        if ballot_url:
            response = page.request.get(ballot_url)
            ballot = response.body() if response.ok else None
        return text, images, files, {"rows": rows, "ballot": ballot}
    finally:
        browser.close()


def scrape() -> None:
    from playwright.sync_api import sync_playwright

    directory = out_dir()
    (directory / "ballots").mkdir(parents=True, exist_ok=True)
    raw_path = directory / "raw_pages.json"
    pages = {page["position"]: page for page in json.loads(raw_path.read_text(encoding="utf-8"))} \
        if raw_path.exists() else {}
    with sync_playwright() as playwright:
        _, _, _, index = _browser_page_text(playwright, INDEX_URL, lambda text: "כינוי הרשימה" in text)
        parties = [(cells[0], cells[1], link) for cells, link in index["rows"] if len(cells) >= 2 and link]
        print(f"{len(parties)} lists on the index page")
        for position, (letters, name, url) in enumerate(parties, start=1):
            url = url.replace("http://", "https://")
            known = pages.get(position)
            if known and known.get("url") == url and _NUMBERED_LINE_RE.search(known["text"]) \
                    and (directory / "ballots" / f"{position}.png").exists():
                known["letters"] = letters
                continue
            for attempt in range(3):
                try:
                    text, images, files, extra = _browser_page_text(
                        playwright, url, lambda text: bool(_NUMBERED_LINE_RE.search(text)))
                except Exception as exc:
                    print(f"  [{position}] attempt {attempt + 1} failed: {exc}")
                    continue
                if _NUMBERED_LINE_RE.search(text):
                    pages[position] = {"position": position, "letters": letters, "name": name, "url": url,
                                       "text": text, "imgs": images, "files": files}
                    if extra["ballot"]:
                        (directory / "ballots" / f"{position}.png").write_bytes(extra["ballot"])
                    break
                time.sleep(5)
            print(f"  [{position}] {name}: {'ok' if position in pages else 'FAILED'}", flush=True)
            raw_path.write_text(json.dumps(sorted(pages.values(), key=lambda p: p["position"]), ensure_ascii=False),
                                encoding="utf-8")


def _render_logo_file(playwright, url: str, background: str | None, path: Path) -> None:
    """The logo file drawn by the browser (so SVGs work too), padded on its background when given."""
    browser = playwright.chromium.launch(channel="chrome", headless=True)
    try:
        page = browser.new_page()
        padding = "16px 22px" if background else "0"
        page.set_content(f'<body style="margin:0;background:transparent"><div id="logo" style="display:inline-block;'
                         f'padding:{padding};background:{background or "transparent"}"><img src="{url}" '
                         f'style="display:block;height:{_LOGO_FILE_HEIGHT_PX}px"></div></body>')
        page.wait_for_function("document.querySelector('img').complete", timeout=30_000)
        page.locator("#logo").screenshot(path=str(path), omit_background=True)
    finally:
        browser.close()


def _download_wikipedia_logo(file_name: str, path: Path) -> None:
    response = requests.get(WIKIPEDIA_API, headers=_HEADERS, timeout=60, params={
        "action": "query", "prop": "imageinfo", "iiprop": "url", "iiurlwidth": _WIKIPEDIA_LOGO_WIDTH_PX,
        "titles": f"File:{file_name}", "format": "json"})
    response.raise_for_status()
    info = next(iter(response.json()["query"]["pages"].values()))["imageinfo"][0]
    image = requests.get(info.get("thumburl") or info["url"], headers=_HEADERS, timeout=60)
    image.raise_for_status()
    if not image.content.startswith(PNG_SIGNATURE):
        from io import BytesIO
        from PIL import Image
        Image.open(BytesIO(image.content)).save(path, "PNG")
        return
    path.write_bytes(image.content)


def logos() -> None:
    """logos/<party_id>.png: the Wikipedia infobox logo, or the one on the party's website (PARTY_SITE_LOGOS)."""
    from playwright.sync_api import sync_playwright

    directory = out_dir() / "logos"
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.png"):
        if int(stale.stem) not in PARTY_WIKIPEDIA_LOGOS.keys() | PARTY_SITE_LOGOS.keys():
            stale.unlink()
    for party_id, file_name in PARTY_WIKIPEDIA_LOGOS.items():
        if party_id in PARTY_SITE_LOGOS:
            continue
        try:
            _download_wikipedia_logo(file_name, directory / f"{party_id}.png")
            print(f"  [{party_id}] Wikipedia: {file_name}")
        except Exception as exc:
            print(f"  [{party_id}] Wikipedia {file_name} failed: {exc}")
        time.sleep(1)
    with sync_playwright() as playwright:
        for party_id, logo_file in PARTY_SITE_LOGOS.items():
            if logo_file:
                url, background = logo_file
                _render_logo_file(playwright, url, background, directory / f"{party_id}.png")
                print(f"  [{party_id}] {url}: the file{f' on {background}' if background else ''}")
                continue
            url = PARTY_WEBSITES[party_id]
            browser = playwright.chromium.launch(channel="chrome", headless=False)
            try:
                page = browser.new_context(locale="he-IL", viewport={"width": 1400, "height": 900}).new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(8000)
                selector = page.evaluate(_LOGO_FINDER_JS)
                if selector is None:
                    print(f"  [{party_id}] {url}: no logo found")
                    continue
                page.locator("[data-knesset-lm-logo]").first.screenshot(path=str(directory / f"{party_id}.png"),
                                                                         omit_background=True)
                print(f"  [{party_id}] {url}: {selector}")
            except Exception as exc:
                print(f"  [{party_id}] {url} failed: {exc}")
            finally:
                browser.close()


# ── build: parse ─────────────────────────────────────────────────────────────

def _candidate_name(text: str) -> str | None:
    name = " ".join(text.replace(",", " ").split())
    if name and len(name.split()) <= _MAX_NAME_TOKENS and not re.search(r"[\d.:;]", name):
        return name
    return None


def parse_candidates(text: str) -> list[dict]:
    """[{position, name_raw (last name first, as published), from_party}] from a list page's text.
    Cells are separated by tabs and new lines; a "מטעם <party>" cell belongs to the candidate before it.
    A one-candidate list may be published without a number."""
    body = text.split("שתפו:", 1)[-1].split("קבצים מצורפים", 1)[0]
    cells = [cell.strip() for cell in re.split(r"[\t\n]", body.replace("\xa0", " ")) if cell.strip()]
    candidates, last = [], None
    for cell in cells:
        if match := _FROM_PARTY_RE.match(cell):
            if last is not None:
                last["from_party"] = match.group(1).removeprefix("מפלגת ").strip()
        elif (match := _NUMBERED_CELL_RE.match(cell)) and (name := _candidate_name(match.group(2))):
            last = {"position": int(match.group(1)), "name_raw": name, "from_party": ""}
            candidates.append(last)
    if not candidates:
        names = [name for cell in cells if not cell.startswith("תמונת פתק") and (name := _candidate_name(cell))]
        return [{"position": 1, "name_raw": names[0], "from_party": ""}] if len(names) == 1 else []
    by_position = {c["position"]: c for c in reversed(candidates)}
    sequential = []
    while len(sequential) + 1 in by_position:
        sequential.append(by_position[len(sequential) + 1])
    return sequential


def submitted_by(text: str) -> str:
    match = re.search(r"רשימת המועמדים הוגשה (.+)", text)
    return match.group(1).strip() if match else ""


# ── build: MKs of every Knesset ──────────────────────────────────────────────

def _name_tokens(name: str) -> tuple[str, ...]:
    name = re.sub(r"[\"'׳״`’]", "", name)
    name = re.sub(r"[-־–()]", " ", name)
    return tuple(name.split())


def every_mk() -> dict[int, dict]:
    """PersonID → {person_id, first_name, last_name, knessets, mk_id (oknesset), site_id (Knesset site), aliases}."""
    from utils import knesset_db as kdb
    filter_expr = " or ".join(f"PositionID eq {position_id}" for position_id in _MK_POSITION_IDS)
    rows = kdb.odata_all_rows("KNS_PersonToPosition", {"$filter": filter_expr, "$expand": "KNS_Person",
                                                       "$orderby": "Id"})
    people: dict[int, dict] = {}
    for row in rows:
        person = row.get("KNS_Person") or {}
        entry = people.setdefault(row["PersonID"], {
            "person_id": row["PersonID"], "first_name": (person.get("FirstName") or "").strip(),
            "last_name": (person.get("LastName") or "").strip(), "knessets": set(), "aliases": set()})
        if row.get("KnessetNum"):
            entry["knessets"].add(row["KnessetNum"])
    for member in kdb._fetch_members(True) + kdb._fetch_members(False):
        entry = people.get(member.get("PersonID"))
        if entry is None:
            continue
        entry["mk_id"] = str(member["mk_individual_id"])
        entry["aliases"].update(member.get("altnames") or [])
    site_names = knesset_site_names()
    for entry in people.values():
        entry["knessets"] = sorted(entry["knessets"])
        entry["aliases"] = sorted(entry["aliases"])
        mk_id = entry.get("mk_id")
        if mk_id and mk_id != str(entry["person_id"]):
            entry["site_id"] = int(mk_id)
        else:
            names = [f"{entry['first_name']} {entry['last_name']}", *entry["aliases"]]
            entry["site_id"] = SITE_IDS_BY_PERSON_ID.get(entry["person_id"]) or next(
                (site_id for name in names if (site_id := _site_id_of(name, site_names))), None)
    return people


def knesset_site_names() -> dict[int, str]:
    """Knesset site MkId → name for the MKs whose oknesset id is not their site id: the current MKs and
    everyone who entered since, whose site ids run from _NEWER_SITE_IDS_START (the site lists no former MKs,
    so their ids are scanned)."""
    from concurrent.futures import ThreadPoolExecutor
    lobby = requests.get(f"{KNESSET_SITE_API}/MkLobby/GetMkLobbyData", params={"lang": "he"},
                         headers=_HEADERS, timeout=60).json()["mks"]
    names = {mk["MkId"]: f"{mk['Firstname']} {mk['Lastname']}" for mk in lobby}

    def site_name(site_id: int) -> tuple[int, str | None]:
        try:
            return site_id, (_site_mk("GetMkdetailsHeader", site_id) or {}).get("Name")
        except Exception as exc:
            print(f"  Knesset site MK {site_id} failed: {exc}")
            return site_id, None

    scanned = range(_NEWER_SITE_IDS_START, max(names) + _SITE_IDS_SCANNED_PAST_CURRENT)
    with ThreadPoolExecutor(4) as pool:
        names.update({site_id: name for site_id, name in pool.map(site_name, scanned) if name})
    return names


def _site_id_of(name: str, site_names: dict[int, str]) -> int | None:
    """The one site MK with the same name tokens, the same up to spelling, or holding them all (a middle name:
    יצחק פינדרוס = יצחק זאב פינדרוס)."""
    tokens = _name_tokens(name)
    if len(set(tokens)) < 2:
        return None
    for same in (lambda other: sorted(other) == sorted(tokens),
                 lambda other: _same_tokens_up_to_spelling(tokens, other),
                 lambda other: set(tokens) <= set(other)):
        found = {site_id for site_id, site_name in site_names.items() if same(_name_tokens(site_name))}
        if len(found) == 1:
            return found.pop()
        if found:
            return None
    return None


_PARTIAL_MATCH_MIN_KNESSET = 20
_NEWER_SITE_IDS_START = 850
# MKs the Knesset site names by a nickname no source lists (בני גנץ = בנימין גנץ).
SITE_IDS_BY_PERSON_ID = {30657: 988}
# Candidates the lists name by a legal name, extra surname or spelling the MK roster doesn't have (חילי = יחיאל משה).
PERSON_IDS_BY_CANDIDATE_NAME = {
    "טרופר יחיאל משה": 30683, "כהן אליהו": 30083, "מעוז אביגדור": 30814, "לסקי שוץ גבריאלה": 30818,
    "ביילין יוסף": 2192, "שיטרית שמעון": 2634, "בר עם עוזי": 861, "פורז פוזמנטיר אברהם": 519,
    "יזבק היבא": 30715, "שיטרית קטרין": 30706,
}
_SITE_IDS_SCANNED_PAST_CURRENT = 50
_SITE_API_ATTEMPTS = 4
_SITE_API_RETRY_SECONDS = 10


def _joined_prefixes(tokens: tuple[str, ...]) -> list[str]:
    """Tokens with a separate "אל" joined to the next token, so "אל הואשלה" = "אלהואשלה"."""
    joined: list[str] = []
    for token in tokens:
        if joined and joined[-1] == "אל":
            joined[-1] += token
        else:
            joined.append(token)
    return joined


def _same_tokens_up_to_spelling(tokens: tuple[str, ...], other_tokens: tuple[str, ...]) -> bool:
    """Both names pair up token by token, each pair starting with the same letter and fuzzily equal like
    find_mk's token rule (איזנקוט גד = גדי איזנקוט, בעז = בועז), but ירון ≠ פירון."""
    from itertools import permutations
    from rapidfuzz import fuzz
    tokens, other_tokens = _joined_prefixes(tokens), _joined_prefixes(other_tokens)
    if len(tokens) != len(other_tokens) or len(tokens) > 4:
        return False
    return any(all(token[0] == other[0] and fuzz.ratio(token, other) >= config.FUZZY_NAME_TOKEN_MATCH_MIN_RATIO
                   for token, other in zip(tokens, ordering))
               for ordering in permutations(other_tokens))


def _is_name_of(name_raw: str, person: dict, name: str) -> bool:
    """name_raw (last name first, as published) is this person: the same tokens, or for MKs of recent
    Knessets the same tokens up to spelling, or one name holding the other's tokens as long as the
    published name starts with the last name."""
    tokens, person_tokens = _name_tokens(name_raw), _name_tokens(name)
    if len(set(tokens)) < 2 or len(set(person_tokens)) < 2:
        return False
    if sorted(tokens) == sorted(person_tokens):
        return True
    if max(person["knessets"]) < _PARTIAL_MATCH_MIN_KNESSET:
        return False
    if _same_tokens_up_to_spelling(tokens, person_tokens):
        return True
    last_name = _name_tokens(person["last_name"])
    contained = set(person_tokens) <= set(tokens) or set(tokens) <= set(person_tokens)
    return contained and bool(last_name) and tuple(tokens[:len(last_name)]) == last_name


def match_mk(name_raw: str, people: list[dict]) -> tuple[dict | None, str]:
    """The MK a candidate is (see _is_name_of); the latest-serving one wins and a tie between
    different people is left unmatched."""
    found = [person for person in people
             if any(_is_name_of(name_raw, person, name)
                    for name in [f"{person['first_name']} {person['last_name']}", *person["aliases"]])]
    if not found:
        return None, ""
    ranked = sorted(found, key=lambda p: max(p["knessets"]), reverse=True)
    if len(ranked) > 1 and max(ranked[0]["knessets"]) == max(ranked[1]["knessets"]):
        return None, "ambiguous: " + ", ".join(f"{p['first_name']} {p['last_name']} ({p['person_id']})" for p in ranked)
    return ranked[0], ""


# ── build: display names, photos, details ────────────────────────────────────

def _name_splits(name_raw: str, first_names: set[str]) -> list[str]:
    """'first last' orderings of a 'last first' name, most likely first: the split whose first-name part holds
    the most known first names and whose last-name part the fewest; ties go to a single first name."""
    tokens = name_raw.split()
    if len(tokens) < 2:
        return [name_raw]

    def score(k: int) -> tuple[int, int]:
        first, last = tokens[k:], tokens[:k]
        return (sum(t in first_names for t in last) - sum(t in first_names for t in first), -k)

    return [" ".join(tokens[k:] + tokens[:k]) for k in sorted(range(1, len(tokens)), key=score)]


def wikipedia_pages(titles: list[str]) -> dict[str, dict]:
    """title → {title, url, extract} for Hebrew Wikipedia articles (redirects followed, no disambiguations)."""
    pages: dict[str, dict] = {}
    for start in range(0, len(titles), 20):
        batch = titles[start:start + 20]
        response = requests.post(WIKIPEDIA_API, headers=_HEADERS, timeout=60, data={
            "action": "query", "format": "json", "redirects": 1, "titles": "|".join(batch),
            "prop": "extracts|pageprops",
            "exintro": 1, "explaintext": 1, "exsentences": 2, "exlimit": 20})
        response.raise_for_status()
        query = response.json().get("query", {})
        redirected = {r["from"]: r["to"] for r in query.get("redirects", []) + query.get("normalized", [])}
        by_title = {page["title"]: page for page in query.get("pages", {}).values() if "missing" not in page}
        for title in batch:
            page = by_title.get(redirected.get(title, title))
            if page and "disambiguation" not in page.get("pageprops", {}):
                pages[title] = {"title": page["title"], "extract": page.get("extract", ""),
                                "url": f"https://he.wikipedia.org/wiki/{page['title'].replace(' ', '_')}"}
        time.sleep(0.5)
    return pages


def _site_mk(method: str, site_id: int) -> dict | None:
    """One Knesset site MKs/<method> record; None for an id the site has no MK for. The site answers a
    burst of requests with an HTML page, so that is retried."""
    for attempt in range(_SITE_API_ATTEMPTS):
        response = requests.get(f"{KNESSET_SITE_API}/MKs/{method}", headers=_HEADERS, timeout=60,
                                params={"mkId": site_id, "languageKey": "he"})
        if response.text.strip() in ("", "null"):
            return None
        if response.text.lstrip().startswith("{"):
            return response.json()
        print(f"  Knesset site {method}({site_id}) answered {response.status_code} non-JSON, retrying")
        time.sleep(_SITE_API_RETRY_SECONDS * (attempt + 1))
    raise RuntimeError(f"Knesset site {method}({site_id}) kept answering non-JSON")


def knesset_site_details(site_id: int) -> tuple[str | None, dict]:
    """(photo url, personal details) from the Knesset site's MK pages."""
    header = _site_mk("GetMkdetailsHeader", site_id) or {}
    content = _site_mk("GetMkDetailsContent", site_id) or {}
    birth = (content.get("DateOfBirth") or "").split(",")[-1].strip()
    details = {"birth_date": birth, "place_of_birth": content.get("PlaceOfBirth") or "",
               "residence": content.get("Residence") or "", "education": content.get("Education") or "",
               "military_service": content.get("MilitaryService") or "",
               "profession": content.get("profession") or "", "immigration_year": content.get("ImmigrationYear") or ""}
    return header.get("MkImage") or header.get("LobbyImage") or None, {key: html.unescape(value).strip() for key, value in details.items() if value and value.strip()}


def _died(site_id: int) -> bool:
    return bool((_site_mk("GetMkDetailsContent", site_id) or {}).get("DeathDate"))


def download(url: str, path: Path) -> bool:
    try:
        response = requests.get(url, headers=_HEADERS, timeout=60)
        response.raise_for_status()
    except Exception as exc:
        print(f"  photo download failed {url}: {exc}")
        return False
    path.write_bytes(response.content)
    return True


def embedded_photo_credit(path: Path) -> str | None:
    """The photographer the image file itself names (EXIF Artist/Copyright, else IPTC byline); None when it names none."""
    from PIL import Image, IptcImagePlugin
    with Image.open(path) as image:
        exif = image.getexif()
        iptc = IptcImagePlugin.getiptcinfo(image) or {}
    byline = iptc.get((2, 80))
    candidates = [exif.get(_EXIF_ARTIST), byline.decode("utf-8", "ignore") if isinstance(byline, bytes) else byline,
                  exif.get(_EXIF_COPYRIGHT)]
    return next((value.strip() for value in candidates if isinstance(value, str) and value.strip("- ")), None)


def profile_mk_ids() -> set[str]:
    """mk_ids of the MKs of every Knesset with processed protocols (config.PROTOCOL_KNESSET_NUMS)."""
    from retrieval import knesset_db_store as store
    conn = store.connect()
    try:
        return {row["id"] for row in store.name_entries(conn, "mks", config.PROTOCOL_KNESSET_NUMS)}
    finally:
        conn.close()


def profile_depth(person: dict, mk_ids_with_protocols: set[str]) -> str:
    """'full' for an MK of a Knesset with processed protocols, else 'bills' (a former MK of an earlier Knesset)."""
    return "full" if person.get("mk_id") in mk_ids_with_protocols else "bills"


def build() -> None:
    directory = out_dir()
    (directory / "photos").mkdir(parents=True, exist_ok=True)
    raw_pages = json.loads((directory / "raw_pages.json").read_text(encoding="utf-8"))
    people = {person_id: person for person_id, person in every_mk().items() if person["knessets"]}
    first_names = {token for person in people.values() for token in person["first_name"].split()}

    db_mk_ids = profile_mk_ids()

    parties, unmatched_names = [], []
    for page in raw_pages:
        candidates = []
        for candidate in parse_candidates(page["text"]):
            known_person_id = PERSON_IDS_BY_CANDIDATE_NAME.get(candidate["name_raw"])
            person, note = (people[known_person_id], "") if known_person_id else match_mk(candidate["name_raw"], list(people.values()))
            if person and person.get("site_id") and _died(person["site_id"]):
                note, person = f"matches {person['first_name']} {person['last_name']} ({person['person_id']}), who died", None
            if note:
                print(f"  [{page['position']}.{candidate['position']}] {candidate['name_raw']}: {note}")
            entry = {**candidate, "name": "", "mk_id": None, "person_id": None, "knessets": [],
                     "profile": "none", "photo": None, "photo_source": None, "photo_url": None, "photo_credit": None, "wikipedia": None, "details": {}}
            if person:
                entry.update(name=f"{person['first_name']} {person['last_name']}", person_id=person["person_id"],
                             mk_id=person.get("mk_id"), knessets=person["knessets"], site_id=person.get("site_id"))
                entry["profile"] = profile_depth(person, db_mk_ids)
                if entry["profile"] == "bills" or max(config.PROTOCOL_KNESSET_NUMS) not in person["knessets"]:
                    print(f"  former MK [{page['position']}.{candidate['position']}] {candidate['name_raw']} → "
                          f"{entry['name']} ({person['person_id']}, Knessets {person['knessets']})")
            else:
                unmatched_names.append(entry)
            candidates.append(entry)
        parties.append({"id": page["position"], "letters": page.get("letters", ""), "name": page["name"],
                        "submitted_by": submitted_by(page["text"]), "gov_url": page["url"],
                        "ballot": f"ballots/{page['position']}.png"
                                  if (directory / "ballots" / f"{page['position']}.png").exists() else None,
                        "website": PARTY_WEBSITES.get(page["position"]),
                        "logo": f"logos/{page['position']}.png"
                                if (directory / "logos" / f"{page['position']}.png").exists() else None,
                        "candidates": candidates})

    title_options = {id(entry): _name_splits(entry["name_raw"], first_names) for entry in unmatched_names}
    wiki = wikipedia_pages(sorted({title for options in title_options.values() for title in options}
                                  | {party_candidate["name"] for party in parties
                                     for party_candidate in party["candidates"] if party_candidate["name"]}))
    for party in parties:
        for entry in party["candidates"]:
            options = [entry["name"]] if entry["name"] else title_options[id(entry)]
            article = next((wiki[title] for title in options if title in wiki and any(
                word in wiki[title]["extract"] for word in _WIKIPEDIA_POLITICS_WORDS)), None)
            if not entry["name"]:
                entry["name"] = article["title"].split(" (")[0] if article else options[0]
            if article:
                entry["wikipedia"] = article["url"]
            photo_path = directory / "photos" / f"{party['id']}_{entry['position']}.jpg"
            photo_url = None
            if entry.get("site_id"):
                try:
                    photo_url, entry["details"] = knesset_site_details(entry["site_id"])
                except Exception as exc:
                    print(f"  Knesset site details failed for {entry['name']} ({entry['site_id']}): {exc}")
                if photo_url:
                    entry["photo_source"] = "knesset"
            if photo_url and download(photo_url, photo_path):
                entry["photo"] = f"photos/{photo_path.name}"
                entry["photo_url"] = photo_url
                entry["photo_credit"] = embedded_photo_credit(photo_path)
            else:
                entry["photo_source"] = None
        leader = party["candidates"][0]["name"] if party["candidates"] else ""
        party["leader"] = leader
        print(f"[{party['id']}] {party['name']}: {len(party['candidates'])} candidates, "
              f"{sum(c['profile'] == 'full' for c in party['candidates'])} full profiles, "
              f"{sum(c['profile'] == 'bills' for c in party['candidates'])} former MKs, "
              f"{sum(bool(c['photo']) for c in party['candidates'])} photos")

    payload = {"source": INDEX_URL, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "parties": parties}
    (directory / "lists.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {directory / 'lists.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=("scrape", "logos", "build"))
    args = parser.parse_args()
    {"scrape": scrape, "logos": logos, "build": build}[args.step]()


if __name__ == "__main__":
    main()
