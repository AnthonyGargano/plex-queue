from __future__ import annotations

import re
from dataclasses import dataclass

TAGS = {
    "1080p", "720p", "2160p", "480p", "576p", "4k", "uhd", "hdr", "hdr10",
    "dv", "sdr", "bluray", "brrip", "bdrip", "webrip", "webdl", "web", "hdtv",
    "hdrip", "dvdrip", "dvd", "remux", "x264", "x265", "h264", "h265", "hevc",
    "avc", "aac", "ac3", "dts", "truehd", "atmos", "ddp", "amzn", "nf", "hmax",
    "dsnp", "atvp", "extended", "proper", "repack", "internal", "limited",
    "unrated", "theatrical", "directors", "cut", "multi", "subs", "subbed",
    "dubbed", "10bit", "8bit", "yts", "yify", "rarbg", "ettv", "eztv", "cam", "hdts",
    "hdcam", "complete", "season", "pack", "hc", "sparks", "flux", "ntb", "cakes",
}
ARTICLES = {"a", "an", "the", "and"}

YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
SEASON_EP = re.compile(r"\bs(\d{1,2})e(\d{1,2})\b", re.IGNORECASE)
SEASON_ONLY = re.compile(r"\bs(\d{1,2})\b", re.IGNORECASE)
X_EP = re.compile(r"\b(\d{1,2})x(\d{2})\b", re.IGNORECASE)
SEASON_WORD = re.compile(r"\bseason\s+(\d{1,2})\b", re.IGNORECASE)
EP_WORD = re.compile(r"\bepisode\s+(\d{1,2})\b", re.IGNORECASE)
HEIGHT = re.compile(r"\b(2160|1080|720|576|480)[pi]\b|\b4k\b|\buhd\b", re.IGNORECASE)
COLLECTION_WORD = re.compile(
    r"\b(trilogy|duology|quadrilogy|quadriology|saga|anthology|collection|boxset|box\s+set)\b",
    re.IGNORECASE,
)
# Optional "the" so "all of ted lasso" and "all of the office" both work.
SET_OF = re.compile(r"^(?:all|every)\s+of\s+(?:the\s+)?(.+)$", re.IGNORECASE)
SET_KIND = re.compile(
    r"^(?:all|every)\s+(?:the\s+)?(.+?)\s+(?:movies|movie|films|film)$",
    re.IGNORECASE,
)
SET_SHOW_KIND = re.compile(
    r"^(?:all|every)\s+(?:the\s+)?(.+?)\s+(?:tv\s+shows?|tv\s+series|series)$",
    re.IGNORECASE,
)
ALL_SEASONS = re.compile(
    r"^(?:all|every)\s+seasons?\s+(?:of\s+(?:the\s+)?)?(.+)$",
    re.IGNORECASE,
)
TRAILING_SET_MOVIE = re.compile(
    r"\s+(?:movies|movie|films|film)$",
    re.IGNORECASE,
)
TRAILING_SET_SHOW = re.compile(
    r"\s+(?:tv\s+shows?|tv\s+series|series|shows?)$",
    re.IGNORECASE,
)
SET_LIMIT = {"trilogy": 3, "duology": 2, "quadrilogy": 4, "quadriology": 4}
MAX_SHOW_SEASONS = 40
RESOLUTION_NUMBERS = {"480", "576", "720", "1080", "2160"}
ROMAN_SEQUEL = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8",
    "ix": "9", "xi": "11", "xii": "12",
}
IMDB_ID = re.compile(r"\b(tt\d{7,8})\b", re.IGNORECASE)
IMDB_URL = re.compile(r"https?://(?:www\.)?imdb\.com/\S*", re.IGNORECASE)
LEADING_KIND = re.compile(
    r"^\s*(movie|film|tv\s+show|tv\s+series|tv|series|show)\b[\s:,\-]*",
    re.IGNORECASE,
)
TRAILING_MOVIE = re.compile(r"[\s:,\-]*(movie|film)\s*$", re.IGNORECASE)
TRAILING_SHOW = re.compile(
    r"[\s:,\-]*(tv\s+shows?|tv\s+series|series)\s*$",
    re.IGNORECASE,
)
KIND_ONLY = re.compile(
    r"^(?:movie|film|tv\s+show|tv\s+series|tv|series|show)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Request:
    raw: str
    tokens: tuple[str, ...]
    year: int | None
    season: int | None
    episode: int | None
    kind: str | None = None
    imdb_id: str | None = None
    collection: bool = False
    franchise: str | None = None
    collection_limit: int | None = None


def is_year(token: str) -> bool:
    return bool(re.fullmatch(r"(?:19|20)\d{2}", token))


def screen_height(width: int | None, height: int | None) -> int | None:
    """The resolution Plex shows, not the raw frame height.

    A 2.35:1 movie in a 1080p file is often 1920x796. Plex calls that 1080p
    because the picture is 1920 wide.
    """
    wide = int(width or 0)
    high = int(height or 0)
    if wide >= 3840 or high >= 2160:
        return 2160
    if wide >= 1920 or high >= 1080:
        return 1080
    if wide >= 1280:
        return 720
    if high > 0:
        return high
    return None


def video_height(name: str) -> int | None:
    match = HEIGHT.search(name)
    if not match:
        return None
    token = match.group(0).lower()
    if token in {"4k", "uhd"}:
        return 2160
    return int(match.group(1))


def tokenize(text: str) -> list[str]:
    text = text.lower().replace("&", " and ")
    text = SEASON_EP.sub(" ", text)
    text = X_EP.sub(" ", text)
    text = SEASON_ONLY.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = []
    for token in text.split():
        if token in TAGS or token in ARTICLES:
            continue
        token = ROMAN_SEQUEL.get(token, token)
        if token.isdigit() and not is_year(token):
            if token in RESOLUTION_NUMBERS or int(token) > 30:
                continue
        tokens.append(token)
    return tokens


def kind_word(text: str) -> str | None:
    """A reply that is only 'movie' or 'show', with no title in it."""
    match = KIND_ONLY.fullmatch(text.strip())
    if not match:
        return None
    return _kind_label(match.group(0))


def parse_request(text: str) -> Request:
    raw = text.strip()
    imdb_id = None
    match = IMDB_ID.search(raw)
    if match:
        imdb_id = match.group(1).lower()
        work = IMDB_URL.sub(" ", raw)
        work = IMDB_ID.sub(" ", work)
    else:
        work = raw
    explicit = _explicit_kind(work)
    # Resolve "all of the …" before stripping trailing kind words so
    # "all of the office tv show" keeps enough context to mark a show set.
    subject, set_kind = _set_subject(work)
    limit = None
    if subject is not None:
        work = subject
        collection = True
        if set_kind and explicit is None:
            explicit = set_kind
    else:
        collection = False
        work = LEADING_KIND.sub(" ", work)
        work = TRAILING_MOVIE.sub(" ", work)
        work = TRAILING_SHOW.sub(" ", work)
    season = episode = year = None
    match = SEASON_EP.search(work)
    if match:
        season, episode = int(match.group(1)), int(match.group(2))
        work = SEASON_EP.sub(" ", work)
    else:
        match = X_EP.search(work)
        if match:
            season, episode = int(match.group(1)), int(match.group(2))
            work = X_EP.sub(" ", work)
        else:
            match = SEASON_WORD.search(work)
            if match:
                season = int(match.group(1))
                work = SEASON_WORD.sub(" ", work)
            match = EP_WORD.search(work)
            if match:
                episode = int(match.group(1))
                work = EP_WORD.sub(" ", work)
            if season is None:
                match = SEASON_ONLY.search(work)
                if match:
                    season = int(match.group(1))
                    work = SEASON_ONLY.sub(" ", work, count=1)
    match = YEAR.search(work)
    if match:
        year = int(match.group(0))
        work = YEAR.sub(" ", work)
    limited = COLLECTION_WORD.search(work)
    if limited:
        collection = True
        limit = SET_LIMIT.get(limited.group(1).lower())
        work = COLLECTION_WORD.sub(" ", work)
    franchise = " ".join(work.split()) if collection else None
    # An episode number without a season is incomplete for matching and search.
    if episode is not None and season is None:
        episode = None
    if season or episode:
        kind = None if explicit == "movie" else "show"
    elif explicit:
        kind = explicit
    elif collection and limit is not None:
        # "matrix trilogy" and similar are movie sets.
        kind = "movie"
    else:
        # Bare "all of X" stays unclear so the bot can ask movie vs TV show.
        kind = None
    return Request(
        raw, tuple(tokenize(work)), year, season, episode, kind, imdb_id,
        collection, franchise, limit,
    )


def _set_subject(text: str) -> tuple[str | None, str | None]:
    """Franchise inside 'all of …' phrases, plus movie/show when the phrase names it."""
    stripped = text.strip()
    match = ALL_SEASONS.match(stripped)
    if match:
        return (_normalize_set_name(match.group(1)), "show")
    # Prefer "all of …" before "all … tv show" so "all of ted lasso tv show"
    # does not capture a leading "of" as part of the title.
    match = SET_OF.match(stripped)
    if match:
        has_the = bool(
            re.match(r"^(?:all|every)\s+of\s+the\s+", stripped, flags=re.IGNORECASE)
        )
        subject = match.group(1).strip()
        kind = None
        movie = TRAILING_SET_MOVIE.search(subject)
        show = TRAILING_SET_SHOW.search(subject)
        if movie and (not show or movie.start() >= show.start()):
            subject = TRAILING_SET_MOVIE.sub("", subject)
            kind = "movie"
        elif show:
            subject = TRAILING_SET_SHOW.sub("", subject)
            kind = "show"
        # Keep "all of me" / "all of me movie" as a normal title unless "the" or 2+ words.
        if not has_the and len(subject.split()) < 2:
            pass
        else:
            return (_normalize_set_name(subject), kind)
    match = SET_SHOW_KIND.match(stripped)
    if match:
        raw_subject = match.group(1).strip()
        # "all of me tv show" should not become a one-title "me" show set.
        if _false_of_singleton(raw_subject):
            pass
        else:
            return (_normalize_set_name(raw_subject), "show")
    match = SET_KIND.match(stripped)
    if not match:
        return None, None
    raw_subject = match.group(1).strip()
    # "all of me movie" should not become a one-film "me" collection.
    if _false_of_singleton(raw_subject):
        return None, None
    return (_normalize_set_name(raw_subject), "movie")


def _false_of_singleton(raw_subject: str) -> bool:
    """True when a set regex captured 'of <one word>' from 'all of <title> …'."""
    if not re.match(r"^of\s+", raw_subject.strip(), flags=re.IGNORECASE):
        return False
    cleaned = _normalize_set_name(raw_subject)
    return bool(cleaned and len(cleaned.split()) < 2)


def _normalize_set_name(subject: str) -> str | None:
    text = subject.strip()
    # Defensive: SET_SHOW_KIND can still capture a leading "of" on odd phrasing.
    text = re.sub(r"^of\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bstories\b", "story", text, flags=re.IGNORECASE)
    text = text.strip(" -")
    return text or None


def _explicit_kind(text: str) -> str | None:
    match = LEADING_KIND.match(text)
    if match:
        return _kind_label(match.group(1))
    match = TRAILING_MOVIE.search(text)
    if match:
        return "movie"
    match = TRAILING_SHOW.search(text)
    if match:
        return "show"
    return None


def _kind_label(word: str) -> str:
    token = re.sub(r"\s+", " ", word.strip().lower())
    if token in {"movie", "film"}:
        return "movie"
    return "show"


def phrase_match(query_tokens: tuple[str, ...] | list[str], title_words: list[str]) -> bool:
    """True when the query is the whole title, or a franchise prefix sits in front of it.

    Words after the query are a different work ("matrix" does not match
    "matrix reloaded"). A leading franchise does match ("force awakens"
    matches "star wars the force awakens").
    """
    words = [word for word in title_words if not is_year(word)]
    query = [word for word in query_tokens if not is_year(word)]
    if not query or len(query) > len(words):
        return False
    width = len(query)
    for start in range(len(words) - width + 1):
        if words[start:start + width] == query and not words[start + width:]:
            return True
    return False


def franchise_match(query_tokens: tuple[str, ...] | list[str], title_words: list[str]) -> bool:
    """True when the title starts with the query and may continue with a sequel."""
    words = [word for word in title_words if not is_year(word)]
    query = [word for word in query_tokens if not is_year(word)]
    if not query or len(query) > len(words):
        return False
    return words[:len(query)] == list(query)
