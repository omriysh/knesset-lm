"""
web/seo.py

robots.txt and sitemap.xml for search engines. The sitemap lists the pages worth indexing: the tabs, the
about pages, every party, every candidate with a profile, and every subject area and subject.
The research agent page is left out (it needs the visitor's Gemini key and has no content of its own).
"""

from urllib.parse import quote
from xml.sax.saxutils import escape

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

import config
from profiles import subjects
from retrieval import knesset_db_store as store
from web.profiles import ACTIVITY_KNESSET, candidate_lists

router = APIRouter()

SITEMAP_TAB_PATHS = ("/", "/game", "/profiles", "/subjects", config.PROTOCOLS_PAGE_PATH, config.CHAT_PAGE_PATH,
                     config.ABOUT_PAGE_PATH)


def _profile_paths() -> list[str]:
    paths = []
    for party in candidate_lists()["parties"]:
        paths.append(f"/profiles/party/{party['id']}")
        paths += [f"/profiles/party/{party['id']}/candidate/{c['position']}"
                  for c in party["candidates"] if c["profile"] != "none"]
    return paths


def _subject_paths() -> list[str]:
    conn = store.connect(interrupt_after_seconds=config.DB_QUERY_TIMEOUT_SECONDS)
    try:
        areas = subjects.subject_areas(conn, ACTIVITY_KNESSET)
    finally:
        conn.close()
    paths = []
    for area in areas:
        paths.append(f"/subjects/area/{quote(area['name'], safe='')}")
        paths += [f"/subjects/{subject['id']}" for subject in area["subjects"]]
    return paths


def sitemap_paths() -> list[str]:
    paths = list(SITEMAP_TAB_PATHS)
    paths += [f"{config.ABOUT_PAGE_PATH}/{tab}" for tab in config.ABOUT_SUBPAGE_TABS]
    try:
        paths += _profile_paths()
    except Exception as exc:
        print(f"[sitemap] candidate profiles skipped: {exc}", flush=True)
    try:
        paths += _subject_paths()
    except Exception as exc:
        print(f"[sitemap] subjects skipped: {exc}", flush=True)
    return paths


@router.api_route("/robots.txt", methods=["GET", "HEAD"], response_class=PlainTextResponse, include_in_schema=False)
def robots_txt():
    return f"User-agent: *\nAllow: /\n\nSitemap: {config.PUBLIC_SITE_URL}/sitemap.xml\n"


@router.api_route("/sitemap.xml", methods=["GET", "HEAD"], include_in_schema=False)
def sitemap_xml():
    urls = "".join(f"<url><loc>{escape(config.PUBLIC_SITE_URL + path)}</loc></url>\n" for path in sitemap_paths())
    body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n")
    return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})
