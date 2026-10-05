"""
cache.py

Shared HTTP session with persistent disk caching (SQLite, 1-week TTL).
Import SESSION and use it instead of requests.get() for all Knesset API calls.
"""

import requests_cache
from config import CACHE_DB, CACHE_TTL, CACHE_TTL_VOTES

SESSION: requests_cache.CachedSession = requests_cache.CachedSession(
    str(CACHE_DB),
    backend="sqlite",
    expire_after=CACHE_TTL,
    urls_expire_after={"*/KNS_PlenumVote*": CACHE_TTL_VOTES},
)
