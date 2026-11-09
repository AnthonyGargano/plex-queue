from __future__ import annotations

import json
import urllib.parse
import urllib.request

from plex_queue.cache import TtlCache
from plex_queue.models import Hit
from plex_queue.titles import franchise_match, parse_request, phrase_match, tokenize

TRACKER = "udp://tracker.opentrackr.org:1337/announce"
EMPTY_HASH = "0" * 40
_HITS_CACHE: TtlCache[list[Hit]] = TtlCache(900)


def matching_hits(query: str, hits: list[Hit]) -> list[Hit]:
    request = parse_request(query)
    if not request.tokens:
        return []
    matched = []
    for hit in hits:
        hit_words = tokenize(hit.name)
        if request.season is not None:
            hit_parsed = parse_request(hit.name)
            if hit_parsed.season != request.season:
                continue
            if (
                request.episode is not None
                and hit_parsed.episode is not None
                and hit_parsed.episode != request.episode
            ):
                continue
            # Episode names and release groups trail the show title; allow that
            # for season/episode requests (packs still match as a prefix).
            if not franchise_match(request.tokens, hit_words):
                continue
            matched.append(hit)
            continue
        if phrase_match(request.tokens, hit_words):
            matched.append(hit)
    return matched


def magnet_uri(hit: Hit) -> str:
    name = urllib.parse.quote(hit.name, safe="")
    tracker = urllib.parse.quote(TRACKER, safe="")
    return f"magnet:?xt=urn:btih:{hit.info_hash}&dn={name}&tr={tracker}"


def fetch_hits(
    query: str,
    endpoint: str,
    timeout: int = 30,
    *,
    match_as: str | None = None,
) -> list[Hit]:
    """Fetch torrents for ``query``, then keep hits matching ``match_as`` or ``query``.

    Season fills pass the bare show title as ``query`` (larger index page) and
    ``match_as=\"Show S04\"`` so episode releases are not dropped by a narrow API
    search string.
    """
    filter_query = match_as or query
    cache_key = (
        endpoint,
        " ".join(query.lower().split()),
        " ".join(filter_query.lower().split()),
    )
    cached = _HITS_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    url = endpoint + "?" + urllib.parse.urlencode({"q": query})
    req = urllib.request.Request(url, headers={"User-Agent": "plex-queue"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        rows = json.load(response)
    hits = []
    for row in rows:
        info_hash = str(row.get("info_hash") or "")
        if not info_hash or info_hash == EMPTY_HASH or str(row.get("id")) == "0":
            continue
        hits.append(
            Hit(
                name=str(row.get("name") or info_hash),
                info_hash=info_hash.lower(),
                seeders=int(row.get("seeders") or 0),
                size=int(row.get("size") or 0),
            )
        )
    matched = matching_hits(filter_query, hits)
    # Do not cache empty results: a transient empty API reply must not block
    # queuing for the full TTL.
    if matched:
        _HITS_CACHE.set(cache_key, matched)
    return list(matched)
