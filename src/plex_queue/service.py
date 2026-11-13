from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

from plex_queue.boost import (
    MANUAL_STUCK_MIN_AGE_SECONDS,
    MAX_MAGNETS_PER_TITLE,
    STUCK_MIN_AGE_SECONDS,
    clear_boost_siblings,
    group_torrents_by_title,
    is_stuck,
    pick_alternate_hit,
    request_from_label_or_name,
    title_group_key,
    title_group_key_from_label,
    title_key_is_precise,
)
from plex_queue.decide import AlreadyHave, Enqueue, NoUpgrade, NothingFound, UnknownQuality, decide
from plex_queue.errors import public_error
from plex_queue.imdb import Identity, fetch_collection, fetch_imdb
from plex_queue.library import fill_heights
from plex_queue.models import Copy, Torrent, bucket, torrent_line
from plex_queue.place import Label, cleaned_title, is_complete, under_roots
from plex_queue.search import magnet_uri
from plex_queue.titles import (
    MAX_SHOW_SEASONS,
    Request,
    all_of_me_title,
    film_show_title,
    franchise_match,
    parse_request,
    phrase_match,
    tokenize,
    video_height,
)
from plex_queue.events import duration_ms, emit, timed
from plex_queue.tidy import (
    apply_action,
    audit,
    format_report,
    upgrade_replace_paths,
)
from plex_queue.tidy_select import parse_batch, select_rows
from plex_queue.upgrades import RECYCLE_BIN, move_to_trash, path_key
import shutil

RECENT_WINDOWS = {
    "day": (timedelta(hours=24), "24 hours"),
    "24h": (timedelta(hours=24), "24 hours"),
    "today": (timedelta(hours=24), "24 hours"),
    "week": (timedelta(days=7), "7 days"),
    "month": (timedelta(days=30), "30 days"),
}


@dataclass(frozen=True)
class Clarify:
    query: str
    text: str


class Service:
    def __init__(
        self,
        library,
        search,
        deluge,
        min_height: int = 1080,
        access=None,
        ledger=None,
        roots: list[Path] | tuple[Path, ...] | None = None,
        trash_dir: Path | None = None,
        tidy_ledger=None,
        plex=None,
        tidy_buckets: dict | None = None,
        import_margin: int = 0,
        plex_db: Path | None = None,
        guest_max_active: int = 0,
    ):
        self.library = library
        self.search = search
        self.deluge = deluge
        self.min_height = min_height
        self.access = access
        self.ledger = ledger
        self.roots = tuple(Path(root) for root in (roots or ()))
        self.trash_dir = Path(trash_dir) if trash_dir else None
        self.tidy_ledger = tidy_ledger
        self.plex = plex
        self.tidy_buckets = dict(tidy_buckets or {})
        self.import_margin = int(import_margin or 0)
        self.plex_db = Path(plex_db) if plex_db else getattr(library, "plex_db", None)
        self.guest_max_active = max(0, int(guest_max_active or 0))
        self._lock = threading.RLock()

    def guest_active_count(self, torrents: list | None = None) -> int:
        """Count incomplete Deluge torrents (optional pre-fetched list)."""
        items = torrents if torrents is not None else self.deluge.list_torrents()
        return sum(
            1 for torrent in items
            if not is_complete(torrent.state, torrent.progress)
        )

    def guest_queue_blocked(
        self, extra: int = 1, *, active: int | None = None, torrents: list | None = None,
    ) -> str | None:
        """Return a user message when guests should not start more downloads.

        ``extra`` is how many magnets are about to be added (default one).
        Pass ``active`` to use a local running count (avoids stale re-lists and
        mid-loop false blocks after a batch was pre-checked).
        """
        max_active = self.guest_max_active
        if max_active <= 0:
            return None
        if active is None:
            active = self.guest_active_count(torrents)
        if active + max(1, int(extra)) > max_active:
            return (
                f"There are already {active} active downloads "
                f"(guest limit {max_active}). Try again later or ask the owner."
            )
        return None

    def request(
        self, query: str, kind: str | None = None, resolver=None,
        collection_resolver=None, brief: bool = False, as_guest: bool = False,
    ) -> str | Clarify:
        display = cleaned_title(query)
        parsed = parse_request(query)
        if parsed.imdb_id:
            lookup = resolver or fetch_imdb
            identity = lookup(parsed.imdb_id)
            if identity is None:
                return (
                    f"I couldn't look up {parsed.imdb_id}. "
                    "Paste the IMDB link again, or send the title and I'll ask "
                    "whether it is a movie or a TV show."
                )
            display = identity.title or display
            parsed = _from_identity(query, identity)
        elif kind in {"movie", "show"}:
            collection = parsed.collection
            franchise = parsed.franchise
            # Picking "TV show" without a season means fill the whole series —
            # do not ask again for "all" / S01.
            title = " ".join(
                str(display or " ".join(parsed.tokens)).split()
            )
            if (
                kind == "show"
                and parsed.season is None
                and parsed.episode is None
                and not collection
            ):
                collection = True
                franchise = franchise or title or None
            parsed = Request(
                parsed.raw, parsed.tokens, parsed.year, parsed.season,
                parsed.episode, kind, parsed.imdb_id, collection,
                franchise, parsed.collection_limit,
            )
        # Known films never season-fill, even after kind / pack cues.
        title = " ".join(str(display or " ".join(parsed.tokens)).split())
        me = all_of_me_title(title)
        if me and parsed.season is None and parsed.episode is None:
            parsed = Request(
                parsed.raw, tuple(tokenize(me)), parsed.year, parsed.season,
                parsed.episode, "movie", parsed.imdb_id, False, None,
                parsed.collection_limit,
            )
        else:
            film = film_show_title(title)
            if film and parsed.season is None and parsed.episode is None:
                parsed = Request(
                    parsed.raw, tuple(tokenize(film)), parsed.year,
                    parsed.season, parsed.episode, "movie", parsed.imdb_id,
                    False, None, parsed.collection_limit,
                )
        if parsed.collection and not brief:
            if parsed.kind == "show":
                return self._fill_show_seasons(parsed, as_guest=as_guest)
            if parsed.kind == "movie":
                return self._fill_gaps(
                    parsed, collection_resolver, resolver, as_guest=as_guest,
                )
            return _clarify(query)
        if parsed.kind not in {"movie", "show"}:
            return _clarify(query)
        if len("".join(parsed.tokens)) < 3:
            return "Tell me a bit more of the title."

        with self._lock:
            self._ensure_library()
            copies = list(self.library.find(parsed))
        copies = fill_heights(copies)

        if parsed.kind == "show" and parsed.season is None and parsed.episode is None:
            return _need_show_season(display or " ".join(parsed.tokens), copies, brief)
        season_coverage = (
            parsed.kind == "show"
            and parsed.season is not None
            and parsed.episode is None
        )
        decision = decide(copies, [], self.min_height, season_coverage=season_coverage)
        if isinstance(decision, AlreadyHave):
            return _already(
                decision,
                brief,
                season=parsed.season if season_coverage else None,
                reveal_paths=not as_guest,
            )

        hits = self._search_hits(parsed, display)

        # Re-read after network I/O so a concurrent import is not missed.
        with self._lock:
            fresh = list(self.library.find(parsed))
        known_heights = {copy.path: copy.height for copy in copies if copy.height}
        copies = fill_heights(
            [
                copy if copy.height or copy.path not in known_heights
                else replace(copy, height=known_heights[copy.path])
                for copy in fresh
            ]
        )
        season_label = parsed.season if season_coverage else None
        episode_hits: list = []
        if season_coverage:
            packs, episode_hits = _split_season_hits(hits, parsed.season)
            good_packs = [
                hit for hit in packs
                if hit.seeders > 0
                and (video_height(hit.name) or 0) >= self.min_height
            ]
            if good_packs:
                hits = good_packs
            elif episode_hits:
                return self._queue_season_episodes(
                    parsed, display, copies, episode_hits, brief, as_guest=as_guest,
                )
        decision = decide(copies, hits, self.min_height, season_coverage=season_coverage)
        if isinstance(decision, AlreadyHave):
            return _already(
                decision, brief, season=season_label, reveal_paths=not as_guest,
            )
        if isinstance(decision, UnknownQuality):
            shown = _season_copy_label(decision.copy, season_label)
            if brief:
                return f"Found a copy of unknown quality — {shown}"
            lines = [
                "I found a copy, but I can't tell whether it is 1080p, so I did not queue another one.",
                shown,
            ]
            if not as_guest:
                lines.append(decision.copy.path)
            return "\n".join(lines)
        if isinstance(decision, NoUpgrade):
            if season_coverage and episode_hits:
                return self._queue_season_episodes(
                    parsed, display, copies, episode_hits, brief, as_guest=as_guest,
                )
            shown = _season_copy_label(decision.copy, season_label)
            if brief:
                return (
                    f"Library copy is {decision.height}p, nothing better found — {shown}"
                )
            return (
                f"The library copy is {decision.height}p, and nothing 1080p or higher is available.\n"
                f"{shown}\nCheck Plex for the copy you have."
            )
        if isinstance(decision, NothingFound):
            if season_coverage and episode_hits:
                return self._queue_season_episodes(
                    parsed, display, copies, episode_hits, brief, as_guest=as_guest,
                )
            if brief:
                return "Nothing found to queue"
            if copies:
                return f"I found {copies[0].label}, but nothing new to queue."
            return "Nothing matched that title."
        assert isinstance(decision, Enqueue)
        with self._lock:
            # Final library check under the Deluge lock window.
            latest = fill_heights(list(self.library.find(parsed)))
            final = decide(latest, [], self.min_height, season_coverage=season_coverage)
            if isinstance(final, AlreadyHave):
                return _already(
                    final,
                    brief,
                    season=parsed.season if season_coverage else None,
                    reveal_paths=not as_guest,
                )
            existing = _already_in_deluge(
                self.deluge.list_torrents(),
                decision.hit,
                self.min_height,
                season=parsed.season,
                episode=parsed.episode,
            )
            if existing:
                self._remember(existing.hash, parsed, display, decision.replacing)
                shown = _pretty_name(existing.name)
                text = (
                    f"Already in Deluge ({existing.state}, {existing.progress:.0f}%): {shown}"
                )
                return text if brief else self._noted(text)
            if as_guest:
                blocked = self.guest_queue_blocked()
                if blocked:
                    return "Guest download limit reached" if brief else blocked
            self.deluge.add_magnet(magnet_uri(decision.hit))
            self._remember(decision.hit.info_hash, parsed, display, decision.replacing)

        shown = _pretty_name(decision.hit.name)
        if decision.replacing and decision.replacing.height:
            lead = (
                f"Library copy is {decision.replacing.height}p. "
                f"Queued a higher-quality copy ({decision.hit.seeders} seeders):"
            )
            brief_text = (
                f"Queued (upgrade from {decision.replacing.height}p) — {shown}"
            )
        else:
            lead = f"Not in the library. Queued ({decision.hit.seeders} seeders):"
            brief_text = f"Queued (not on Plex) — {shown}"
        text = f"{lead}\n{decision.hit.info_hash[:6]}  {shown}"
        if brief:
            return brief_text
        return self._noted(text)

    def _search_hits(self, parsed: Request, display: str):
        """Search the torrent index; season fills use a broad title query."""
        titled = display.strip() or " ".join(parsed.tokens)
        if (
            parsed.kind == "show"
            and parsed.season is not None
            and parsed.episode is None
        ):
            # Bare title hits the larger index page; match_as keeps only this season.
            return self.search.search(
                titled,
                match_as=f"{titled} S{parsed.season:02d}",
            )
        return self.search.search(_search_text(parsed, display))

    def _queue_season_episodes(
        self,
        parsed: Request,
        display: str,
        copies: list,
        episode_hits: list,
        brief: bool,
        as_guest: bool = False,
    ) -> str:
        """Queue the best release for each missing episode in a season."""
        owned = {
            copy.episode
            for copy in copies
            if copy.episode
            and copy.height
            and copy.height >= self.min_height
        }
        # Best owned file per episode — used as replace_path when upgrading a
        # weak episode that never got a season pack.
        copy_by_episode: dict[int, object] = {}
        for copy in copies:
            if not copy.episode:
                continue
            current = copy_by_episode.get(copy.episode)
            if current is None or (copy.height or 0) > (current.height or 0):
                copy_by_episode[copy.episode] = copy
        best_by_ep: dict[int, object] = {}
        for hit in episode_hits:
            if hit.seeders <= 0:
                continue
            height = video_height(hit.name)
            if height is not None and height < self.min_height:
                continue
            ep = parse_request(hit.name).episode
            if ep is None or ep in owned:
                continue
            current = best_by_ep.get(ep)
            if current is None or hit.seeders > current.seeders:
                best_by_ep[ep] = hit
        if not best_by_ep:
            if brief:
                return "Nothing found to queue"
            if copies:
                return f"I found {copies[0].label}, but nothing new to queue."
            return "Nothing matched that title."

        queued: list[tuple[int, object]] = []
        pending: list[tuple[int, object]] = []
        with self._lock:
            torrents = self.deluge.list_torrents()
            planned: list[tuple[int, object, object | None]] = []
            for ep in sorted(best_by_ep):
                hit = best_by_ep[ep]
                replacing = copy_by_episode.get(ep)
                if (
                    replacing is not None
                    and replacing.height
                    and replacing.height >= self.min_height
                ):
                    replacing = None
                existing = _already_in_deluge(
                    torrents, hit, self.min_height, season=parsed.season, episode=ep,
                )
                if existing:
                    self._remember(existing.hash, parsed, display, replacing, episode=ep)
                    pending.append((ep, existing))
                    continue
                planned.append((ep, hit, replacing))
            # Guests: all-or-nothing against a local active count so mid-loop
            # re-lists cannot under-count or claim the limit after a partial add.
            local_active = self.guest_active_count(torrents)
            if as_guest and planned:
                blocked = self.guest_queue_blocked(
                    extra=len(planned), active=local_active,
                )
                if blocked:
                    return blocked if not brief else "Guest download limit reached"
            for ep, hit, replacing in planned:
                self.deluge.add_magnet(magnet_uri(hit))
                self._remember(hit.info_hash, parsed, display, replacing, episode=ep)
                torrents.append(
                    Torrent(hit.info_hash, hit.name, "Queued", 0, 0, 0)
                )
                local_active += 1
                queued.append((ep, hit))

        shown = display.strip() or " ".join(parsed.tokens)
        season = parsed.season or 0
        if not queued and pending:
            codes = ", ".join(f"E{ep:02d}" for ep, _ in pending)
            text = (
                f"Already in Deluge for {shown} S{season:02d}: {codes}"
            )
            return text if brief else self._noted(text)
        codes = ", ".join(f"E{ep:02d}" for ep, _ in queued)
        if brief:
            return f"Queued {len(queued)} episode(s) — {shown} S{season:02d} {codes}"
        lines = [
            f"No season pack found. Queued {len(queued)} episode(s) "
            f"of {shown} S{season:02d}:"
        ]
        for ep, hit in queued:
            lines.append(f"E{ep:02d}  {hit.info_hash[:6]}  {_pretty_name(hit.name)}")
        if pending:
            lines.append(
                "Already in Deluge: "
                + ", ".join(f"E{ep:02d}" for ep, _ in pending)
            )
        return self._noted("\n".join(lines))

    def _fill_gaps(
        self, parsed: Request, collection_resolver, resolver, as_guest: bool = False,
    ) -> str:
        """Check each movie in the set; queue ones that are missing or below quality."""
        name = parsed.franchise or " ".join(parsed.tokens)
        if len("".join(parsed.tokens)) < 3:
            return "Tell me which movies you mean."
        lookup = collection_resolver or fetch_collection
        try:
            members = list(lookup(name))
        except (TimeoutError, OSError):
            return "I couldn't look up that set of movies. Try again in a minute."
        if parsed.collection_limit:
            members = members[:parsed.collection_limit]
        members = _franchise_members(name, members)
        if not members:
            return (
                f'I couldn\'t find the movies in "{name}". '
                "Send a specific title or an IMDB link."
            )
        owned = queued = pending = blocked = 0
        lines = []
        for member in members:
            label = member.title if not member.year else f"{member.title} ({member.year})"
            query = member.title if not member.year else f"{member.title} {member.year}"
            result = self.request(
                query, kind="movie", resolver=resolver, brief=True, as_guest=as_guest,
            )
            if "Already on Plex" in result:
                owned += 1
            elif "Already in Deluge" in result:
                pending += 1
            elif result.startswith("Queued"):
                queued += 1
            else:
                blocked += 1
            lines.append(f"{label}: {result}")
        head = _collection_head(len(members), owned, queued, blocked, pending)
        intro = f'Found {len(members)} movie(s) for "{name}":'
        return intro + "\n" + "\n".join(lines) + "\n\n" + head

    def _fill_show_seasons(self, parsed: Request, as_guest: bool = False) -> str:
        """Check each season of a show; queue missing or below-quality season packs."""
        name = parsed.franchise or " ".join(parsed.tokens)
        shown = " ".join(name.split())
        if len("".join(parsed.tokens)) < 3:
            return "Tell me which TV show you mean."
        probe = Request(
            parsed.raw, parsed.tokens, parsed.year, None, None, "show",
            parsed.imdb_id, False, None, None,
        )
        with self._lock:
            self._ensure_library()
            owned_seasons = {
                copy.season
                for copy in self.library.find(probe)
                if copy.season
            }
        owned = queued = pending = blocked = 0
        lines = []
        misses = 0
        last_hit = max(owned_seasons) if owned_seasons else 0
        stopped_after = None
        guest_capped = False
        for season in range(1, MAX_SHOW_SEASONS + 1):
            result = self.request(
                f"{shown} S{season:02d}", kind="show", brief=True, as_guest=as_guest,
            )
            label = f"S{season:02d}"
            if _season_absent(result):
                misses += 1
                if season <= last_hit:
                    blocked += 1
                    lines.append(f"{label}: {result}")
                if misses >= 2 and season > last_hit:
                    stopped_after = last_hit
                    break
                continue
            misses = 0
            last_hit = max(last_hit, season)
            if "Already on Plex" in result:
                owned += 1
            elif "Already in Deluge" in result:
                pending += 1
            elif result.startswith("Queued"):
                queued += 1
            else:
                blocked += 1
            lines.append(f"{label}: {result}")
            # Stop probing further seasons once the guest active cap is hit.
            if as_guest and "guest download limit" in result.lower():
                guest_capped = True
                break
        if not lines:
            return (
                f'I couldn\'t find seasons of "{shown}". '
                "Send a specific season like "
                f'"{shown} S01", or an IMDB link.'
            )
        head = _collection_head(len(lines), owned, queued, blocked, pending)
        intro = f'Found {len(lines)} season(s) for "{shown}":'
        text = intro + "\n" + "\n".join(lines) + "\n\n" + head
        if stopped_after:
            text += (
                f"\nNo releases found after S{stopped_after:02d} "
                "(checked the next seasons in the index)."
            )
        if guest_capped:
            text += "\nStopped at the guest download limit."
        return text

    def listing(self, which: str) -> str:
        torrents = [item for item in self.deluge.list_torrents() if bucket(item) == which]
        titles = {"active": "In progress", "queued": "Queued", "done": "Finished"}
        if not torrents:
            return self._noted(f"{titles[which]}: nothing.")
        lines = [torrent_line(item) for item in torrents[:30]]
        extra = len(torrents) - len(lines)
        text = f"{titles[which]} ({len(torrents)}):\n" + "\n".join(lines)
        if extra > 0:
            text += f"\n…and {extra} more"
        return self._noted(text)

    def counts(self) -> str:
        torrents = self.deluge.list_torrents()
        totals = {"active": 0, "queued": 0, "done": 0}
        for item in torrents:
            totals[bucket(item)] += 1
        return self._noted(
            f"In progress: {totals['active']}\n"
            f"Queued: {totals['queued']}\n"
            f"Finished: {totals['done']}"
        )

    def boost_stuck(
        self,
        user_id: int | None = None,
        *,
        only_when_added: bool = False,
        min_age_seconds: int | None = None,
    ) -> str:
        """Add one alternate magnet per stuck title group.

        Same-title magnets are grouped (movie+year or show+season/episode). A
        title only gets another magnet when every incomplete member is stuck,
        and only up to ``MAX_MAGNETS_PER_TITLE``. Owner-only when ``user_id``
        is provided; import-cycle auto-boost omits it.

        Manual /boost (``user_id`` set) uses no age wait — 0% and idle is
        enough. Auto-boost keeps the multi-hour gate unless overridden.
        """
        if (
            user_id is not None
            and self.access is not None
            and not self.access.is_owner(user_id)
        ):
            return "Only the owner can do that."
        if min_age_seconds is None:
            min_age_seconds = (
                STUCK_MIN_AGE_SECONDS
                if only_when_added or user_id is None
                else MANUAL_STUCK_MIN_AGE_SECONDS
            )
        # CLI passes owner user_id; treat that like manual (no age wait).
        if user_id is not None and not only_when_added:
            min_age_seconds = MANUAL_STUCK_MIN_AGE_SECONDS
        with self._lock:
            torrents = self.deluge.list_torrents()
        if self.ledger is None:
            return self._noted("Import ledger is not set up.")
        grouped = group_torrents_by_title(self.ledger, torrents)
        candidates = []
        for key, members in grouped.items():
            if not title_key_is_precise(key):
                continue
            incomplete = [
                item for item in members
                if not is_complete(item.state, item.progress)
            ]
            if not incomplete:
                continue
            if not all(
                is_stuck(item, min_age_seconds=min_age_seconds)
                for item in incomplete
            ):
                continue
            candidates.append((key, incomplete))
        if not candidates:
            if only_when_added:
                return ""
            if min_age_seconds <= 0:
                return self._noted(
                    "No stuck title groups (need incomplete magnets at 0% "
                    "with no download for that title)."
                )
            hours = max(1, int(min_age_seconds) // 3600)
            return self._noted(
                "No stuck title groups (every incomplete magnet for a title "
                f"must be at 0% for {hours}+ hours with no download)."
            )
        known = {item.hash.lower() for item in torrents}
        added: list[str] = []
        skipped: list[str] = []
        for _key, members in sorted(candidates, key=lambda row: row[0]):
            line = self._boost_title_group(
                members, known, min_age_seconds=min_age_seconds,
            )
            if line.startswith("Added"):
                added.append(line)
            else:
                skipped.append(line)
        if only_when_added and not added:
            return ""
        bits = []
        if added:
            bits.append(f"Boosted {len(added)}:")
            bits.extend(added)
        if skipped and not only_when_added:
            bits.append(f"Skipped {len(skipped)}:")
            bits.extend(skipped)
        if not bits:
            return self._noted("Nothing to boost.")
        return self._noted("\n".join(bits))

    def clear_boost_siblings(self, finished_hash: str) -> list[str]:
        """Remove incomplete same-title / boost-group peers after import."""
        if self.ledger is None:
            return []
        with self._lock:
            return clear_boost_siblings(self.ledger, self.deluge, finished_hash)

    def _boost_title_group(
        self,
        members: list[Torrent],
        known_hashes: set[str],
        *,
        min_age_seconds: int = STUCK_MIN_AGE_SECONDS,
    ) -> str:
        primary = min(members, key=lambda item: (item.time_added or 0, item.hash))
        short = primary.short_id
        name = _pretty_name(primary.name)
        if self.ledger is None:
            return f"{short}  {name}: import ledger is not set up"
        label = self.ledger.label(primary.hash)
        for item in members:
            if label is None:
                label = self.ledger.label(item.hash)
        parsed, display = request_from_label_or_name(label, primary.name)
        if not parsed.tokens:
            return f"{short}  {name}: could not parse a title to search"
        try:
            hits = self._search_hits(parsed, display)
        except Exception as exc:
            return f"{short}  {name}: search failed ({public_error(exc)})"
        group_key = title_group_key(
            kind=parsed.kind,
            title=display,
            year=parsed.year,
            season=parsed.season,
            episode=parsed.episode,
        )
        # Cap + all-stuck re-check + add must be atomic across /boost and auto-boost.
        with self._lock:
            live_rows = {
                item.hash.lower(): item for item in self.deluge.list_torrents()
            }
            known_hashes |= set(live_rows)
            live_group = []
            for item in live_rows.values():
                if is_complete(item.state, item.progress):
                    continue
                item_key = None
                state = self.ledger.state(item.hash) or {}
                item_key = str(state.get("title_key") or "").strip() or None
                if not item_key:
                    item_key = title_group_key_from_label(self.ledger.label(item.hash))
                if not item_key:
                    item_parsed, item_display = request_from_label_or_name(
                        None, item.name,
                    )
                    item_key = title_group_key(
                        kind=item_parsed.kind,
                        title=item_display,
                        year=item_parsed.year,
                        season=item_parsed.season,
                        episode=item_parsed.episode,
                    )
                if group_key and item_key == group_key:
                    live_group.append(item)
            if not live_group:
                return f"{short}  {name}: no longer in Deluge"
            if not all(
                is_stuck(item, min_age_seconds=min_age_seconds)
                for item in live_group
            ):
                return f"{short}  {name}: a copy for this title is progressing"
            # Count Deluge + ledger-linked peers so a magnet not yet visible in
            # list_torrents still counts toward the per-title cap.
            ledger_active = {
                digest
                for digest, row in self.ledger.states().items()
                if str(row.get("title_key") or "") == (group_key or "")
                and (
                    digest in live_rows
                    or (
                        row.get("boost_group")
                        and row.get("status") not in {"imported", "copied"}
                        and not row.get("boost_cleared")
                    )
                )
            }
            effective = {item.hash.lower() for item in live_group} | ledger_active
            if len(effective) >= MAX_MAGNETS_PER_TITLE:
                return (
                    f"{short}  {name}: already at {MAX_MAGNETS_PER_TITLE} "
                    "magnets for this title"
                )
            alternate = pick_alternate_hit(
                hits, min_height=self.min_height, exclude_hashes=known_hashes,
            )
            if alternate is None:
                return (
                    f"{short}  {name}: no other "
                    f"{self.min_height}p+ release found"
                )
            state = self.ledger.state(primary.hash) or {}
            for item in live_group:
                row = self.ledger.state(item.hash) or {}
                if row.get("replace_path"):
                    state = row
                    primary = item
                    break
            replacing = None
            replace_path = state.get("replace_path")
            replace_height = state.get("replace_height")
            if replace_path:
                replacing = Copy(
                    "movie" if parsed.kind == "movie" else "episode",
                    display,
                    parsed.year,
                    display if parsed.kind == "show" else None,
                    parsed.season,
                    parsed.episode,
                    replace_height if isinstance(replace_height, int) else None,
                    str(replace_path),
                )
            self.deluge.add_magnet(magnet_uri(alternate))
            self._remember(
                alternate.info_hash,
                parsed,
                display,
                replacing,
                episode=parsed.episode,
            )
            peer_hashes = [item.hash for item in live_group]
            peer_hashes.append(alternate.info_hash)
            # Prefer an original (no boost_of) as primary when linking.
            link_primary = primary.hash
            for item in live_group:
                row = self.ledger.state(item.hash) or {}
                if not row.get("boost_of"):
                    link_primary = item.hash
                    break
            others = [digest for digest in peer_hashes if digest.lower() != link_primary.lower()]
            self.ledger.link_boost_members(link_primary, others)
            if group_key:
                for digest in peer_hashes:
                    self.ledger.mark(digest, title_key=group_key)
            known_hashes.add(alternate.info_hash.lower())
        shown = display or name
        return (
            f"Added {alternate.info_hash[:6]} for {shown} "
            f"({len(live_group)} stuck, {alternate.seeders} seeders) — "
            f"{_pretty_name(alternate.name)}"
        )

    def recent(self, window: str = "day", *, reveal_paths: bool = False) -> str:
        """List imports transferred into library folders within the time window.

        Guests see titles only; absolute library paths stay owner-only.
        """
        key = (window or "day").strip().lower()
        chosen = RECENT_WINDOWS.get(key)
        if chosen is None:
            return (
                "Usage: /recent, /recent week, or /recent month "
                "(default is the past 24 hours)."
            )
        span, label = chosen
        if self.ledger is None:
            return "Import history is not set up."
        cutoff = datetime.now(timezone.utc) - span
        rows = []
        for digest, state in self.ledger.states().items():
            if state.get("status") not in {"imported", "copied"}:
                continue
            dest = str(state.get("dest") or "").strip()
            if not dest:
                continue
            when = _import_time(state, dest)
            if when is None or when < cutoff:
                continue
            title = _recent_title(self.ledger.label(digest), digest)
            rows.append((when, digest[:6], title, dest))
        rows.sort(key=lambda row: row[0], reverse=True)
        if not rows:
            return self._noted(f"Nothing transferred in the past {label}.")
        lines = [f"Transferred in the past {label} ({len(rows)}):"]
        for when, short, title, dest in rows[:40]:
            stamp = when.astimezone().strftime("%b %d %I:%M %p")
            if reveal_paths:
                lines.append(f"{stamp}  {short}  {title}\n  → {dest}")
            else:
                lines.append(f"{stamp}  {short}  {title}")
        extra = len(rows) - min(len(rows), 40)
        if extra > 0:
            lines.append(f"…and {extra} more")
        return self._noted("\n".join(lines))

    def remove(self, needle: str, delete_files: bool, user_id: int = 0) -> str:
        if self.access is None or not self.access.is_owner(user_id):
            return "Only the owner can do that."
        return self._noted(self.deluge.remove(needle, delete_files))

    _REVIEW_STATUSES = {
        "pending", "awaiting_import", "awaiting_plex", "needs_video",
    }

    def upgrades(self, user_id: int = 0) -> str:
        if self.access is None or not self.access.is_owner(user_id):
            return "Only the owner can do that."
        if self.ledger is None:
            return "Upgrade review is not set up."
        with self._lock:
            ready = []
            stuck = []
            for digest, state in list(self.ledger.states().items()):
                status = state.get("upgrade_status")
                if status not in self._REVIEW_STATUSES:
                    continue
                replace_path = state.get("replace_path")
                if replace_path and not Path(replace_path).is_file():
                    self.ledger.mark(digest, upgrade_status="gone")
                    continue
                label = self.ledger.label(digest)
                title = label.title if label else digest[:6]
                if label and label.year:
                    title = f"{title} ({label.year})"
                old_height = state.get("replace_height") or "?"
                if state.get("new_height"):
                    new_label = f"{state['new_height']}p"
                elif status == "pending":
                    new_label = f"{self.min_height}p"
                else:
                    new_label = "?"
                old_name = Path(str(replace_path or "")).name or "(missing)"
                short = digest[:6]
                line = (
                    f"id {short}\n"
                    f"  {title}\n"
                    f"  old {old_height}p → new {new_label}\n"
                    f"  file: {old_name}"
                )
                if status == "pending":
                    ready.append(line)
                else:
                    stuck.append(f"{line}\n  status: {status}")
            self.ledger.flush()
        if not ready and not stuck:
            return "No upgrades waiting for review."
        parts = []
        if ready:
            parts.append(
                "Ready to retire old lower-quality files.\n"
                "The id is the 6-character code after \"id\".\n"
                "Example: /drop 58e38a   or   /keep 58e38a\n"
                "/drop moves the old file to the Recycle Bin (Windows) "
                "or import.trash_dir elsewhere — not a hard delete.\n"
                "/keep leaves the old file and clears it from this list.\n\n"
                + "\n\n".join(ready)
            )
        if stuck:
            parts.append(
                "Waiting — not ready to /drop yet.\n"
                "Plex has not confirmed the new file on the same library item "
                "(still scanning, or new files landed in a different folder).\n"
                "Run /import after Plex finishes analyzing, or /keep <id>.\n\n"
                + "\n\n".join(stuck)
            )
        return "\n\n".join(parts)

    def drop_upgrade(self, needle: str, user_id: int = 0) -> str:
        if self.access is None or not self.access.is_owner(user_id):
            return "Only the owner can do that."
        if self.ledger is None:
            return "Upgrade review is not set up."
        with self._lock:
            digest, state, error = self._find_upgrade(needle, {"pending"})
            if error or digest is None or state is None:
                return error or "Nothing matched that upgrade id."
            replace_path = state.get("replace_path")
            if not replace_path:
                return "That upgrade has no old file path recorded."
            if not Path(replace_path).is_file():
                self._clear_replace_siblings(digest, replace_path, "gone")
                self.ledger.flush()
                return "That old library file is already gone. Cleared it from the list."
            label = self.ledger.label(digest)
            try:
                dest = move_to_trash(
                    Path(replace_path),
                    self.trash_dir,
                    self.roots,
                    title=label.title if label else "",
                    year=label.year if label else None,
                )
            except OSError as exc:
                return f"Could not move the old file: {public_error(exc)}"
            self.ledger.mark(
                digest,
                upgrade_status="trashed",
                trash_path=str(dest),
            )
            self._clear_replace_siblings(digest, replace_path, "gone")
            self.ledger.flush()
            emit(
                "upgrade.drop",
                status="ok",
                torrent_id=digest[:8],
                user_id=user_id,
                recycle=dest == RECYCLE_BIN,
            )
        if dest == RECYCLE_BIN:
            return (
                "Moved old copy to the Windows Recycle Bin.\n"
                "Empty the Recycle Bin when you're sure."
            )
        return f"Moved old copy to trash:\n{dest}\nDelete it yourself when you're sure."

    def keep_upgrade(self, needle: str, user_id: int = 0) -> str:
        if self.access is None or not self.access.is_owner(user_id):
            return "Only the owner can do that."
        if self.ledger is None:
            return "Upgrade review is not set up."
        with self._lock:
            digest, state, error = self._find_upgrade(needle, self._REVIEW_STATUSES)
            if error or digest is None or state is None:
                return error or "Nothing matched that upgrade id."
            self.ledger.mark(digest, upgrade_status="kept")
            self.ledger.flush()
            old = Path(str(state.get("replace_path") or "")).name or "old copy"
        return f"Kept {old}. Removed from the upgrade list."

    def _find_upgrade(
        self, needle: str, statuses: set[str],
    ) -> tuple[str | None, dict | None, str | None]:
        text = needle.strip().lower()
        if not text:
            return None, None, "Usage needs an upgrade id."
        matches = []
        for digest, state in self.ledger.states().items():
            if state.get("upgrade_status") not in statuses:
                continue
            if digest.startswith(text) or digest[:6] == text:
                matches.append((digest, state))
        if not matches:
            return None, None, "No upgrade review matches that id."
        if len(matches) > 1:
            return None, None, "That id matches more than one upgrade. Use more characters."
        return matches[0][0], matches[0][1], None

    def _clear_replace_siblings(self, kept: str, replace_path: str, status: str) -> None:
        old_key = path_key(replace_path)
        for digest, state in self.ledger.states().items():
            if digest == kept:
                continue
            if state.get("upgrade_status") not in {
                "pending", "awaiting_import", "awaiting_plex", "needs_video",
            }:
                continue
            if path_key(str(state.get("replace_path") or "")) == old_key:
                self.ledger.mark(digest, upgrade_status=status)

    def _noted(self, text: str) -> str:
        note = getattr(self.deluge, "boot_note", "")
        if note:
            self.deluge.boot_note = ""
            return f"{note}\n{text}"
        return text

    def scan(self) -> str:
        self.library.refresh()
        return "Library reread.\n" + "\n".join(self.library.notes)

    def refresh_plex(
        self,
        user_id: int = 0,
        progress=None,
        *,
        poll_seconds: int = 20,
        max_wait_seconds: int = 5400,
        min_wait_seconds: int = 600,
        idle_polls: int = 9,
        sleep=None,
    ) -> str:
        """Ask Plex to fully refresh every section, wait with status updates, then reread.

        ``progress`` is an optional ``callable(str)`` for interim Telegram/CLI updates.
        Waits at least ``min_wait_seconds`` (default 10m) and ``idle_polls`` quiet
        polls (default ~3m at 20s) after scan activity so multi-section refreshes
        are not declared done during gaps between libraries.
        """
        if self.access is None or not self.access.is_owner(user_id):
            return "Only the owner can do that."
        if self.plex is None:
            return "Plex scan is not configured."
        import time as _time

        sleeper = sleep or _time.sleep

        def report(message: str) -> None:
            if progress is not None and message:
                progress(message)

        from plex_queue.otel_setup import span

        started = timed()
        with span("plex.refresh_all", user_id=user_id):
            report("Stopping any in-progress scan and starting a full Plex refresh…")
            notes, section_ids = self.plex.refresh_all()
            kickoff = "\n".join(notes) if notes else "Refresh requested."
            report(kickoff)
            if section_ids:
                report(
                    f"Watching Plex scan progress (about every {max(5, int(poll_seconds))}s; "
                    f"won't finish before {max(0, int(min_wait_seconds)) // 60}m)…"
                )
                self._wait_for_plex_scan(
                    report,
                    poll_seconds=poll_seconds,
                    max_wait_seconds=max_wait_seconds,
                    min_wait_seconds=min_wait_seconds,
                    idle_polls=idle_polls,
                    sleep=sleeper,
                )
            report("Rereading local Plex DB and drives…")
            self.library.refresh()
            notes.extend(self.library.notes)
            emit(
                "plex.refresh_all",
                status="ok" if section_ids else "error",
                user_id=user_id,
                sections=len(section_ids),
                duration_ms=duration_ms(started),
            )
            summary = ["Plex refresh request complete; local index reread."]
            summary.extend(self.library.notes)
            if not section_ids and notes:
                return "\n".join(notes)
            return "\n".join(summary)

    def _wait_for_plex_scan(
        self,
        report,
        *,
        poll_seconds: int,
        max_wait_seconds: int,
        min_wait_seconds: int,
        idle_polls: int,
        sleep,
    ) -> None:
        from plex_queue.plexscan import format_scan_activities, is_scan_activity

        interval = max(5, int(poll_seconds))
        limit = max(interval, int(max_wait_seconds))
        quiet_needed = max(3, int(idle_polls))
        floor = max(0, int(min_wait_seconds))
        seen_scan = False
        idle_streak = 0
        waited = 0
        last_fingerprint = ""
        while waited < limit:
            sleep(interval)
            waited += interval
            try:
                activities = self.plex.list_activities()
            except Exception:
                activities = None
            if activities is None:
                # Do not treat API failures as "scan finished".
                report("Could not read Plex activity status; still waiting…")
                continue
            scans = [item for item in activities if is_scan_activity(item)]
            if not scans:
                if seen_scan:
                    # Plex often gaps between sections; require quiet + min runtime.
                    idle_streak += 1
                    if idle_streak >= quiet_needed and waited >= floor:
                        report("Plex library scan looks finished.")
                        return
                    if idle_streak >= quiet_needed:
                        minutes, seconds = divmod(max(0, floor - waited), 60)
                        report(
                            "Scan is quiet; holding until the minimum wait elapses "
                            f"({minutes}m {seconds}s left)…"
                        )
                    else:
                        report(
                            "No active library scan right now; waiting in case another "
                            "section starts…"
                        )
                    continue
                if waited >= max(floor, 60, interval * 2):
                    report(
                        "No scan activity reported yet after the minimum wait; "
                        "continuing with a local reread (check Plex Activity)."
                    )
                    return
                report("Waiting for Plex to start scanning…")
                continue
            seen_scan = True
            idle_streak = 0
            fingerprint = "|".join(
                f"{item.title}:{item.subtitle}:{item.progress}" for item in scans[:6]
            )
            if fingerprint == last_fingerprint and waited % (interval * 2) != 0:
                continue
            last_fingerprint = fingerprint
            minutes, seconds = divmod(waited, 60)
            header = f"Plex scan still running ({minutes}m {seconds}s):"
            body = format_scan_activities(activities)
            report(f"{header}\n{body}" if body else header)
        report(
            f"Still scanning after {limit // 60} minutes; rereading the local index now. "
            "Check Plex Activity for the rest."
        )

    def tidy(self, user_id: int = 0, refresh: bool = True) -> str:
        if self.access is None or not self.access.is_owner(user_id):
            return "Only the owner can do that."
        if self.tidy_ledger is None:
            return "Tidy review is not set up."
        from plex_queue.otel_setup import span

        with self._lock:
            notes: list[str] = []
            if refresh:
                started = timed()
                with span("tidy.audit", user_id=user_id):
                    self.library.refresh()
                    notes.extend(self.library.notes)
                    actions, space = self._audit_actions()
                    self.tidy_ledger.replace_pending(
                        [action.as_row() for action in actions]
                    )
                    notes.extend(space)
                    counts: dict[str, int] = {}
                    for action in actions:
                        if action.status != "pending":
                            continue
                        counts[action.kind] = counts.get(action.kind, 0) + 1
                    emit(
                        "tidy.audit",
                        status="ok",
                        user_id=user_id,
                        pending=sum(counts.values()),
                        counts=counts,
                        duration_ms=duration_ms(started),
                    )
            rows = self.tidy_ledger.list(statuses={"pending", "blocked"})
            return format_report(
                rows,
                notes=notes if refresh else None,
                audited_at=self.tidy_ledger.audited_at(),
            )

    def tidy_apply(self, needle: str, user_id: int = 0) -> str:
        if self.access is None or not self.access.is_owner(user_id):
            return "Only the owner can do that."
        if self.tidy_ledger is None:
            return "Tidy review is not set up."
        text = (needle or "").strip()
        if not text:
            return (
                "Usage: /tidy apply <id>\n"
                "/tidy apply dupes\n"
                "/tidy orphans\n"
                "/tidy apply renames 50\n"
                "/tidy apply moves 1-20\n"
                "Selectors: 50 | 1-100 | 1:50 | 1,5,987 | s01 | s01:05 | all"
            )
        with self._lock:
            kind, selector = parse_batch(text)
            if kind is not None:
                return self._tidy_apply_many({kind}, user_id, selector=selector)
            action_id, row, error = self.tidy_ledger.find(
                text.lower(), {"pending", "blocked"},
            )
            if error or action_id is None or row is None:
                return error or "Nothing matched that tidy id."
            if row.get("status") == "blocked" or row.get("kind") == "report":
                return (
                    f"id {action_id} is report-only.\n"
                    f"{row.get('detail') or 'No automatic fix.'}"
                )
            from plex_queue.otel_setup import span

            started = timed()
            with span(
                "tidy.apply",
                action_id=action_id,
                kind=str(row.get("kind") or ""),
                user_id=user_id,
            ):
                try:
                    message = apply_action(
                        row,
                        roots=self.roots,
                        trash_dir=self.trash_dir,
                        plex=self.plex,
                        library=self.library,
                        free_of=lambda path: int(shutil.disk_usage(path).free),
                        margin=self.import_margin,
                        upgrade_paths=self._upgrade_paths(),
                    )
                except (OSError, sqlite3.Error) as exc:
                    self.tidy_ledger.mark(
                        action_id, status="failed", error=public_error(exc),
                    )
                    emit(
                        "tidy.apply",
                        status="failed",
                        action_id=action_id,
                        kind=row.get("kind"),
                        user_id=user_id,
                        error=public_error(exc),
                        duration_ms=duration_ms(started),
                    )
                    return f"Could not apply tidy {action_id}: {public_error(exc)}"
                self.tidy_ledger.mark(action_id, status="done")
                self.tidy_ledger.cancel_related(row.get("source"), except_id=action_id)
                emit(
                    "tidy.apply",
                    status="ok",
                    action_id=action_id,
                    kind=row.get("kind"),
                    user_id=user_id,
                    confidence=row.get("confidence") or "plex",
                    duration_ms=duration_ms(started),
                )
                return f"Applied {action_id}.\n{message}"

    def tidy_skip(self, needle: str, user_id: int = 0) -> str:
        if self.access is None or not self.access.is_owner(user_id):
            return "Only the owner can do that."
        if self.tidy_ledger is None:
            return "Tidy review is not set up."
        text = (needle or "").strip()
        if not text:
            return (
                "Usage: /tidy skip <id>\n"
                "/tidy skip renames all\n"
                "/tidy skip moves 1-50"
            )
        with self._lock:
            kind, selector = parse_batch(text)
            if kind is not None:
                return self._tidy_skip_many({kind}, user_id, selector=selector)
            action_id, row, error = self.tidy_ledger.find(
                text.lower(), {"pending", "blocked", "failed"},
            )
            if error or action_id is None or row is None:
                return error or "Nothing matched that tidy id."
            self.tidy_ledger.mark(action_id, status="skipped")
            title = row.get("title") or action_id
            emit(
                "tidy.skip",
                status="ok",
                action_id=action_id,
                kind=row.get("kind"),
                user_id=user_id,
            )
            return f"Skipped {action_id} ({title})."

    def _tidy_apply_many(
        self, kinds: set[str], user_id: int, *, selector: str = "",
    ) -> str:
        rows = self.tidy_ledger.pending(kinds)
        # Bulk dupes only apply Plex-linked matches; weak title hits are id-only.
        if kinds == {"trash"}:
            rows = [
                row for row in rows
                if str(row.get("confidence") or "plex") == "plex"
            ]
        # Large move/rename batches require an explicit selector (count/range/all).
        if kinds & {"move", "rename"} and not (selector or "").strip():
            return (
                f"Pending {next(iter(kinds))} actions: {len(rows)}.\n"
                "Pass a selector so this is intentional, e.g.\n"
                f"/tidy apply {next(iter(kinds))}s 50\n"
                f"/tidy apply {next(iter(kinds))}s 1-100\n"
                f"/tidy apply {next(iter(kinds))}s all"
            )
        chosen, error = select_rows(rows, selector)
        if error:
            return error
        if not chosen:
            return f"No pending {'/'.join(sorted(kinds))} actions."
        started = timed()
        ok = 0
        failed = 0
        failures: list[str] = []
        upgrade_paths = self._upgrade_paths()
        interrupted = False
        try:
            for index, row in enumerate(chosen, start=1):
                action_id = str(row.get("id") or "")
                try:
                    apply_action(
                        row,
                        roots=self.roots,
                        trash_dir=self.trash_dir,
                        plex=self.plex,
                        library=self.library,
                        free_of=lambda path: int(shutil.disk_usage(path).free),
                        margin=self.import_margin,
                        upgrade_paths=upgrade_paths,
                    )
                except KeyboardInterrupt:
                    interrupted = True
                    break
                except (OSError, sqlite3.Error) as exc:
                    self.tidy_ledger.mark(action_id, status="failed", error=public_error(exc))
                    failed += 1
                    if len(failures) < 15:
                        failures.append(f"{action_id}: failed — {public_error(exc)}")
                    continue
                self.tidy_ledger.mark(action_id, status="done")
                self.tidy_ledger.cancel_related(row.get("source"), except_id=action_id)
                ok += 1
                if index % 25 == 0 or index == len(chosen):
                    log.info(
                        "tidy apply progress: %s/%s ok=%s failed=%s",
                        index, len(chosen), ok, failed,
                    )
        finally:
            if self.library is not None and kinds & {"move", "rename", "trash"}:
                # One stale-mark after the batch — not after every file.
                self.library.invalidate()
        emit(
            "tidy.apply_batch",
            status=(
                "interrupted" if interrupted
                else ("ok" if failed == 0 else "partial")
            ),
            kinds=sorted(kinds),
            selector=selector or "all",
            requested=len(chosen),
            ok=ok,
            failed=failed,
            user_id=user_id,
            duration_ms=duration_ms(started),
        )
        if interrupted:
            lines = [
                f"Interrupted (Ctrl+C) after {ok} applied, {failed} failed "
                f"(of {len(chosen)} queued). Re-run to continue."
            ]
        else:
            lines = [f"Applied {ok} action(s), {failed} failed (of {len(chosen)})."]
        if failures:
            lines.append("")
            lines.extend(failures)
            leftover = failed - len(failures)
            if leftover > 0:
                lines.append(f"…and {leftover} more failures")
        return "\n".join(lines)

    def _tidy_skip_many(
        self, kinds: set[str], user_id: int, *, selector: str = "",
    ) -> str:
        rows = self.tidy_ledger.pending(kinds)
        if kinds & {"move", "rename"} and not (selector or "").strip():
            return (
                f"Pending {next(iter(kinds))} actions: {len(rows)}.\n"
                "Pass a selector, e.g. /tidy skip renames all"
            )
        chosen, error = select_rows(rows, selector)
        if error:
            return error
        if not chosen:
            return f"No pending {'/'.join(sorted(kinds))} actions to skip."
        for row in chosen:
            self.tidy_ledger.mark(str(row.get("id") or ""), status="skipped")
        emit(
            "tidy.skip_batch",
            status="ok",
            kinds=sorted(kinds),
            selector=selector or "all",
            count=len(chosen),
            user_id=user_id,
        )
        return f"Skipped {len(chosen)} {'/'.join(sorted(kinds))} action(s)."

    def _upgrade_paths(self) -> set[str]:
        if self.ledger is None:
            return set()
        return upgrade_replace_paths(self.ledger.states())

    def _audit_actions(self):
        prior_moves: list[dict] = []
        if self.tidy_ledger is not None:
            prior_moves = self.tidy_ledger.list(
                statuses={"done"}, kinds={"move"},
            )
        return audit(
            list(self.library.copies),
            plex_db=self.plex_db,
            buckets=self.tidy_buckets,
            roots=self.roots,
            upgrade_paths=self._upgrade_paths(),
            free_of=lambda path: int(shutil.disk_usage(path).free),
            margin=self.import_margin,
            prior_moves=prior_moves,
        )

    def _ensure_library(self) -> None:
        self.library.ensure_fresh()

    def _remember(
        self,
        info_hash: str,
        parsed: Request,
        display: str,
        replacing=None,
        *,
        episode: int | None = None,
    ) -> None:
        if self.ledger is None or parsed.kind not in {"movie", "show"}:
            return
        title = display or " ".join(parsed.tokens)
        replace_path = None
        replace_height = None
        if replacing is not None and getattr(replacing, "path", None):
            candidate = Path(replacing.path)
            roots = self.roots or tuple(getattr(self.library, "roots", ()) or ())
            if roots and under_roots(candidate, roots):
                replace_path = str(candidate)
                replace_height = replacing.height
        ep = parsed.episode if episode is None else episode
        self.ledger.remember(
            info_hash,
            Label(parsed.kind, title, parsed.year, parsed.season, ep),
            replace_path=replace_path,
            replace_height=replace_height,
        )


def _clarify(query: str) -> Clarify:
    shown = " ".join(query.split())
    if len(shown) > 80:
        shown = shown[:77] + "..."
    return Clarify(
        query.strip(),
        f'Is "{shown}" a movie or a TV show?\n'
        "An IMDB link is the surest way. Paste one, such as "
        "https://www.imdb.com/title/tt0133093/",
    )


def _from_identity(raw: str, identity: Identity) -> Request:
    titled = identity.title if identity.year is None else f"{identity.title} {identity.year}"
    parsed = parse_request(titled)
    return Request(
        raw.strip(), parsed.tokens, identity.year or parsed.year,
        parsed.season, parsed.episode, identity.kind, identity.imdb_id,
    )


def _season_absent(result: str) -> bool:
    text = result.strip().casefold()
    return text.startswith("nothing") or "nothing matched" in text or "nothing found" in text


def _collection_head(
    total: int, owned: int, queued: int, blocked: int, pending: int = 0,
) -> str:
    bits = []
    if owned:
        bits.append(f"{owned} already covered")
    if pending:
        bits.append(f"{pending} already in Deluge")
    if queued:
        bits.append(f"{queued} queued")
    if blocked:
        bits.append(f"{blocked} not filled")
    if not bits:
        return f"You already have all {total}. Check Plex."
    if owned == total and not queued and not pending and not blocked:
        return f"You already have all {total}. Check Plex."
    if blocked and not queued and not pending:
        return (
            f"{owned} already covered, {blocked} not filled "
            "(no suitable release or unknown quality)."
        )
    return ", ".join(bits) + "."


def _import_time(state: dict, dest: str) -> datetime | None:
    raw = state.get("imported_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    try:
        mtime = Path(dest).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


def _recent_title(label: Label | None, digest: str) -> str:
    if label is None:
        return digest[:6]
    bits = [label.title]
    if label.year:
        bits[0] = f"{label.title} ({label.year})"
    if label.kind == "show" and label.season:
        if label.episode:
            bits.append(f"S{label.season:02d}E{label.episode:02d}")
        else:
            bits.append(f"S{label.season:02d}")
    return " ".join(bits)


def _search_text(parsed: Request, display: str = "") -> str:
    words = display.strip() or " ".join(parsed.tokens)
    if parsed.year and str(parsed.year) not in words:
        words = f"{words} {parsed.year}"
    if parsed.season and parsed.episode:
        words = f"{words} S{parsed.season:02d}E{parsed.episode:02d}"
    elif parsed.season:
        words = f"{words} S{parsed.season:02d}"
    return words


def _split_season_hits(hits: list, season: int | None) -> tuple[list, list]:
    """Separate season packs from single-episode releases for one season."""
    packs = []
    episodes = []
    for hit in hits:
        parsed = parse_request(hit.name)
        if season is not None and parsed.season != season:
            continue
        if parsed.episode is None:
            packs.append(hit)
        else:
            episodes.append(hit)
    return packs, episodes


def _need_show_season(title: str, copies: list, brief: bool = False) -> str:
    shown = " ".join(str(title).split()) or "that show"
    if brief:
        return f'Tell me a season of "{shown}", such as "{shown} S01"'
    if not copies:
        return (
            f'Tell me which season of "{shown}" — for example "{shown} S01" '
            f'or "{shown} S01E01".'
        )
    seasons: dict[int, list] = {}
    for copy in copies:
        if copy.season:
            seasons.setdefault(copy.season, []).append(copy)
    lines = _season_lines(seasons)
    return (
        f'Found "{shown}" on Plex with {len(seasons)} season(s):\n'
        + "\n".join(lines)
        + f'\nA show request needs a season or episode — try "{shown} S{min(seasons):02d}" '
        f'or "{shown} S{min(seasons):02d}E01".'
    )


def _season_lines(seasons: dict[int, list]) -> list[str]:
    lines = []
    for season in sorted(seasons):
        episodes = sorted({copy.episode for copy in seasons[season] if copy.episode})
        heights = [copy.height for copy in seasons[season] if copy.height]
        best = max(heights) if heights else None
        quality = f"{best}p" if best else "unknown quality"
        if episodes:
            span = f"E{min(episodes):02d}-E{max(episodes):02d}"
            lines.append(f"S{season:02d}: {len(episodes)} episode(s) {span}, best {quality}")
        else:
            lines.append(f"S{season:02d}: on disk, best {quality}")
    return lines


def _season_copy_label(copy, season: int | None) -> str:
    if season is None:
        return copy.label
    show = copy.show or copy.title
    return f"{show} S{season:02d}"


def _already(
    decision: AlreadyHave,
    brief: bool = False,
    season: int | None = None,
    *,
    reveal_paths: bool = True,
) -> str:
    shown = _season_copy_label(decision.copy, season)
    if brief:
        return f"Already on Plex at {decision.height}p — {shown}"
    if season is not None:
        lines = [
            f"Already on Plex: season {season} episodes look complete at {decision.height}p "
            "through the highest episode I found.",
            shown,
        ]
        if reveal_paths:
            lines.append(decision.copy.path)
        lines.append(
            "Check Plex. If the season continues past that, request the next episode."
        )
        return "\n".join(lines)
    extra = ""
    if decision.count > 1:
        extra = f"\n{decision.count} copies on disk. Best is {decision.height}p."
    lines = [
        f"Already on Plex at {decision.height}p.{extra}",
        decision.copy.label,
    ]
    if reveal_paths:
        lines.append(decision.copy.path)
    lines.append("Check Plex.")
    return "\n".join(lines)


def _already_in_deluge(
    torrents: list[Torrent],
    hit,
    min_height: int,
    *,
    season: int | None = None,
    episode: int | None = None,
) -> Torrent | None:
    """True when Deluge already has this release (same hash) or the same title+season."""
    hit_parsed = parse_request(hit.name)
    want_season = season if season is not None else hit_parsed.season
    want_episode = episode if episode is not None else hit_parsed.episode
    hit_tokens = tokenize(cleaned_title(hit.name) or hit.name)
    for torrent in torrents:
        if torrent.hash.lower() == hit.info_hash.lower():
            return torrent
        torrent_parsed = parse_request(torrent.name)
        if not _same_show_release(
            want_season, want_episode, torrent_parsed.season, torrent_parsed.episode,
        ):
            continue
        torrent_tokens = tokenize(cleaned_title(torrent.name) or torrent.name)
        if not phrase_match(hit_tokens, torrent_tokens):
            continue
        height = video_height(torrent.name)
        if height is None or height >= min_height or (video_height(hit.name) or 0) <= (height or 0):
            return torrent
    return None


def _same_show_release(
    want_season: int | None,
    want_episode: int | None,
    have_season: int | None,
    have_episode: int | None,
) -> bool:
    """Season/episode must agree so S02 does not match an S01 torrent by title alone."""
    if want_season is None and have_season is None:
        return True
    if want_season is None or have_season is None:
        return False
    if want_season != have_season:
        return False
    if want_episode is None:
        # Season-pack request: only match another pack, not a single episode.
        return have_episode is None
    if have_episode is None:
        # Episode request against a season pack already in Deluge.
        return True
    return want_episode == have_episode


def _pretty_name(name: str) -> str:
    return cleaned_title(name) or " ".join(str(name).replace(".", " ").split())


def _franchise_members(name: str, members: list[Identity]) -> list[Identity]:
    """Keep titles that belong to the franchise; drop unrelated IMDB noise."""
    words = tokenize(name)
    if not words:
        return list(members)
    needles = [words]
    if len(words) > 1:
        needles.append([words[-1]])
    kept = []
    seen = set()
    for member in members:
        title_words = tokenize(member.title)
        if not any(franchise_match(needle, title_words) or phrase_match(needle, title_words) for needle in needles):
            continue
        key = (member.imdb_id, member.title.casefold(), member.year)
        if key in seen:
            continue
        seen.add(key)
        kept.append(member)
    return kept
