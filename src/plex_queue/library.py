from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from plex_queue.models import Copy
from plex_queue.place import cleaned_title
from plex_queue.titles import (
    Request,
    franchise_match,
    parse_request,
    phrase_match,
    screen_height,
    tokenize,
    video_height,
)

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
SKIP_DIRS = {"$recycle.bin", "system volume information", ".trash"}


class Library:
    def __init__(self, plex_db: Path | None, roots: list[Path]):
        self.plex_db = plex_db
        self.roots = roots
        self.copies: list[Copy] = []
        self.notes: list[str] = []
        self.loaded = False
        self._lock = threading.RLock()

    def invalidate(self) -> None:
        """Mark the index stale without forcing an immediate full drive walk."""
        with self._lock:
            self.loaded = False
            self.copies = []

    def ensure_fresh(self, include_disk: bool = True) -> None:
        """Refresh under the library lock if the index was invalidated."""
        with self._lock:
            if not self.loaded:
                self._refresh_unlocked(include_disk)

    def refresh(self, include_disk: bool = True) -> None:
        with self._lock:
            self._refresh_unlocked(include_disk)

    def note_files(self, paths: list[Path] | tuple[Path, ...]) -> int:
        """Fold newly imported videos into the index without a full drive walk."""
        with self._lock:
            if not self.loaded:
                # Warm from Plex only; then append the new paths (no multi-drive walk).
                self._refresh_unlocked(include_disk=False)
            known = {_key(copy.path) for copy in self.copies}
            added = 0
            for path in paths:
                for video in _videos_from(path):
                    key = _key(str(video))
                    if key in known:
                        continue
                    stem = video.stem.lower()
                    if "trailer" in stem or stem.endswith("sample"):
                        continue
                    self.copies.append(copy_from_file(video))
                    known.add(key)
                    added += 1
            if added:
                log.info("library noted %s new file(s) without a full walk", added)
            return added

    def remember_move(self, source: Path, dest: Path) -> None:
        """Update the in-memory index after a tidy move without re-reading Plex.

        Batch ``tidy apply moves`` used to call note_files()+invalidate() per
        file, which reopened the Plex DB thousands of times and eventually
        crashed with ``database disk image is malformed``.
        """
        with self._lock:
            if not self.loaded:
                return
            src_key = _key(str(source))
            self.copies = [
                copy for copy in self.copies if _key(copy.path) != src_key
            ]
            known = {_key(copy.path) for copy in self.copies}
            for video in _videos_from(dest):
                key = _key(str(video))
                if key in known:
                    continue
                stem = video.stem.lower()
                if "trailer" in stem or stem.endswith("sample"):
                    continue
                self.copies.append(copy_from_file(video))
                known.add(key)

    def _refresh_unlocked(self, include_disk: bool = True) -> None:
        copies: list[Copy] = []
        notes: list[str] = []
        known_paths: set[str] = set()
        if self.plex_db and self.plex_db.exists():
            plex_copies = load_plex(self.plex_db)
            copies.extend(plex_copies)
            known_paths.update(_key(copy.path) for copy in plex_copies)
            notes.append(f"Plex index: {len(plex_copies)} files")
        elif self.plex_db:
            notes.append(f"Plex database not found at {self.plex_db}")
        else:
            notes.append("No Plex database configured")

        disk_count = 0
        if include_disk:
            for root in self.roots:
                if not root.is_dir():
                    notes.append(f"Drive not connected: {root}")
                    continue
                for path in _walk_videos(root):
                    if _key(str(path)) in known_paths:
                        continue
                    if "trailer" in path.stem.lower() or path.stem.lower().endswith("sample"):
                        continue
                    copies.append(copy_from_file(path))
                    disk_count += 1
            if disk_count:
                notes.append(f"Extra files on disk, not yet in Plex: {disk_count}")
        else:
            notes.append("Disk walk skipped; using the Plex index only.")
        self.copies = copies
        self.notes = notes
        self.loaded = True
        log.info("library ready: %s copies", len(copies))

    def find(self, request: Request) -> list[Copy]:
        with self._lock:
            if not self.loaded:
                self._refresh_unlocked(True)
            return self._find_unlocked(request)

    def _find_unlocked(self, request: Request) -> list[Copy]:
        if not request.tokens:
            return []
        found = []
        matcher = franchise_match if request.collection else phrase_match
        for copy in self.copies:
            if request.season or request.episode:
                if copy.season != request.season:
                    continue
                if request.episode and copy.episode != request.episode:
                    continue
            if request.kind == "movie" and copy.kind == "episode":
                continue
            if request.kind == "show" and copy.kind == "movie":
                continue
            if request.kind == "show" and copy.kind == "file" and not copy.season:
                continue
            if copy.tokens:
                tokens = copy.tokens
            else:
                raw = copy.show if copy.kind == "episode" else copy.title
                tokens = tuple(tokenize(cleaned_title(raw or "") or (raw or "")))
            if not matcher(request.tokens, list(tokens)):
                continue
            if (
                request.year
                and not request.collection
                and copy.kind != "episode"
                and copy.year
                and copy.year != request.year
            ):
                continue
            found.append(copy)
        return found


def make_copy(
    kind: str,
    title: str,
    year: int | None,
    show: str | None,
    season: int | None,
    episode: int | None,
    height: int | None,
    path: str,
) -> Copy:
    # Normalize release/folder names before tokenizing so "Movie.Name.2019.1080p"
    # matches queries like "Movie Name", not the raw filename.
    match_name = show if kind == "episode" and show else title
    normalized = cleaned_title(match_name or "") or (match_name or "")
    return Copy(
        kind, title, year, show, season, episode, height, path,
        tuple(tokenize(normalized)),
    )


def copy_from_file(path: Path) -> Copy:
    parsed = parse_request(path.stem)
    kind = "episode" if parsed.season else "file"
    # Prefer a cleaned title for matching; keep the stem only as a fallback label.
    nice = cleaned_title(path.stem) or path.stem
    show = nice if parsed.season else None
    return make_copy(
        kind, nice, parsed.year, show, parsed.season, parsed.episode,
        video_height(path.stem), str(path),
    )


def load_plex(path: Path) -> list[Copy]:
    uri = path.resolve().as_posix()
    try:
        connection = sqlite3.connect(f"file:{uri}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        raise OSError(f"Plex database could not be opened: {exc}") from exc
    try:
        try:
            return _load_plex_rows(connection.cursor())
        except sqlite3.DatabaseError as exc:
            raise OSError(
                "Plex database is unreadable (malformed or locked). "
                "Close Plex and retry, or repair/restore the DB."
            ) from exc
    finally:
        connection.close()


def _load_plex_rows(cursor) -> list[Copy]:
    copies = []
    for title, year, width, height, file_path in cursor.execute(
        """
        SELECT mi.title, mi.year, m.width, m.height, mp.file
        FROM metadata_items mi
        JOIN media_items m ON m.metadata_item_id = mi.id AND m.deleted_at IS NULL
        JOIN media_parts mp ON mp.media_item_id = m.id AND mp.deleted_at IS NULL
        WHERE mi.deleted_at IS NULL AND mi.metadata_type = 1 AND mp.file IS NOT NULL
        """
    ):
        copies.append(
            make_copy(
                "movie", title or "", year, None, None, None,
                screen_height(width, height), file_path,
            )
        )
    for title, show, year, season, episode, width, height, file_path in cursor.execute(
        """
        SELECT episode.title, show.title, show.year, season."index",
               episode."index", m.width, m.height, mp.file
        FROM metadata_items episode
        JOIN metadata_items season ON season.id = episode.parent_id
        JOIN metadata_items show ON show.id = season.parent_id
        JOIN media_items m ON m.metadata_item_id = episode.id AND m.deleted_at IS NULL
        JOIN media_parts mp ON mp.media_item_id = m.id AND mp.deleted_at IS NULL
        WHERE episode.deleted_at IS NULL
          AND episode.metadata_type = 4
          AND mp.file IS NOT NULL
        """
    ):
        copies.append(
            make_copy(
                "episode",
                title or "",
                year,
                show,
                season,
                episode,
                screen_height(width, height),
                file_path,
            )
        )
    return copies


def _videos_from(path: Path):
    if path.is_file():
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            yield path
        return
    if path.is_dir():
        yield from _walk_videos(path)


def _walk_videos(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name.lower() not in SKIP_DIRS]
        for filename in filenames:
            if Path(filename).suffix.lower() in VIDEO_EXTENSIONS:
                yield Path(dirpath) / filename


def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def fill_heights(copies: list[Copy], limit: int = 8) -> list[Copy]:
    """Read the video height for matched files whose names don't say it."""
    if not shutil.which("ffprobe"):
        return copies
    pending = [copy for copy in copies if not copy.height][:limit]
    if not pending:
        return copies
    heights: dict[str, int | None] = {}
    workers = min(4, len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for copy, height in zip(pending, pool.map(lambda item: probe_height(item.path), pending)):
            heights[copy.path] = height
    filled = []
    for copy in copies:
        if copy.height or copy.path not in heights:
            filled.append(copy)
            continue
        filled.append(replace(copy, height=heights[copy.path] or copy.height))
    return filled


def probe_height(path: str) -> int | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "csv=p=0", path,
            ],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return screen_height(int(parts[0]), int(parts[1]))
    if len(parts) == 1 and parts[0].isdigit():
        return screen_height(None, int(parts[0]))
    return None
