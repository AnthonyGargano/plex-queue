from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import sys
import time
from pathlib import Path

from plex_queue.place import confined, safe_name, under_roots
from plex_queue.titles import screen_height

RECYCLE_BIN = Path("(Recycle Bin)")

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}


def path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def plex_paths_known(
    plex_db: Path | None,
    paths: list[str] | tuple[str, ...],
) -> set[str]:
    """Normcased paths from the list that currently appear in Plex media_parts."""
    if plex_db is None or not plex_db.exists() or not paths:
        return set()
    wanted = {path_key(path) for path in paths}
    found = set()
    for parts in _plex_parts_by_item(plex_db).values():
        for file_path, _height in parts:
            if file_path in wanted:
                found.add(file_path)
    return found


def plex_share_item(
    plex_db: Path | None,
    left: str,
    right_paths: list[str] | tuple[str, ...],
) -> bool:
    """True when left and any right path appear on the same metadata item."""
    if plex_db is None or not plex_db.exists() or not left or not right_paths:
        return False
    left_key = path_key(left)
    right_keys = {path_key(path) for path in right_paths}
    for parts in _plex_parts_by_item(plex_db).values():
        keys = {path for path, _height in parts}
        if left_key in keys and keys & right_keys:
            return True
    return False


def plex_parts_by_item(plex_db: Path) -> dict[int, list[tuple[str, int | None]]]:
    """Map each Plex metadata item id to its (path_key, height) media parts."""
    uri = plex_db.resolve().as_posix()
    connection = sqlite3.connect(f"file:{uri}?mode=ro&immutable=1", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT mi.id, mp.file, m.width, m.height
            FROM media_parts mp
            JOIN media_items m ON m.id = mp.media_item_id AND m.deleted_at IS NULL
            JOIN metadata_items mi ON mi.id = m.metadata_item_id AND mi.deleted_at IS NULL
            WHERE mp.deleted_at IS NULL AND mp.file IS NOT NULL
            """
        )
        by_item: dict[int, list[tuple[str, int | None]]] = {}
        for metadata_id, file_path, width, height in rows:
            if not file_path:
                continue
            by_item.setdefault(int(metadata_id), []).append(
                (path_key(file_path), screen_height(width, height))
            )
        return by_item
    finally:
        connection.close()


# Older call sites and tests may still use the private name.
_plex_parts_by_item = plex_parts_by_item


def plex_new_height(
    plex_db: Path | None,
    new_paths: list[str] | tuple[str, ...],
    *,
    replace_path: str | None = None,
) -> int | None:
    """Best indexed height for a new path that shares the old file's Plex item."""
    if plex_db is None or not plex_db.exists() or not new_paths or not replace_path:
        return None
    wanted = {path_key(path) for path in new_paths}
    old_key = path_key(replace_path)
    by_item = _plex_parts_by_item(plex_db)
    old_items = {
        item_id
        for item_id, parts in by_item.items()
        if any(path == old_key for path, _height in parts)
    }
    best = None
    for item_id in old_items:
        for path, height in by_item.get(item_id) or []:
            if path in wanted and height and (best is None or height > best):
                best = height
    return best


def plex_new_is_best(
    plex_db: Path | None,
    new_paths: list[str] | tuple[str, ...],
    min_height: int,
    *,
    replace_path: str | None = None,
    replace_height: int | None = None,
) -> bool:
    """True when a new path shares the old file's Plex item, meets min_height, and beats the old copy."""
    if plex_db is None or not plex_db.exists() or not new_paths or not replace_path:
        return False
    wanted = {path_key(path) for path in new_paths}
    old_key = path_key(replace_path)
    by_item = _plex_parts_by_item(plex_db)
    old_items = {
        item_id
        for item_id, parts in by_item.items()
        if any(path == old_key for path, _height in parts)
    }
    if not old_items:
        return False
    floor = replace_height or 0
    for item_id in old_items:
        parts = by_item.get(item_id) or []
        old_heights = [height or 0 for path, height in parts if path == old_key]
        old_on_item = max(old_heights) if old_heights else floor
        target = max(floor, old_on_item)
        new_heights = [height for path, height in parts if path in wanted and height]
        if not new_heights:
            continue
        best_new = max(new_heights)
        if best_new < min_height or best_new <= target:
            continue
        # New file must meet min_height and strictly beat the old copy. A peer
        # 4K version on the same item is fine; same-height duplicates are not.
        return True
    return False


def video_locals(jobs) -> list[str]:
    """Main video path(s) from import jobs; skip samples/trailers.

    Season packs include every episode so upgrade confirmation can match the
    old library episode, not only the largest file in the folder.
    """
    candidates = []
    for job in jobs:
        path = Path(job.local)
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        stem = path.stem.lower()
        if "trailer" in stem or stem.endswith("sample") or ".sample" in stem:
            continue
        candidates.append((int(getattr(job, "size", 0) or 0), str(path)))
    if not candidates:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _size, path in candidates]


def trash_path(
    source: Path,
    trash_dir: Path,
    *,
    title: str = "",
    year: int | None = None,
) -> Path:
    trash_dir.mkdir(parents=True, exist_ok=True)
    label = safe_name(title) if title else safe_name(source.stem)
    if year:
        label = f"{label} ({year})"
    dest = trash_dir / f"{label} — {safe_name(source.name)}"
    if dest.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = trash_dir / f"{label} — {stamp} — {safe_name(source.name)}"
    if not confined(dest, trash_dir):
        raise OSError("Trash destination left the trash folder.")
    return dest


def move_to_trash(
    source: Path,
    trash_dir: Path | None,
    roots: list[Path] | tuple[Path, ...],
    *,
    title: str = "",
    year: int | None = None,
    recycle: bool | None = None,
) -> Path:
    """Move an old library file out of the way after a confirmed upgrade.

    On Windows, defaults to the Recycle Bin (``recycle=True``). Elsewhere, or
    when ``recycle=False``, requires ``trash_dir`` and moves into that folder.
    """
    _assert_safe_library_file(source, roots)
    use_recycle = sys.platform == "win32" if recycle is None else recycle
    if use_recycle:
        _assert_safe_library_file(source, roots)
        _send_to_recycle_bin(source)
        return RECYCLE_BIN
    if trash_dir is None:
        raise OSError("Set import.trash_dir before using /drop on this system.")
    dest = trash_path(source, trash_dir, title=title, year=year)
    _assert_safe_library_file(source, roots)
    shutil.move(str(source), str(dest))
    if not confined(dest, trash_dir):
        # Extremely defensive: if something rewrote the path, do not leave it.
        raise OSError("Trash move escaped the trash folder.")
    return dest


def _assert_safe_library_file(
    source: Path,
    roots: list[Path] | tuple[Path, ...],
) -> None:
    """Reject symlinks and paths outside roots immediately before a trash move.

    Uses lstat so a planted symlink is refused before under_roots() would
    follow it via resolve(). Call again right before send2trash/shutil.move.
    """
    try:
        info = source.lstat()
    except OSError as exc:
        raise OSError("Old library file is missing.") from exc
    if stat.S_ISLNK(info.st_mode):
        raise OSError("Refusing to move a symlink.")
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Refusing to move a non-regular library path.")
    if not under_roots(source, roots):
        raise OSError("Refusing to move a file outside the library roots.")


def _send_to_recycle_bin(path: Path) -> None:
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise OSError(
            "Send2Trash is not installed; cannot use the Recycle Bin."
        ) from exc
    # Resolve only after symlink rejection so we do not follow a planted link.
    send2trash(os.fspath(path))
    if path.exists():
        raise OSError("Recycle Bin move did not remove the library file.")
