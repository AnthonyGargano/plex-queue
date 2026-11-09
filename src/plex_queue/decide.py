from __future__ import annotations

from dataclasses import dataclass

from plex_queue.models import Copy, Hit
from plex_queue.titles import video_height


@dataclass(frozen=True)
class AlreadyHave:
    copy: Copy
    count: int
    height: int


@dataclass(frozen=True)
class UnknownQuality:
    copy: Copy
    count: int


@dataclass(frozen=True)
class NoUpgrade:
    copy: Copy
    height: int


@dataclass(frozen=True)
class Enqueue:
    hit: Hit
    replacing: Copy | None


@dataclass(frozen=True)
class NothingFound:
    pass


Decision = AlreadyHave | UnknownQuality | NoUpgrade | Enqueue | NothingFound


def decide(
    copies: list[Copy],
    hits: list[Hit],
    min_height: int = 1080,
    *,
    season_coverage: bool = False,
) -> Decision:
    """Decide whether to queue.

    For a single movie or episode, one copy at min_height is enough.
    For a whole season, every episode from 1 through the highest owned
    episode must be present at min_height, with no gaps.
    """
    if season_coverage:
        return _decide_season(copies, hits, min_height)
    return _decide_title(copies, hits, min_height)


def season_covered(copies: list[Copy], min_height: int) -> tuple[bool, Copy | None]:
    """True when episodes 1..N are all present and each is at least min_height.

    When the season is incomplete or below quality, the second value is the
    copy that should be retired after an upgrade (a below-min_height episode),
    never an already-good episode that happens to be the tallest in the set.
    """
    best_by_episode: dict[int, Copy] = {}
    for copy in copies:
        if not copy.episode:
            continue
        current = best_by_episode.get(copy.episode)
        if current is None or (copy.height or 0) > (current.height or 0):
            best_by_episode[copy.episode] = copy
    if not best_by_episode:
        return False, None
    representative = max(best_by_episode.values(), key=lambda copy: copy.height or 0)
    highest = max(best_by_episode)
    if set(best_by_episode) != set(range(1, highest + 1)):
        return False, _season_upgrade_target(best_by_episode, min_height)
    for copy in best_by_episode.values():
        if not copy.height or copy.height < min_height:
            return False, _season_upgrade_target(best_by_episode, min_height)
    return True, representative


def _season_upgrade_target(
    best_by_episode: dict[int, Copy],
    min_height: int,
) -> Copy | None:
    """Pick a below-quality episode to record as replace_path for /drop."""
    weak = [
        copy for copy in best_by_episode.values()
        if copy.height is not None and copy.height < min_height
    ]
    if weak:
        return min(weak, key=lambda copy: (copy.height or 0, copy.episode or 0))
    unknown = [copy for copy in best_by_episode.values() if not copy.height]
    if unknown:
        return min(unknown, key=lambda copy: copy.episode or 0)
    # Gaps only — every owned episode is already good enough; nothing to /drop.
    return None


def _decide_title(copies: list[Copy], hits: list[Hit], min_height: int) -> Decision:
    known = [copy for copy in copies if copy.height]
    unknown = [copy for copy in copies if not copy.height]
    best = max(known, key=lambda copy: copy.height or 0) if known else None
    if best and best.height and best.height >= min_height:
        return AlreadyHave(best, len(copies), best.height)
    if unknown and best is None:
        return UnknownQuality(unknown[0], len(copies))
    return _enqueue(copies, hits, min_height, best)


def _decide_season(copies: list[Copy], hits: list[Hit], min_height: int) -> Decision:
    covered, best = season_covered(copies, min_height)
    if covered and best and best.height:
        return AlreadyHave(best, len(copies), best.height)
    # A season with only unknown-height files is not "owned enough".
    # Prefer a better release when one exists.
    return _enqueue(copies, hits, min_height, best)


def _enqueue(
    copies: list[Copy],
    hits: list[Hit],
    min_height: int,
    best: Copy | None,
) -> Decision:
    pool = [hit for hit in hits if hit.seeders > 0]
    if best is not None:
        better = [
            hit for hit in pool
            if (video_height(hit.name) or 0) >= min_height
        ]
        if not better:
            return NoUpgrade(best, best.height or 0)
        pool = better
    else:
        good = [
            hit for hit in pool
            if (video_height(hit.name) or 0) >= min_height
        ]
        if good:
            pool = good
    if not pool:
        if copies and best is None:
            return UnknownQuality(copies[0], len(copies))
        return NothingFound()
    chosen = max(pool, key=lambda hit: hit.seeders)
    replacing = best if best is not None else None
    return Enqueue(chosen, replacing)
