from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from plex_queue.cache import TtlCache

SUGGEST = "https://v3.sg.media-imdb.com/suggestion/x/"
SHOW_IDS = {"tvseries", "tvminiseries", "tvepisode"}
MOVIE_IDS = {"movie", "tvmovie", "short", "video", "tvspecial"}

# Franchises whose official titles do not all share the same prefix.
KNOWN_COLLECTIONS = {
    ("dark", "knight"): (
        "tt0372784",  # Batman Begins
        "tt0468569",  # The Dark Knight
        "tt1345836",  # The Dark Knight Rises
    ),
    ("batman", "dark", "knight"): (
        "tt0372784",
        "tt0468569",
        "tt1345836",
    ),
    ("before",): (
        "tt0107048",  # Before Sunrise
        "tt0381681",  # Before Sunset
        "tt2209418",  # Before Midnight
    ),
    ("cornetto"): (
        "tt0372863",  # Shaun of the Dead
        "tt0425112",  # Hot Fuzz
        "tt1637725",  # The World's End
    ),
    ("three", "flavours", "cornetto"): (
        "tt0372863",
        "tt0425112",
        "tt1637725",
    ),
    # Titles are "The Bourne …" except the 2016 entry "Jason Bourne".
    ("bourne",): (
        "tt0258463",  # The Bourne Identity
        "tt0372183",  # The Bourne Supremacy
        "tt0440963",  # The Bourne Ultimatum
        "tt1194173",  # The Bourne Legacy
        "tt1232829",  # Jason Bourne
    ),
    ("jason", "bourne"): (
        "tt0258463",
        "tt0372183",
        "tt0440963",
        "tt1194173",
        "tt1232829",
    ),
}


@dataclass(frozen=True)
class Identity:
    imdb_id: str
    title: str
    year: int | None
    kind: str


_IMDB_CACHE: TtlCache[Identity] = TtlCache(900)


def fetch_collection(name: str, timeout: int = 20, lookup=None) -> list[Identity]:
    """Movies in this franchise, oldest first.

    Uses a small map when titles do not share a prefix (Dark Knight, Bourne,
    Cornetto), otherwise keeps IMDB suggestion rows whose titles start with
    the query. If a multi-word query only finds one hit, retries with the last
    word so "jason bourne" still finds "The Bourne Identity".
    """
    from plex_queue.titles import tokenize

    resolve = lookup or fetch_imdb
    known = known_collection(name, resolve, timeout=timeout)
    if known:
        return known
    rows = suggest_rows(name, timeout=timeout)
    found = collection_from_rows(name, rows)
    words = tokenize(name)
    if len(found) <= 1 and len(words) > 1:
        short = words[-1]
        wider = collection_from_rows(short, suggest_rows(short, timeout=timeout))
        if len(wider) > len(found):
            return wider
    return found


def known_collection(name: str, lookup, timeout: int = 20) -> list[Identity]:
    from plex_queue.titles import tokenize

    key = tuple(tokenize(name))
    ids = KNOWN_COLLECTIONS.get(key)
    if not ids:
        return []
    found = []
    seen = set()
    for imdb_id in ids:
        if imdb_id in seen:
            continue
        try:
            identity = lookup(imdb_id, timeout=timeout)
        except TypeError:
            identity = lookup(imdb_id)
        if identity is None or identity.kind != "movie":
            continue
        seen.add(imdb_id)
        found.append(identity)
    found.sort(key=lambda item: (item.year or 0, item.title.casefold()))
    return found


def suggest_rows(name: str, timeout: int = 20) -> list[dict]:
    safe = urllib.parse.quote(name.strip().lower())
    req = urllib.request.Request(
        SUGGEST + safe + ".json",
        headers={"User-Agent": "plex-queue"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.load(response)
    return list(data.get("d") or [])


def collection_from_rows(name: str, rows: list[dict]) -> list[Identity]:
    from plex_queue.titles import franchise_match, tokenize

    query = tokenize(name)
    found = []
    seen = set()
    for row in rows:
        if str(row.get("qid") or "").lower() != "movie":
            continue
        imdb_id = str(row.get("id") or "").lower()
        title = str(row.get("l") or "").strip()
        year = _year(row.get("y"))
        if not imdb_id.startswith("tt") or not title or not year or imdb_id in seen:
            continue
        if not franchise_match(query, tokenize(title)):
            continue
        seen.add(imdb_id)
        found.append(Identity(imdb_id, title, year, "movie"))
    found.sort(key=lambda item: (item.year or 0, item.title.casefold()))
    return found


def fetch_imdb(imdb_id: str, timeout: int = 20) -> Identity | None:
    wanted = imdb_id.lower()
    cached = _IMDB_CACHE.get(wanted)
    if cached is not None:
        return cached
    safe = urllib.parse.quote(wanted)
    req = urllib.request.Request(
        SUGGEST + safe + ".json",
        headers={"User-Agent": "plex-queue"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.load(response)
    for row in data.get("d") or []:
        if str(row.get("id") or "").lower() != wanted:
            continue
        title = str(row.get("l") or "").strip()
        kind = _kind(row)
        if not title or not kind:
            return None
        identity = Identity(wanted, title, _year(row.get("y")), kind)
        _IMDB_CACHE.set(wanted, identity)
        return identity
    return None


def _kind(row: dict) -> str | None:
    qid = str(row.get("qid") or "").lower()
    if qid in SHOW_IDS:
        return "show"
    if qid in MOVIE_IDS:
        return "movie"
    label = str(row.get("q") or "").lower()
    if "series" in label or label.startswith("tv "):
        return "show"
    if "feature" in label or "movie" in label:
        return "movie"
    return None


def _year(value) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
