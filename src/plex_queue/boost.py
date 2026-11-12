from __future__ import annotations

import logging
import time
from collections import defaultdict

from plex_queue.models import Hit, Torrent
from plex_queue.place import cleaned_title, is_complete
from plex_queue.titles import parse_request, tokenize, video_height

log = logging.getLogger(__name__)

STUCK_MIN_AGE_SECONDS = 2 * 60 * 60
# Owner /boost skips the age wait — you're looking at Deluge and asking now.
MANUAL_STUCK_MIN_AGE_SECONDS = 0
STUCK_MAX_PROGRESS = 1.0
MAX_MAGNETS_PER_TITLE = 3


def is_stuck(
    torrent: Torrent,
    *,
    now: float | None = None,
    min_age_seconds: int = STUCK_MIN_AGE_SECONDS,
    max_progress: float = STUCK_MAX_PROGRESS,
) -> bool:
    """True when a torrent has barely started and has been idle long enough.

    ``min_age_seconds=0`` (manual /boost) only requires 0% progress and no
    download rate. Auto-boost keeps the multi-hour age gate.
    """
    if is_complete(torrent.state, torrent.progress):
        return False
    if torrent.progress >= max_progress:
        return False
    if torrent.rate > 0:
        return False
    age_needed = max(0, int(min_age_seconds))
    if age_needed <= 0:
        return True
    added = int(torrent.time_added or 0)
    if added <= 0:
        return False
    clock = time.time() if now is None else float(now)
    return (clock - added) >= age_needed


def stuck_torrents(
    torrents: list[Torrent],
    *,
    now: float | None = None,
    min_age_seconds: int = STUCK_MIN_AGE_SECONDS,
    max_progress: float = STUCK_MAX_PROGRESS,
) -> list[Torrent]:
    return [
        item
        for item in torrents
        if is_stuck(
            item,
            now=now,
            min_age_seconds=min_age_seconds,
            max_progress=max_progress,
        )
    ]


def title_group_key(
    *,
    kind: str | None,
    title: str,
    year: int | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> str | None:
    """Stable key for same-title Deluge grouping (movie/year or show/season/ep).

    Show keys require a season so S01 and S02 never share a cleanup group.
    """
    words = tokenize(cleaned_title(title) or title)
    if not words:
        return None
    joined = " ".join(words)
    if kind == "show" or season is not None:
        if season is None:
            return None
        if episode is not None:
            return f"show|{joined}|s{int(season):02d}|e{int(episode):02d}"
        return f"show|{joined}|s{int(season):02d}"
    year_bit = str(int(year)) if year else ""
    return f"movie|{joined}|{year_bit}"


def title_key_is_precise(key: str | None) -> bool:
    """True when a key is safe for same-title cleanup.

    Movies need a year. Shows need at least a season (episode optional so a
    season-pack group can clear incomplete packs for that season only).
    """
    if not key:
        return False
    parts = str(key).split("|")
    if parts[0] == "movie" and len(parts) >= 3 and parts[2].isdigit():
        return True
    if parts[0] == "show" and len(parts) >= 3 and parts[2].startswith("s"):
        return True
    return False


def title_group_key_from_label(label) -> str | None:
    if label is None:
        return None
    return title_group_key(
        kind=getattr(label, "kind", None),
        title=str(getattr(label, "title", "") or ""),
        year=getattr(label, "year", None),
        season=getattr(label, "season", None),
        episode=getattr(label, "episode", None),
    )


def title_group_key_for_torrent(ledger, torrent: Torrent) -> str | None:
    """Resolve a title key from ledger label/state, else parse the torrent name."""
    if ledger is not None:
        state = ledger.state(torrent.hash) or {}
        stored = str(state.get("title_key") or "").strip()
        if stored:
            return stored
        label = ledger.label(torrent.hash)
        keyed = title_group_key_from_label(label)
        if keyed:
            return keyed
    parsed, display = request_from_label_or_name(None, torrent.name)
    return title_group_key(
        kind=parsed.kind,
        title=display,
        year=parsed.year,
        season=parsed.season,
        episode=parsed.episode,
    )


def group_torrents_by_title(ledger, torrents: list[Torrent]) -> dict[str, list[Torrent]]:
    grouped: dict[str, list[Torrent]] = defaultdict(list)
    for torrent in torrents:
        key = title_group_key_for_torrent(ledger, torrent)
        if not key:
            continue
        grouped[key].append(torrent)
    return dict(grouped)


def pick_alternate_hit(
    hits: list[Hit],
    *,
    min_height: int,
    exclude_hashes: set[str] | frozenset[str],
) -> Hit | None:
    """Best seeded release at min_height that is not already in Deluge by hash."""
    blocked = {digest.lower() for digest in exclude_hashes}
    pool = []
    for hit in hits:
        digest = hit.info_hash.lower()
        if digest in blocked or hit.seeders <= 0:
            continue
        # Unknown resolution counts as 0 — same floor as decide()/queue paths.
        if (video_height(hit.name) or 0) < min_height:
            continue
        pool.append(hit)
    if not pool:
        return None
    return max(pool, key=lambda hit: hit.seeders)


def clear_boost_siblings(ledger, deluge, finished_hash: str) -> list[str]:
    """Remove incomplete Deluge peers linked in the finished import's boost_group.

    Only ledger ``boost_group`` members are eligible — never delete by title
    match alone. Same-title stuck torrents are linked into one group when
    boosted, so clearing the group still clears the Bourne-Identity-style set.
    """
    if ledger is None:
        return []
    finished = finished_hash.lower()
    try:
        torrents = {item.hash.lower(): item for item in deluge.list_torrents()}
    except Exception:
        log.exception("boost sibling cleanup could not list Deluge torrents")
        return []
    state = ledger.state(finished) or {}
    group = str(state.get("boost_group") or "").strip().lower()
    if not group:
        return []
    candidates = {
        digest for digest in ledger.boost_group_members(finished)
        if digest != finished
    }
    if not candidates:
        ledger.clear_boost_links(finished)
        return []

    notes = []
    remove_failed = False
    for digest in sorted(candidates):
        torrent = torrents.get(digest)
        if torrent is None:
            ledger.clear_boost_links(digest)
            continue
        if is_complete(torrent.state, torrent.progress):
            # Leave complete peers for their own import cycle; keep group so a
            # later retry can still see remaining incomplete members.
            remove_failed = True
            continue
        # Re-check this hash alone before delete — narrow the list/remove gap.
        try:
            fresh = {
                item.hash.lower(): item for item in deluge.list_torrents()
            }.get(digest)
        except Exception:
            fresh = torrent
        if fresh is None:
            ledger.clear_boost_links(digest)
            continue
        if is_complete(fresh.state, fresh.progress):
            remove_failed = True
            continue
        try:
            deluge.remove_hash(digest)
        except Exception as exc:
            log.warning(
                "failed to remove boost sibling %s: %s",
                digest[:6],
                type(exc).__name__,
            )
            notes.append(
                f"Could not remove stuck alternate {digest[:6]} "
                f"({torrent.name}): left it in Deluge."
            )
            remove_failed = True
            continue
        ledger.clear_boost_links(digest)
        notes.append(
            f"Removed incomplete alternate {digest[:6]} after "
            f"{finished[:6]} finished ({torrent.name})."
        )
    # Keep the finished torrent in the boost_group when a sibling delete failed
    # or a complete peer still shares the group, so import cycles can retry.
    if not remove_failed:
        # Confirm no incomplete group members remain before clearing finished.
        remaining = [
            digest for digest in ledger.boost_group_members(finished)
            if digest != finished
        ]
        still_incomplete = False
        try:
            live = {item.hash.lower(): item for item in deluge.list_torrents()}
        except Exception:
            live = torrents
        for digest in remaining:
            peer = live.get(digest)
            if peer is None:
                ledger.clear_boost_links(digest)
                continue
            if not is_complete(peer.state, peer.progress):
                still_incomplete = True
                break
        if not still_incomplete:
            ledger.clear_boost_links(finished)
    return notes


def request_from_label_or_name(label, torrent_name: str):
    """Build a Request for re-searching a stuck torrent's title."""
    from plex_queue.titles import Request

    if label is not None and label.kind in {"movie", "show"}:
        title = label.title or ""
        raw = title
        if label.kind == "show" and label.season and label.episode:
            raw = f"{title} S{label.season:02d}E{label.episode:02d}"
        elif label.kind == "show" and label.season:
            raw = f"{title} S{label.season:02d}"
        elif label.year:
            raw = f"{title} {label.year}"
        parsed = parse_request(raw)
        return Request(
            raw,
            parsed.tokens or tuple(str(title).split()),
            label.year if label.year is not None else parsed.year,
            label.season if label.season is not None else parsed.season,
            label.episode if label.episode is not None else parsed.episode,
            label.kind,
            parsed.imdb_id,
        ), title
    parsed = parse_request(torrent_name)
    kind = parsed.kind
    if kind not in {"movie", "show"}:
        kind = "show" if parsed.season is not None else "movie"
    display = " ".join(parsed.tokens) or torrent_name
    return Request(
        torrent_name,
        parsed.tokens,
        parsed.year,
        parsed.season,
        parsed.episode,
        kind,
        parsed.imdb_id,
    ), display
