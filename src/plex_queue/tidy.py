from __future__ import annotations

import hashlib
import logging
import re
import shutil
import sqlite3
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from plex_queue.models import Copy
from plex_queue.place import (
    Label,
    cleaned_title,
    confined,
    gb,
    movie_folder,
    safe_filename,
    safe_name,
    survey,
    under_roots,
)
from plex_queue.plexscan import load_sections, section_id
from plex_queue.titles import tokenize
from plex_queue.upgrades import (
    RECYCLE_BIN,
    move_to_trash,
    path_key,
    plex_parts_by_item,
)

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}

PRIORITY = {
    "trash": 1,
    "scan": 2,
    "move": 3,
    "rename": 4,
    "report": 5,
}

BUCKET_ORDER = (
    "kids_movies",
    "kids_shows",
    "anime_movies",
    "anime_shows",
    "christmas",
    "documentary",
    "standup",
    "workouts",
    "movies",
    "shows",
)

# Word-boundary checks so "Movies" is not treated as kids ("kid" ⊂ "movies").
_KIDS = re.compile(r"\bkids?\b|\bchildren\b", re.IGNORECASE)
_ANIME = re.compile(r"\banime\b", re.IGNORECASE)
_CHRISTMAS = re.compile(r"\bchristmas\b|\bholiday\b", re.IGNORECASE)
_DOC = re.compile(r"\bdocumentar\w*\b", re.IGNORECASE)
_STANDUP = re.compile(r"\bstand[\s-]?up\b|\bstandup\b", re.IGNORECASE)
_WORKOUT = re.compile(r"\bworkout\w*\b|\bfitness\b", re.IGNORECASE)
_PWNAGE = re.compile(r"\bpwnage\b", re.IGNORECASE)
_MOVIE = re.compile(r"\bmovies?\b", re.IGNORECASE)
_SHOW = re.compile(r"\bshows?\b", re.IGNORECASE)

GENRE_BUCKETS = {
    "anime": "anime",
    "animation": None,  # alone is not enough; kids handled separately
    "children": "kids",
    "family": "kids",
    "kids": "kids",
    "documentary": "documentary",
    "stand-up": "standup",
    "standup": "standup",
    "stand up": "standup",
    "comedy": None,
    "holiday": "christmas",
    "christmas": "christmas",
    "fitness": "workouts",
    "sport": None,
    "workout": "workouts",
}


@dataclass(frozen=True)
class Action:
    id: str
    kind: str
    priority: int
    title: str
    detail: str
    source: str | None = None
    dest: str | None = None
    keep: str | None = None
    status: str = "pending"
    bucket: str | None = None
    confidence: str = "plex"  # plex = same metadata item; weak = title heuristic

    def as_row(self) -> dict:
        return asdict(self)


def action_id(*parts: object) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def derive_buckets(
    roots: list[Path] | tuple[Path, ...],
    movie_dirs: list[Path] | tuple[Path, ...] = (),
    show_dirs: list[Path] | tuple[Path, ...] = (),
    explicit: dict | None = None,
) -> dict[str, tuple[Path, ...]]:
    """Build tidy destination buckets from config or root leaf names."""
    buckets: dict[str, list[Path]] = {name: [] for name in BUCKET_ORDER}
    if explicit:
        for name, values in explicit.items():
            key = str(name).strip().lower()
            if key not in buckets:
                continue
            buckets[key] = [Path(item) for item in values]
        # Always keep import destinations available unless the owner overrode them.
        if not buckets["movies"]:
            buckets["movies"] = [Path(path) for path in movie_dirs]
        if not buckets["shows"]:
            buckets["shows"] = [Path(path) for path in show_dirs]
        return {key: tuple(_unique(paths)) for key, paths in buckets.items() if paths}

    for path in movie_dirs:
        buckets["movies"].append(Path(path))
    for path in show_dirs:
        buckets["shows"].append(Path(path))

    for root in roots:
        path = Path(root)
        name = str(path)
        leaf = path.name
        if _PWNAGE.search(name):
            continue
        if _ANIME.search(name):
            if _MOVIE.search(leaf) or _MOVIE.search(name):
                buckets["anime_movies"].append(path)
            else:
                buckets["anime_shows"].append(path)
            continue
        if _KIDS.search(name):
            if _MOVIE.search(leaf) or _MOVIE.search(name):
                buckets["kids_movies"].append(path)
            else:
                buckets["kids_shows"].append(path)
            continue
        if _CHRISTMAS.search(name):
            buckets["christmas"].append(path)
            continue
        if _DOC.search(name):
            buckets["documentary"].append(path)
            continue
        if _STANDUP.search(name):
            buckets["standup"].append(path)
            continue
        if _WORKOUT.search(name):
            buckets["workouts"].append(path)
            continue
        special = bool(
            _KIDS.search(name)
            or _ANIME.search(name)
            or _CHRISTMAS.search(name)
            or _DOC.search(name)
            or _STANDUP.search(name)
            or _WORKOUT.search(name)
        )
        if _MOVIE.search(leaf) and path not in buckets["movies"] and not special:
            buckets["movies"].append(path)
        elif _SHOW.search(leaf) and path not in buckets["shows"] and not special:
            buckets["shows"].append(path)
    return {key: tuple(_unique(paths)) for key, paths in buckets.items() if paths}


def bucket_of(path: Path | str, buckets: dict[str, tuple[Path, ...]]) -> str | None:
    target = Path(path)
    best: tuple[str, int] | None = None
    for name, roots in buckets.items():
        for root in roots:
            if confined(target, root):
                length = len(str(root.resolve())) if root.exists() else len(str(root))
                if best is None or length > best[1]:
                    best = (name, length)
    return None if best is None else best[0]


def classify_copy(copy: Copy, genres: set[str] | None = None) -> str | None:
    """Return a bucket family hint: kids/anime/christmas/documentary/standup/workouts/movies/shows."""
    tags = {tag.lower() for tag in (genres or set())}
    for tag in tags:
        mapped = GENRE_BUCKETS.get(tag)
        if mapped:
            if mapped == "kids":
                return "kids_shows" if copy.kind == "episode" else "kids_movies"
            if mapped == "anime":
                return "anime_shows" if copy.kind == "episode" else "anime_movies"
            return mapped
    # Title heuristics for christmas / standup / workout when genres are thin.
    title = " ".join(
        part for part in [(copy.show or ""), copy.title] if part
    ).lower()
    if "christmas" in title or "holiday" in title:
        return "christmas"
    if "standup" in title or "stand-up" in title or "stand up" in title:
        return "standup"
    if "workout" in title or "fitness" in title:
        return "workouts"
    if copy.kind == "episode":
        return "shows"
    if copy.kind == "movie":
        return "movies"
    # Disk-only files: season token ⇒ show, else movie.
    if copy.season:
        return "shows"
    return "movies"


def expected_path(copy: Copy, root: Path) -> Path | None:
    path = Path(copy.path)
    name = safe_filename(path.name)
    if copy.kind == "episode" or (copy.season is not None and copy.show):
        show = safe_name(cleaned_title(copy.show or copy.title) or (copy.show or copy.title or "show"))
        if copy.season is None:
            return None
        folder = root / show / f"Season {int(copy.season):02d}"
        if copy.episode:
            return folder / f"{show} S{int(copy.season):02d}E{int(copy.episode):02d}{path.suffix.lower()}"
        return folder / name
    label = Label("movie", cleaned_title(copy.title) or copy.title or path.stem, copy.year)
    return root / movie_folder(label) / name


def audit(
    copies: list[Copy],
    *,
    plex_db: Path | None,
    buckets: dict[str, tuple[Path, ...]],
    roots: list[Path] | tuple[Path, ...],
    upgrade_paths: set[str] | None = None,
    free_of=None,
    margin: int = 0,
) -> tuple[list[Action], list[str]]:
    """Scan copies and return proposed actions + space summary notes."""
    notes: list[str] = []
    actions: list[Action] = []
    upgrade_paths = {path_key(path) for path in (upgrade_paths or set())}
    plex_known = _plex_known_paths(plex_db)
    genres_by_path = _plex_genres_by_path(plex_db) if plex_db else {}
    sections = []
    if plex_db and plex_db.exists():
        try:
            sections = load_sections(plex_db)
        except (OSError, sqlite3.Error):
            log.warning("could not read Plex section locations", exc_info=True)
            sections = []

    actions.extend(
        _duplicate_actions(copies, plex_db, buckets, upgrade_paths)
    )
    actions.extend(_orphan_actions(copies, plex_known, sections, roots))
    actions.extend(
        _misplaced_actions(copies, buckets, genres_by_path, free_of, margin)
    )
    actions.extend(_layout_actions(copies, buckets))
    notes.extend(_space_notes(buckets, free_of))

    # Deduplicate by id; prefer lower priority number already sorted later.
    by_id: dict[str, Action] = {}
    for action in actions:
        by_id.setdefault(action.id, action)
    # If a file will move buckets, drop same-source layout renames (stale otherwise).
    moving = {
        path_key(action.source)
        for action in by_id.values()
        if action.kind == "move" and action.source
    }
    if moving:
        by_id = {
            key: action
            for key, action in by_id.items()
            if not (
                action.kind == "rename"
                and action.source
                and path_key(action.source) in moving
            )
        }
    ordered = sorted(by_id.values(), key=lambda row: (row.priority, row.id))
    return ordered, notes


def apply_action(
    row: dict,
    *,
    roots: list[Path] | tuple[Path, ...],
    trash_dir: Path | None,
    plex=None,
    library=None,
    free_of=None,
    margin: int = 0,
    recycle: bool | None = None,
) -> str:
    """Apply one pending tidy action. Returns a user-facing message."""
    kind = str(row.get("kind") or "")
    if kind == "trash":
        return _apply_trash(row, roots, trash_dir, plex, library, recycle=recycle)
    if kind == "scan":
        return _apply_scan(row, plex, roots)
    if kind in {"move", "rename"}:
        return _apply_move(row, roots, plex, library, free_of, margin)
    if kind == "report":
        return str(row.get("detail") or row.get("title") or "Report only.")
    return f"Unknown tidy action kind: {kind}"


def format_report(
    actions: list[dict] | list[Action],
    *,
    notes: list[str] | None = None,
    audited_at: str | None = None,
    limit: int = 40,
) -> str:
    rows = [
        action.as_row() if isinstance(action, Action) else dict(action)
        for action in actions
    ]
    pending = [
        row for row in rows
        if row.get("status", "pending") in {"pending", "blocked"}
    ]
    pending.sort(key=lambda row: (int(row.get("priority") or 99), str(row.get("id"))))
    if not pending and not notes:
        stamp = f" (audited {audited_at})" if audited_at else ""
        return f"Library looks tidy{stamp}. No pending actions."
    lines = []
    if audited_at:
        lines.append(f"Tidy audit {audited_at}")
    counts: dict[str, int] = {}
    for row in pending:
        if row.get("status") != "pending":
            continue
        counts[str(row.get("kind"))] = counts.get(str(row.get("kind")), 0) + 1
    if counts:
        summary = ", ".join(
            f"{count} {kind}"
            for kind, count in sorted(
                counts.items(), key=lambda item: PRIORITY.get(item[0], 99),
            )
        )
        lines.append(f"Pending: {summary}")
    blocked = sum(1 for row in pending if row.get("status") == "blocked")
    if blocked:
        lines.append(f"Blocked/report-only: {blocked}")
    lines.append(
        "Owner commands: /tidy apply <id>   /tidy skip <id>\n"
        "/tidy apply dupes   /tidy orphans\n"
        "/tidy apply renames 50   /tidy apply moves 1-20\n"
        "/tidy apply renames 1,5,9   /tidy skip renames all\n"
        "Selectors: 50 | 1-100 | 1:50 | 1,5,987 | s01 | s01:05 | s01e03 | all"
    )
    shown = pending[:limit]
    for index, row in enumerate(shown, start=1):
        lines.append(_action_line(row, index=index))
    extra = len(pending) - len(shown)
    if extra > 0:
        lines.append(f"…and {extra} more (indexes continue after this page on re-audit)")
    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines)


def _action_line(row: dict, index: int | None = None) -> str:
    kind = row.get("kind")
    aid = row.get("id")
    title = row.get("title") or ""
    detail = row.get("detail") or ""
    source = row.get("source") or ""
    keep = row.get("keep") or ""
    dest = row.get("dest") or ""
    confidence = row.get("confidence") or ""
    prefix = f"#{index} " if index is not None else ""
    weak = " weak" if confidence == "weak" else ""
    bits = [f"{prefix}id {aid}  [{kind}{weak}]  {title}", f"  {detail}"]
    if keep:
        bits.append(f"  keep: {keep}")
    if source:
        bits.append(f"  source: {source}")
    if dest:
        bits.append(f"  dest: {dest}")
    return "\n".join(bits)


def _duplicate_actions(
    copies: list[Copy],
    plex_db: Path | None,
    buckets: dict[str, tuple[Path, ...]],
    upgrade_paths: set[str],
) -> list[Action]:
    actions: list[Action] = []
    covered: set[str] = set()

    if plex_db and plex_db.exists():
        by_item = plex_parts_by_item(plex_db)
        height_by_path = {path_key(copy.path): copy.height for copy in copies}
        for item_id, parts in by_item.items():
            if len(parts) < 2:
                continue
            ranked = []
            for path, height in parts:
                ranked.append(
                    (
                        height or height_by_path.get(path) or 0,
                        _file_size(path),
                        _bucket_rank(path, buckets),
                        path,
                    )
                )
            ranked.sort(key=lambda row: (row[0], row[1], -row[2]), reverse=True)
            winner = ranked[0][3]
            # Refuse auto-trash when no copy has a known height — size alone is a weak signal.
            if not any(height for height, _size, _rank, _path in ranked):
                continue
            for height, _size, _rank, loser in ranked[1:]:
                if loser in upgrade_paths or path_key(loser) in covered:
                    continue
                # Require a known, strictly lower height — never trash on size alone.
                if not height or height >= (ranked[0][0] or 0):
                    continue
                covered.add(path_key(loser))
                label = _label_for_path(copies, loser) or Path(loser).stem
                actions.append(
                    Action(
                        id=action_id("trash", loser, winner),
                        kind="trash",
                        priority=PRIORITY["trash"],
                        title=str(label),
                        detail=(
                            f"Duplicate on same Plex item #{item_id}. "
                            f"Keep {ranked[0][0]}p, trash {height}p copy."
                        ),
                        source=loser,
                        keep=winner,
                        status="pending",
                        confidence="plex",
                    )
                )

    # Weak groups for paths not already covered by Plex same-item dupes.
    groups: dict[tuple, list[Copy]] = {}
    for copy in copies:
        key = _weak_key(copy)
        if key is None:
            continue
        groups.setdefault(key, []).append(copy)
    for key, group in groups.items():
        unique: dict[str, Copy] = {}
        for copy in group:
            unique[path_key(copy.path)] = copy
        if len(unique) < 2:
            continue
        ranked = sorted(
            unique.values(),
            key=lambda copy: (
                copy.height or 0,
                _file_size(copy.path),
                -_bucket_rank(copy.path, buckets),
            ),
            reverse=True,
        )
        winner = ranked[0]
        if not any(copy.height for copy in ranked):
            continue
        for loser in ranked[1:]:
            loser_key = path_key(loser.path)
            if loser_key in covered or loser_key in upgrade_paths:
                continue
            if not loser.height or not winner.height or loser.height >= winner.height:
                continue
            covered.add(loser_key)
            # Title-only matches can collide; keep pending but mark weak so
            # /tidy apply dupes skips them unless the owner targets the id.
            actions.append(
                Action(
                    id=action_id("trash", loser.path, winner.path),
                    kind="trash",
                    priority=PRIORITY["trash"],
                    title=winner.label,
                    detail=(
                        f"WEAK title match (not same Plex item). Keep {winner.height}p "
                        f"({Path(winner.path).name}), trash {loser.height}p. "
                        "Apply by id only after you confirm."
                    ),
                    source=loser.path,
                    keep=winner.path,
                    status="pending",
                    confidence="weak",
                )
            )
    return actions


def _orphan_actions(
    copies: list[Copy],
    plex_known: set[str],
    sections: list[tuple[int, Path]],
    roots: list[Path] | tuple[Path, ...],
) -> list[Action]:
    actions = []
    # Group orphans by parent directory for fewer scan actions.
    by_dir: dict[str, list[Copy]] = {}
    blocked = []
    for copy in copies:
        key = path_key(copy.path)
        if key in plex_known:
            continue
        path = Path(copy.path)
        if not under_roots(path, roots):
            continue
        sid = section_id(sections, path.parent) if sections else None
        if sid is None:
            blocked.append(copy)
            continue
        parent = str(path.parent)
        by_dir.setdefault(parent, []).append(copy)
    for parent, group in sorted(by_dir.items()):
        sample = group[0]
        actions.append(
            Action(
                id=action_id("scan", parent),
                kind="scan",
                priority=PRIORITY["scan"],
                title=sample.label,
                detail=f"{len(group)} file(s) on disk but not in Plex under {parent}",
                source=parent,
                dest=parent,
                status="pending",
            )
        )
    for copy in blocked:
        actions.append(
            Action(
                id=action_id("blocked", copy.path),
                kind="report",
                priority=PRIORITY["report"],
                title=copy.label,
                detail=(
                    "On disk under library roots but not in any Plex section. "
                    "Add the folder in Plex, then /tidy again."
                ),
                source=copy.path,
                status="blocked",
            )
        )
    return actions


def _misplaced_actions(
    copies: list[Copy],
    buckets: dict[str, tuple[Path, ...]],
    genres_by_path: dict[str, set[str]],
    free_of,
    margin: int,
) -> list[Action]:
    actions = []
    for copy in copies:
        current = bucket_of(copy.path, buckets)
        wanted = classify_copy(copy, genres_by_path.get(path_key(copy.path)))
        if not wanted or wanted not in buckets:
            continue
        if current == wanted:
            continue
        roots = buckets.get(wanted) or ()
        if not roots:
            continue
        size = _file_size(copy.path)
        dest_root = _pick_root(roots, size, margin, free_of)
        if dest_root is None:
            actions.append(
                Action(
                    id=action_id("move-blocked", copy.path, wanted),
                    kind="report",
                    priority=PRIORITY["report"],
                    title=copy.label,
                    detail=f"Belongs in {wanted}, but no drive has enough free space.",
                    source=copy.path,
                    bucket=wanted,
                    status="blocked",
                )
            )
            continue
        dest = expected_path(copy, dest_root)
        if dest is None:
            continue
        if path_key(dest) == path_key(copy.path):
            continue
        actions.append(
            Action(
                id=action_id("move", copy.path, dest),
                kind="move",
                priority=PRIORITY["move"],
                title=copy.label,
                detail=f"Misplaced in {current or 'unknown'}; move to {wanted}.",
                source=copy.path,
                dest=str(dest),
                bucket=wanted,
                status="pending",
            )
        )
    return actions


def _layout_actions(
    copies: list[Copy],
    buckets: dict[str, tuple[Path, ...]],
) -> list[Action]:
    actions = []
    for copy in copies:
        current_bucket = bucket_of(copy.path, buckets)
        if not current_bucket:
            continue
        roots = buckets.get(current_bucket) or ()
        root = _root_for(Path(copy.path), roots)
        if root is None:
            continue
        expected = expected_path(copy, root)
        if expected is None:
            continue
        if path_key(expected) == path_key(copy.path):
            continue
        # Only rewrite layout when the destination is free or is the same file.
        if expected.exists() and path_key(expected) != path_key(copy.path):
            actions.append(
                Action(
                    id=action_id("rename-blocked", copy.path, expected),
                    kind="report",
                    priority=PRIORITY["report"],
                    title=copy.label,
                    detail=(
                        f"Messy layout under {root.name}, but normalized path "
                        f"already exists: {expected.name}"
                    ),
                    source=copy.path,
                    dest=str(expected),
                    bucket=current_bucket,
                    status="blocked",
                )
            )
            continue
        actions.append(
            Action(
                id=action_id("rename", copy.path, expected),
                kind="rename",
                priority=PRIORITY["rename"],
                title=copy.label,
                detail=f"Messy layout under {root.name}; normalize folder/name.",
                source=copy.path,
                dest=str(expected),
                bucket=current_bucket,
                status="pending",
            )
        )
    return actions


def _space_notes(buckets: dict[str, tuple[Path, ...]], free_of) -> list[str]:
    def _free(path: Path) -> int:
        if free_of is not None:
            return int(free_of(path))
        return int(shutil.disk_usage(path).free)

    lines = ["Drive space:"]
    seen = set()
    for name in BUCKET_ORDER:
        for root in buckets.get(name) or ():
            key = path_key(root)
            if key in seen:
                continue
            seen.add(key)
            try:
                if not Path(root).exists():
                    lines.append(f"  {root}: not connected")
                    continue
                free = _free(Path(root))
            except OSError:
                lines.append(f"  {root}: not connected")
                continue
            lines.append(f"  {root}: {gb(free)} free  [{name}]")
    return lines if len(lines) > 1 else []


def _apply_trash(row, roots, trash_dir, plex, library, recycle=None) -> str:
    source = Path(str(row.get("source") or ""))
    keep = str(row.get("keep") or "")
    if not keep:
        raise OSError("Duplicate trash requires a keep path.")
    keep_path = Path(keep)
    # Re-validate ledger paths at apply time (do not trust a tampered tidy.json).
    _assert_safe_library_file(keep_path, roots)
    _assert_safe_library_file(source, roots)
    if path_key(source) == path_key(keep_path):
        raise OSError("Duplicate trash source and keep path must differ.")
    # Prefer trashing the smaller/weaker file if a ledger row was swapped.
    if _file_size(source) > _file_size(keep_path):
        raise OSError(
            "Refusing to trash a larger file than the keep path; re-run /tidy."
        )
    dest = move_to_trash(
        source,
        trash_dir,
        roots,
        title=str(row.get("title") or ""),
        recycle=recycle,
    )
    parents = []
    if source.parent.exists():
        parents.append(source.parent)
    if plex is not None and parents:
        notes, _refreshed = plex.refresh_dirs(parents)
    else:
        notes = []
    if library is not None:
        library.invalidate()
    where = "Recycle Bin" if dest == RECYCLE_BIN else str(dest)
    message = f"Trashed duplicate:\n{source}\n→ {where}"
    if notes:
        message += "\n" + "\n".join(notes)
    return message


def _apply_scan(row, plex, roots) -> str:
    directory = Path(str(row.get("source") or row.get("dest") or ""))
    try:
        info = directory.lstat()
    except OSError as exc:
        raise OSError("Scan path is missing.") from exc
    if stat.S_ISLNK(info.st_mode):
        raise OSError("Refusing to scan a symlink path from the tidy ledger.")
    try:
        resolved = directory.resolve()
    except OSError as exc:
        raise OSError("Scan path is missing.") from exc
    if not under_roots(resolved, roots):
        raise OSError("Refusing to scan a folder outside the library roots.")
    if plex is None:
        return f"Files are in {resolved}. Plex scan is not configured."
    notes, refreshed = plex.refresh_dirs([resolved])
    if refreshed:
        return "\n".join(notes) or f"Asked Plex to scan {resolved}."
    detail = "\n".join(notes) if notes else f"Could not scan {resolved}."
    raise OSError(detail)


def _apply_move(row, roots, plex, library, free_of, margin) -> str:
    source = Path(str(row.get("source") or ""))
    dest = Path(str(row.get("dest") or ""))
    _assert_safe_library_file(source, roots)
    # Destination must stay under some configured library root.
    if not under_roots(dest, roots):
        raise OSError("Refusing to move outside the library roots.")
    if not confined(dest, _owning_root(dest, roots)):
        raise OSError("Refusing to move outside the destination root.")
    if dest.exists() and path_key(dest) != path_key(source):
        raise OSError("Destination already exists.")
    needed = _file_size(source)
    owning = _owning_root(dest, roots)
    if free_of is None:
        def free_of(path: Path) -> int:
            return int(shutil.disk_usage(path).free)
    # Same-volume renames need no extra free space beyond the file itself.
    try:
        same_drive = source.resolve().anchor == dest.resolve().anchor
    except OSError:
        same_drive = False
    if not same_drive:
        free = int(free_of(owning))
        if free < needed + margin:
            raise OSError("Not enough free space on the destination drive.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _assert_safe_library_file(source, roots)
    if not under_roots(dest, roots):
        raise OSError("Refusing to move outside the library roots.")
    shutil.move(str(source), str(dest))
    if not under_roots(dest, roots):
        raise OSError("Move escaped the library roots.")
    _prune_empty_parents(source.parent, roots)
    parents = [source.parent, dest.parent]
    notes = []
    if plex is not None:
        notes, _refreshed = plex.refresh_dirs(parents)
    if library is not None:
        library.note_files([dest])
        library.invalidate()
    message = f"Moved:\n{source}\n→ {dest}"
    if notes:
        message += "\n" + "\n".join(notes)
    return message


def _assert_safe_library_file(source: Path, roots) -> None:
    try:
        info = source.lstat()
    except OSError as exc:
        raise OSError("Library file is missing.") from exc
    if stat.S_ISLNK(info.st_mode):
        raise OSError("Refusing to move a symlink.")
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Refusing to move a non-regular library path.")
    if not under_roots(source, roots):
        raise OSError("Refusing to move a file outside the library roots.")


def _prune_empty_parents(start: Path, roots) -> None:
    current = start
    while True:
        if not under_roots(current, roots):
            return
        if any(confined(current, Path(root)) and path_key(current) == path_key(root) for root in roots):
            return
        try:
            next(current.iterdir())
            return
        except StopIteration:
            pass
        except OSError:
            return
        parent = current.parent
        try:
            current.rmdir()
        except OSError:
            return
        current = parent


def _owning_root(path: Path, roots) -> Path:
    best = None
    best_len = -1
    for root in roots:
        root = Path(root)
        if confined(path, root):
            length = len(str(root))
            if length > best_len:
                best = root
                best_len = length
    if best is None:
        raise OSError("Path is not under any library root.")
    return best


def _root_for(path: Path, roots: tuple[Path, ...]) -> Path | None:
    best = None
    best_len = -1
    for root in roots:
        if confined(path, root):
            length = len(str(root))
            if length > best_len:
                best = root
                best_len = length
    return best


def _pick_root(roots, needed: int, margin: int, free_of) -> Path | None:
    if free_of is None:
        def free_of(path: Path) -> int:
            return int(shutil.disk_usage(path).free)
    report = survey(roots, needed, margin, free_of)
    return report.root


def _plex_known_paths(plex_db: Path | None) -> set[str]:
    if plex_db is None or not plex_db.exists():
        return set()
    known = set()
    for parts in plex_parts_by_item(plex_db).values():
        for path, _height in parts:
            known.add(path)
    return known


def _plex_genres_by_path(plex_db: Path | None) -> dict[str, set[str]]:
    if plex_db is None or not plex_db.exists():
        return {}
    uri = plex_db.resolve().as_posix()
    connection = sqlite3.connect(f"file:{uri}?mode=ro&immutable=1", uri=True)
    try:
        # tag_type 1 = genre in Plex.
        rows = connection.execute(
            """
            SELECT mp.file, t.tag
            FROM media_parts mp
            JOIN media_items m ON m.id = mp.media_item_id AND m.deleted_at IS NULL
            JOIN metadata_items mi ON mi.id = m.metadata_item_id AND mi.deleted_at IS NULL
            JOIN taggings tg ON tg.metadata_item_id = mi.id
            JOIN tags t ON t.id = tg.tag_id AND t.tag_type = 1
            WHERE mp.deleted_at IS NULL AND mp.file IS NOT NULL AND t.tag IS NOT NULL
            """
        )
        by_path: dict[str, set[str]] = {}
        for file_path, tag in rows:
            if not file_path or not tag:
                continue
            by_path.setdefault(path_key(file_path), set()).add(str(tag).lower())
        return by_path
    except sqlite3.Error:
        log.warning("could not read Plex genres", exc_info=True)
        return {}
    finally:
        connection.close()


def _weak_key(copy: Copy) -> tuple | None:
    if copy.kind == "episode" or (copy.season is not None and copy.show):
        show = " ".join(tokenize(cleaned_title(copy.show or "") or (copy.show or "")))
        if not show or copy.season is None or copy.episode is None:
            return None
        return ("episode", show, int(copy.season), int(copy.episode))
    title = " ".join(tokenize(cleaned_title(copy.title or "") or (copy.title or "")))
    if not title:
        return None
    return ("movie", title, copy.year)


def _label_for_path(copies: list[Copy], path: str) -> str | None:
    key = path_key(path)
    for copy in copies:
        if path_key(copy.path) == key:
            return copy.label
    return None


def _bucket_rank(path: str | Path, buckets: dict[str, tuple[Path, ...]]) -> int:
    name = bucket_of(path, buckets)
    if name is None:
        return 99
    try:
        return BUCKET_ORDER.index(name)
    except ValueError:
        return 99


def _file_size(path: str | Path) -> int:
    try:
        return int(Path(path).stat().st_size)
    except OSError:
        return 0


def _unique(paths: list[Path]) -> list[Path]:
    seen = set()
    out = []
    for path in paths:
        key = path_key(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def upgrade_replace_paths(states: dict[str, dict]) -> set[str]:
    """Paths currently claimed by the import upgrade review flow."""
    active = {"pending", "awaiting_import", "awaiting_plex", "needs_video"}
    found = set()
    for state in states.values():
        if state.get("upgrade_status") not in active:
            continue
        replace = state.get("replace_path")
        if replace:
            found.add(path_key(replace))
    return found
