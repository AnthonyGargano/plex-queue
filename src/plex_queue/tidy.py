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
from plex_queue.titles import (
    RESOLUTION_NUMBERS,
    Request,
    TAGS,
    YEAR,
    parse_request,
    tokenize,
    video_height,
)
from plex_queue.upgrades import (
    RECYCLE_BIN,
    move_to_trash,
    path_key,
    plex_parts_by_item,
)

_EXTRA_DIR_NAMES = frozenset({
    "extras", "extra", "bonus", "bonuses", "featurettes", "featurette",
    "interviews", "interview", "trailers", "trailer", "samples", "sample",
    "deleted scenes", "deleted_scenes", "behind the scenes", "behind_the_scenes",
})
_EXTRA_NAME_MARKERS = (
    "post-show", "after-show", "aftershow", "talking.", "deleted scene",
    "bonus scene", "behind the scenes", "featurette",
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

# Intentional specialized shelves — never pull files off them. Plex genres are
# noisy (White Christmas → family/kids while it belongs on the Christmas shelf;
# Dragon Ball Z / Malcolm already live on anime/kids). Only propose moves
# *into* these buckets from generic movies/shows.
_SPECIALIZED_BUCKETS = frozenset({
    "kids_movies",
    "kids_shows",
    "anime_movies",
    "anime_shows",
    "christmas",
    "documentary",
    "standup",
    "workouts",
})

# Show trees keep featurettes/extras/holiday episodes. Plex often tags those
# clips as movies or holiday, which must not rip them into Movies/Christmas.
_SHOW_FAMILY = frozenset({"shows", "kids_shows", "anime_shows"})
_KIDS_EXPLICIT = frozenset({"kids", "children"})

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

_STANDUP_TAGS = frozenset({"stand-up", "standup", "stand up"})
_CHRISTMAS_TAGS = frozenset({"holiday", "christmas"})
_DOC_TAGS = frozenset({"documentary"})
_WORKOUT_TAGS = frozenset({"fitness", "workout", "workouts"})
# Strict person standup title: optional honorific + First Last: Special.
# Allow initials/abbreviations (Louis C.K., Ms. Pat). Never match franchises
# like "Star Wars: …" / "Men in Black: …" via the denylist below.
_STANDUP_SPECIAL = re.compile(
    # "Dr Jason Leong: …" / "Ms. Pat: …"
    r"^(?:dr\.?|ms\.?|mr\.?|mrs\.?)\s+"
    r"[a-z][a-z'’.]*(?:\s+[a-z][a-z'’.]*){1,2}:\s+\S"
    # Exactly "First Last: …" (Louis C.K. ok). Not "American Pie Presents: …".
    r"|^(?!the\b|a\b|an\b)[a-z][a-z'’.]*\s+[a-z][a-z'’.]*:\s+\S",
    re.IGNORECASE,
)
_NOT_STANDUP_TITLE = re.compile(
    r"(?i)^(?:"
    r"south park|star wars|star trek|american pie|austin powers|men in black|"
    r"guardians of|pirates of|history of|book club|police academy|walk hard|"
    r"ace ventura|bridget jones|impractical jokers|absolutely fabulous|"
    r"another gay|playing with|puppet master|operation fortune|quantum leap|"
    r"resident evil|mission impossible|fast and furious|john wick|"
    r"airplane|robin hood|lost boys|jackass|mr\.?\s*monk|behind the mask|"
    r"to all the boys|pathological"
    r")\b"
    r"|\b(?:the\s+)?movie\b|\bsequel\b|\bvol\.?\b|\bvolume\b|\bepisode\b|"
    r"\bpart\s+\d\b"
)
_EPISODEISH_TITLE = re.compile(
    r"(?i)\bS\d{1,2}(?:E\d{1,3}|EX\w*|M\d+)\b|"
    r"\bgag reel\b|\bsizzle reel\b|\bdeleted scenes?\b|"
    r"\binside the episode\b|\bcharacter retrospectives?\b"
)
_MISPLACED_DETAIL = re.compile(
    r"Misplaced in ([^;]+); move to ([^.]+)",
    re.IGNORECASE,
)
# Release-group clutter in a folder name — the only in-place rename we propose.
_RELEASE_FOLDER_JUNK = re.compile(
    r"(?i)(?:"
    r"[\[({][^\[\](){}]{0,40}\b(?:1080p|720p|2160p|480p|4k|webrip|web-?dl|bluray|"
    r"bdrip|dvdrip|hdtv|yts\.?mx|yts\.?am|yify|rarbg|x264|x265|hevc|10bit|"
    r"aac|dts|truehd|atmos|remux|proper|repack|amzn|nf|hulu)\b"
    r"|"
    r"\b(?:1080p|720p|2160p|webrip|web-?dl|bluray|yts|yify|rarbg)\b"
    r")"
)
# Prefer keeping "This Is 40 (2012)" / "Rush Hour 3 (2007)" when stripping junk.
_TITLE_YEAR_HEAD = re.compile(
    r"^(?P<head>.+?\(\d{4}\))"
    r"(?P<junk>\s*[\[({].*)$",
    re.IGNORECASE,
)
_COLLECTION_PACK = re.compile(
    r"(?i)\b(?:complete|collection|trilogy|quadrilogy|filmography|anthology)\b"
    r"|\d{4}\s*[-–]\s*\d{4}"
)
# Extra release tokens cleaned_title may leave on dotted folder names.
_LAYOUT_EXTRA_TAGS = frozenset({
    "remastered", "restored", "criterion", "hybrid", "readnfo", "nfo",
    "tgx", "publichd", "galaxyrg", "judas", "tigole", "mpc", "bokutox",
})


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
    keep_height: int | None = None
    source_height: int | None = None

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
    episode = copy.kind == "episode" or copy.season is not None
    title = " ".join(
        part for part in [(copy.show or ""), copy.title] if part
    ).lower()

    # Fixed priority — Plex often tags standup specials as Documentary (+ Comedy).
    if tags & _STANDUP_TAGS:
        return "standup"
    if "anime" in tags:
        return "anime_shows" if episode else "anime_movies"
    if tags & _KIDS_EXPLICIT or ("family" in tags and "animation" in tags):
        return "kids_shows" if episode else "kids_movies"
    # Holiday genre on movies only; never from bare "holiday" in a title
    # (Roman Holiday is a romance, not Christmas).
    if not episode and (tags & _CHRISTMAS_TAGS or "christmas" in title):
        return "christmas"
    # Plex often tags standup specials as Documentary + Comedy. Require both
    # plus a person-name title — comedy alone + "Foo: Bar" yanked Star Wars etc.
    if (
        not episode
        and tags & _DOC_TAGS
        and "comedy" in tags
        and _looks_like_standup_special(title)
    ):
        return "standup"
    if tags & _DOC_TAGS:
        return "documentary"
    if tags & _WORKOUT_TAGS or "workout" in title or "fitness" in title:
        return "workouts"
    # Episode/extra-shaped titles sitting in a movie library.
    if not episode and _EPISODEISH_TITLE.search(title):
        return "shows"
    if episode:
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
    prior_moves: list[dict] | None = None,
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
    recovered = _recovery_actions(
        copies, buckets, prior_moves or (), free_of, margin,
    )
    actions.extend(recovered)
    recovered_keys = {path_key(action.source) for action in recovered if action.source}
    actions.extend(
        _misplaced_actions(
            copies, buckets, genres_by_path, free_of, margin,
            skip_paths=recovered_keys,
        )
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
    upgrade_paths: set[str] | None = None,
) -> str:
    """Apply one pending tidy action. Returns a user-facing message."""
    kind = str(row.get("kind") or "")
    if kind == "trash":
        return _apply_trash(
            row,
            roots,
            trash_dir,
            plex,
            library,
            recycle=recycle,
            upgrade_paths=upgrade_paths,
        )
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
        # Break down the hidden tail so "24710 more" is not mistaken for more moves.
        rest = pending[limit:]
        rest_counts: dict[str, int] = {}
        blocked_rest = 0
        for row in rest:
            if row.get("status") == "blocked":
                blocked_rest += 1
                continue
            kind = str(row.get("kind") or "?")
            rest_counts[kind] = rest_counts.get(kind, 0) + 1
        parts = [
            f"{count} {kind}"
            for kind, count in sorted(
                rest_counts.items(), key=lambda item: PRIORITY.get(item[0], 99),
            )
        ]
        if blocked_rest:
            parts.append(f"{blocked_rest} blocked")
        detail = ", ".join(parts) if parts else "actions"
        lines.append(
            f"…and {extra} more ({detail}; showing first {limit} — "
            f"use apply moves 1-20 / renames 50; indexes continue on re-audit)"
        )
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
            # Plex often keeps stale media_parts; only rank files that still exist.
            live = [
                (path, height)
                for path, height in parts
                if _regular_library_file(path)
            ]
            if len(live) < 2:
                continue
            ranked = []
            for path, height in live:
                ranked.append(
                    (
                        0 if _looks_like_extra(path) else 1,
                        height or height_by_path.get(path) or 0,
                        _file_size(path),
                        _bucket_rank(path, buckets),
                        path,
                    )
                )
            ranked.sort(
                key=lambda row: (row[0], row[1], row[2], -row[3]),
                reverse=True,
            )
            winner = ranked[0][4]
            winner_height = ranked[0][1]
            # Refuse auto-trash when no copy has a known height — size alone is a weak signal.
            if not any(height for _main, height, _size, _rank, _path in ranked):
                continue
            for _main, height, _size, _rank, loser in ranked[1:]:
                loser_key = path_key(loser)
                if loser_key in upgrade_paths or loser_key in covered:
                    continue
                # Require a known, strictly lower height — never trash on size alone.
                if not height or height >= (winner_height or 0):
                    continue
                if not _duplicate_pair_ok(winner, loser):
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
                            f"Keep {winner_height}p, trash {height}p copy."
                        ),
                        source=loser,
                        keep=winner,
                        status="pending",
                        confidence="plex",
                        keep_height=int(winner_height),
                        source_height=int(height),
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
            [
                copy for copy in unique.values()
                if _regular_library_file(copy.path)
            ],
            key=lambda copy: (
                0 if _looks_like_extra(copy.path) else 1,
                copy.height or 0,
                _file_size(copy.path),
                -_bucket_rank(copy.path, buckets),
            ),
            reverse=True,
        )
        if len(ranked) < 2:
            continue
        winner = ranked[0]
        if not any(copy.height for copy in ranked):
            continue
        for loser in ranked[1:]:
            loser_key = path_key(loser.path)
            if loser_key in covered or loser_key in upgrade_paths:
                continue
            if not loser.height or not winner.height or loser.height >= winner.height:
                continue
            if not _duplicate_pair_ok(winner.path, loser.path):
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
                    keep_height=int(winner.height),
                    source_height=int(loser.height),
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
    skip_paths: set[str] | None = None,
) -> list[Action]:
    actions = []
    skip_paths = skip_paths or set()
    for copy in copies:
        if path_key(copy.path) in skip_paths:
            continue
        if _in_trash_tree(copy.path):
            continue
        current = bucket_of(copy.path, buckets)
        # Outside configured library roots (Downloads/Documents/…) — not tidy.
        if current is None:
            continue
        # Extras/featurettes stay with the parent show tree.
        if _looks_like_extra(copy.path):
            continue
        wanted = classify_copy(copy, genres_by_path.get(path_key(copy.path)))
        if not wanted or wanted not in buckets:
            continue
        if current == wanted:
            continue
        # Trust intentional specialized shelves entirely (christmas/kids/anime/…).
        if current in _SPECIALIZED_BUCKETS:
            continue
        # Show shelves keep extras, holiday episodes, and documentary/standup
        # clips with the series. Only allow show→show rehomes (kids/anime).
        if current in _SHOW_FAMILY and wanted not in _SHOW_FAMILY:
            continue
        if copy.kind == "episode" and wanted not in _SHOW_FAMILY:
            continue
        action = _propose_move(copy, current, wanted, buckets, free_of, margin)
        if action is not None:
            actions.append(action)
    return actions


def _recovery_actions(
    copies: list[Copy],
    buckets: dict[str, tuple[Path, ...]],
    prior_moves: list[dict] | tuple[dict, ...],
    free_of,
    margin: int,
) -> list[Action]:
    """Undo earlier tidy moves that current policy would refuse.

    Yesterday's shows→movies / kids→shows / standup→movies applies left extras
    and specials in the wrong shelf; propose putting them back.
    """
    by_path = {path_key(copy.path): copy for copy in copies}
    # After layout renames, the ledger dest path may no longer exist — find the
    # same filename still sitting in the destination bucket.
    by_bucket_name: dict[tuple[str, str], list[Copy]] = {}
    for copy in copies:
        bucket = bucket_of(copy.path, buckets)
        if not bucket:
            continue
        key = (bucket, Path(copy.path).name.casefold())
        by_bucket_name.setdefault(key, []).append(copy)
    actions: list[Action] = []
    seen: set[str] = set()
    for row in prior_moves:
        if row.get("kind") != "move" or row.get("status") != "done":
            continue
        detail = str(row.get("detail") or "")
        matched = _MISPLACED_DETAIL.search(detail)
        if not matched:
            continue
        from_bucket = matched.group(1).strip().lower()
        to_bucket = matched.group(2).strip().lower()
        original_source = str(row.get("source") or "")
        current_path = str(row.get("dest") or "")
        if not current_path:
            continue
        copy = by_path.get(path_key(current_path))
        # Basename fallback when ledger dest was renamed after the move.
        # Require parent-folder overlap under generic shelves so common names
        # like Gag Reel.mkv cannot bind the wrong file.
        if copy is None and (
            to_bucket in _SPECIALIZED_BUCKETS
            or from_bucket in _SPECIALIZED_BUCKETS
        ):
            candidates = by_bucket_name.get(
                (to_bucket, Path(current_path).name.casefold())
            ) or []
            dest_folder = Path(current_path).parent.name.casefold()
            if to_bucket in _SPECIALIZED_BUCKETS and len(candidates) == 1:
                copy = candidates[0]
            else:
                ranked = [
                    item for item in candidates
                    if dest_folder
                    and dest_folder in Path(item.path).parent.name.casefold()
                ]
                copy = ranked[0] if len(ranked) == 1 else None
        if copy is None:
            continue
        if path_key(copy.path) in seen:
            continue
        if _in_trash_tree(copy.path):
            continue
        title = " ".join(
            part for part in [(copy.show or ""), copy.title] if part
        )
        if not _should_undo_move(
            from_bucket,
            to_bucket,
            original_source,
            title,
            copy=copy,
        ):
            continue
        if from_bucket not in buckets:
            continue
        current = bucket_of(copy.path, buckets)
        if current == from_bucket:
            continue
        # Prefer the exact original path when the parent folder still exists.
        orig = Path(original_source) if original_source else None
        if orig is not None and orig.parent.exists():
            dest = orig
            if path_key(dest) == path_key(copy.path):
                continue
            actions.append(
                Action(
                    id=action_id("undo-move", copy.path, from_bucket),
                    kind="move",
                    priority=PRIORITY["move"],
                    title=copy.label,
                    detail=f"Undo bad tidy move; return to {from_bucket}.",
                    source=copy.path,
                    dest=str(dest),
                    bucket=from_bucket,
                    status="pending",
                )
            )
            seen.add(path_key(copy.path))
            continue
        action = _propose_move(
            copy, current or to_bucket, from_bucket, buckets, free_of, margin,
            detail=f"Undo bad tidy move; return to {from_bucket}.",
            id_prefix="undo-move",
        )
        if action is not None:
            actions.append(action)
            seen.add(path_key(copy.path))
    return actions


def _should_undo_move(
    from_bucket: str,
    to_bucket: str,
    original_source: str,
    title: str,
    *,
    copy: Copy | None = None,
) -> bool:
    if from_bucket == to_bucket:
        return False
    # Never should have left an intentional specialized shelf.
    if from_bucket in _SPECIALIZED_BUCKETS:
        return True
    # Comedy "Title: Subtitle" false positives dumped features into Standup.
    if to_bucket == "standup" and from_bucket in {
        "movies", "shows", "kids_movies", "anime_movies",
    }:
        if copy is not None and (
            copy.kind == "episode" or copy.season is not None
        ):
            return True
        return not _looks_like_standup_special(title)
    # Show→movies (etc.) only when the original path/title looks like an extra
    # or episode — do not yank real features that lived under Shows.
    if from_bucket in _SHOW_FAMILY and to_bucket not in _SHOW_FAMILY:
        if copy is not None and (
            copy.kind == "episode" or copy.season is not None
        ):
            return True
        return bool(
            _looks_like_extra(original_source)
            or _EPISODEISH_TITLE.search(title or "")
            or _EPISODEISH_TITLE.search(original_source or "")
        )
    return False


def _looks_like_standup_special(title: str) -> bool:
    text = (title or "").strip()
    if not text or "concert" in text.casefold():
        return False
    if _NOT_STANDUP_TITLE.search(text):
        return False
    return bool(_STANDUP_SPECIAL.search(text))


def _propose_move(
    copy: Copy,
    current: str | None,
    wanted: str,
    buckets: dict[str, tuple[Path, ...]],
    free_of,
    margin: int,
    *,
    detail: str | None = None,
    id_prefix: str = "move",
) -> Action | None:
    roots = buckets.get(wanted) or ()
    if not roots:
        return None
    size = _file_size(copy.path)
    dest_root = _pick_root(roots, size, margin, free_of)
    if dest_root is None:
        return Action(
            id=action_id(f"{id_prefix}-blocked", copy.path, wanted),
            kind="report",
            priority=PRIORITY["report"],
            title=copy.label,
            detail=f"Belongs in {wanted}, but no drive has enough free space.",
            source=copy.path,
            bucket=wanted,
            status="blocked",
        )
    dest = expected_path(copy, dest_root)
    if dest is None:
        return None
    if path_key(dest) == path_key(copy.path):
        return None
    return Action(
        id=action_id(id_prefix, copy.path, dest),
        kind="move",
        priority=PRIORITY["move"],
        title=copy.label,
        detail=detail or f"Misplaced in {current or 'unknown'}; move to {wanted}.",
        source=copy.path,
        dest=str(dest),
        bucket=wanted,
        status="pending",
    )


def _layout_actions(
    copies: list[Copy],
    buckets: dict[str, tuple[Path, ...]],
) -> list[Action]:
    """Propose conservative in-place renames only.

    Full Plex-style rewrites (strip episode titles, drop show years, pad seasons)
    are too lossy for an existing library. Only clean clear release-group folder
    clutter on movies, and keep the original filename.
    """
    actions = []
    for copy in copies:
        if _looks_like_extra(copy.path) or _in_trash_tree(copy.path):
            continue
        # Episodes keep their titles/years/season wording — do not rewrite.
        if copy.kind == "episode" or copy.season is not None:
            continue
        current_bucket = bucket_of(copy.path, buckets)
        if not current_bucket:
            continue
        roots = buckets.get(current_bucket) or ()
        root = _root_for(Path(copy.path), roots)
        if root is None:
            continue
        expected = _gentle_movie_layout_dest(copy, root)
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
                        f"Release clutter under {root.name}, but clean folder "
                        f"already exists: {expected.parent.name}"
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
                detail=(
                    f"Release clutter under {root.name}; "
                    f"clean folder name (keep filename)."
                ),
                source=copy.path,
                dest=str(expected),
                bucket=current_bucket,
                status="pending",
            )
        )
    return actions


def _gentle_movie_layout_dest(copy: Copy, root: Path) -> Path | None:
    """Return a cleaned movie path only when the parent folder has release junk.

    Keeps the original filename. Prefers stripping junk from the existing
    ``Title (Year) [YTS…]`` folder so numbers like ``This Is 40`` / ``Rush Hour 3``
    survive. Falls back to Plex title only for dotted release folders.
    """
    path = Path(copy.path)
    parent = path.parent
    name = path.name.casefold()
    if name.startswith("extra ") or name.startswith("extras ") or name.startswith("extra."):
        return None
    try:
        under_root = parent == root or root in parent.parents
    except (OSError, ValueError):
        under_root = False
    if not under_root:
        return None
    # Multi-movie packs — leave alone (would peel one title out of the set).
    if _COLLECTION_PACK.search(parent.name):
        return None
    directly_under_root = parent == root
    junk_folder = _RELEASE_FOLDER_JUNK.search(parent.name) is not None
    # Loose files on the bucket root: only act when the filename itself has
    # release clutter (do not wrap an already-clean Title (Year).mkv).
    junk_name = (
        _RELEASE_FOLDER_JUNK.search(path.name) is not None
        or _title_looks_like_release(path.stem)
    )
    if directly_under_root:
        if not junk_name:
            return None
    elif not junk_folder:
        return None

    stripped = None if directly_under_root else _strip_release_junk_folder(parent.name)
    if stripped:
        clean_folder = safe_name(stripped)
    else:
        # Dotted release folder or file on the root. Prefer a clean Plex title
        # (keeps "This Is 40"); humanize when Plex itself stored a release dump.
        label = Label(
            "movie",
            _layout_movie_title(copy, path, directly_under_root=directly_under_root),
            copy.year,
        )
        clean_folder = movie_folder(label)

    base = root if directly_under_root else parent.parent
    try:
        if base != root and root not in base.parents:
            return None
    except (OSError, ValueError):
        return None
    # Avoid Title (Year)/Title (Year)/file when junk sat under an already-clean folder.
    if path_key(base.name) == path_key(clean_folder):
        return base / path.name
    dest = base / clean_folder / path.name
    if path_key(dest) == path_key(path):
        return None
    return dest


def _strip_release_junk_folder(name: str) -> str | None:
    """Return ``Title (Year)`` when ``name`` is that plus release brackets/tags."""
    text = (name or "").strip()
    if not text or not _RELEASE_FOLDER_JUNK.search(text):
        return None
    matched = _TITLE_YEAR_HEAD.match(text)
    if not matched:
        return None
    head = matched.group("head").strip()
    junk = matched.group("junk") or ""
    if not head or not junk.strip():
        return None
    if path_key(head) == path_key(text):
        return None
    return head


def _layout_movie_title(
    copy: Copy,
    path: Path,
    *,
    directly_under_root: bool = False,
) -> str:
    """Folder title for dotted release layouts; keep sequel digits."""
    raw = (copy.title or "").strip()
    if raw and not _title_looks_like_release(raw):
        return raw
    # Never seed from the bucket root name (e.g. "Movies") when the file sits
    # directly under it — use the filename stem instead.
    if directly_under_root:
        seed = raw or path.stem
    else:
        seed = raw or path.parent.name or path.stem
    return _humanize_release_title(seed) or raw or path.stem


def _title_looks_like_release(text: str) -> bool:
    if _RELEASE_FOLDER_JUNK.search(text):
        return True
    # Some.Like.It.Hot.1959.REMASTERED.1080p…
    if text.count(".") >= 2 and YEAR.search(text):
        return True
    return False


def _humanize_release_title(text: str) -> str:
    """Strip release tokens; keep short digit words (3, 40) unlike cleaned_title."""
    work = (text or "").strip()
    suffix = Path(work).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        work = work[: -len(suffix)]
    work = YEAR.sub(" ", work)
    work = re.sub(r"\[[^\]]*\]", " ", work)
    work = re.sub(r"\{[^}]*\}", " ", work)
    work = re.sub(r"[._]+", " ", work)
    kept: list[str] = []
    for word in work.split():
        token = word.strip(" -()[]")
        if not token:
            continue
        lower = token.lower()
        if lower in TAGS or lower in _LAYOUT_EXTRA_TAGS:
            continue
        if lower in RESOLUTION_NUMBERS or f".{lower}" in VIDEO_EXTENSIONS:
            continue
        parts = [part for part in re.split(r"[-]+", lower) if part]
        if parts and all(
            part in TAGS or part in _LAYOUT_EXTRA_TAGS or part in RESOLUTION_NUMBERS
            for part in parts
        ):
            continue
        # Drop pure digit tokens only when they are resolution heights.
        if token.isdigit() and token in RESOLUTION_NUMBERS:
            continue
        kept.append(token)
    title = re.sub(r"\s+", " ", " ".join(kept)).strip(" -")
    return title


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


def _apply_trash(
    row, roots, trash_dir, plex, library, recycle=None, upgrade_paths=None,
) -> str:
    source = Path(str(row.get("source") or ""))
    keep = str(row.get("keep") or "")
    if not keep:
        raise OSError("Duplicate trash requires a keep path.")
    keep_path = Path(keep)
    # Re-validate ledger paths at apply time (do not trust a tampered tidy.json).
    _assert_safe_library_file(keep_path, roots, label="Keep path")
    _assert_safe_library_file(source, roots, label="Source path")
    if path_key(source) == path_key(keep_path):
        raise OSError("Duplicate trash source and keep path must differ.")
    claimed = {path_key(path) for path in (upgrade_paths or set())}
    if path_key(source) in claimed or path_key(keep_path) in claimed:
        raise OSError(
            "That file is claimed by an active upgrade; finish /drop or /keep, then re-run /tidy."
        )
    if not _duplicate_pair_ok(str(keep_path), str(source)):
        raise OSError(
            "Keep/source no longer look like the same title; re-run /tidy."
        )
    # Prefer live filename tags; fall back to Plex heights stored at audit time.
    keep_height = video_height(keep_path.name) or _row_height(row.get("keep_height"))
    source_height = video_height(source.name) or _row_height(row.get("source_height"))
    if not keep_height or not source_height:
        raise OSError(
            "Could not re-verify resolution for this duplicate; re-run /tidy."
        )
    if source_height >= keep_height:
        raise OSError(
            "Source is no longer lower resolution than keep; re-run /tidy."
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
    # Cross-disk moves copy then delete; Ctrl+C mid-copy can leave a partial dest.
    try:
        shutil.move(str(source), str(dest))
    except BaseException:
        _cleanup_partial_dest(source, dest)
        raise
    if not under_roots(dest, roots):
        raise OSError("Move escaped the library roots.")
    _prune_empty_parents(source.parent, roots)
    if library is not None:
        # Update the in-memory index before Plex scan so a SQLite blip cannot
        # leave the move unmarked after the file is already relocated.
        library.remember_move(source, dest)
    parents = [source.parent, dest.parent]
    notes = []
    if plex is not None:
        try:
            notes, _refreshed = plex.refresh_dirs(parents)
        except OSError as exc:
            # Keep the move; Plex can catch up on the next scan.
            notes = [f"Moved, but Plex scan failed: {exc}"]
    message = f"Moved:\n{source}\n→ {dest}"
    if notes:
        message += "\n" + "\n".join(notes)
    return message


def _cleanup_partial_dest(source: Path, dest: Path) -> None:
    """Remove an incomplete cross-disk copy when source is still present."""
    try:
        if not source.exists() or not dest.exists():
            return
        if path_key(source) == path_key(dest):
            return
        dest.unlink(missing_ok=True)
    except OSError:
        pass


def _assert_safe_library_file(source: Path, roots, *, label: str = "Library file") -> None:
    try:
        info = source.lstat()
    except OSError as exc:
        raise OSError(f"{label} is missing.") from exc
    if stat.S_ISLNK(info.st_mode):
        raise OSError("Refusing to move a symlink.")
    if not stat.S_ISREG(info.st_mode):
        raise OSError("Refusing to move a non-regular library path.")
    if not under_roots(source, roots):
        raise OSError("Refusing to move a file outside the library roots.")


def _row_height(value) -> int | None:
    try:
        height = int(value)
    except (TypeError, ValueError):
        return None
    return height if height > 0 else None


def _regular_library_file(path: str | Path) -> bool:
    try:
        info = Path(path).lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _looks_like_extra(path: str | Path) -> bool:
    """True for extras/bonus folders or after-show style filenames."""
    text = str(path).replace("/", "\\")
    parts = [part for part in text.split("\\") if part]
    for part in parts[:-1]:
        lowered = part.strip().casefold()
        if lowered in _EXTRA_DIR_NAMES or lowered.startswith("season ") and "extra" in lowered:
            return True
        if "after-show" in lowered or "bonus" in lowered:
            return True
    name = (parts[-1] if parts else "").casefold()
    return any(marker in name for marker in _EXTRA_NAME_MARKERS)


def _in_trash_tree(path: str | Path) -> bool:
    """True when a path segment is a trash/recycle folder (never rehome those)."""
    for part in Path(path).parts:
        lowered = part.strip().casefold()
        if lowered in {"_trash", "trash", "$recycle.bin", "recycle.bin"}:
            return True
    return False


def _duplicate_pair_ok(keep: str | Path, loser: str | Path) -> bool:
    """Reject Plex same-item pairs that are clearly different titles/extras.

    Title similarity uses **filenames only** — full paths pollute tokens with the
    show folder name and make unrelated episodes look related.
    """
    keep_s = str(keep)
    loser_s = str(loser)
    keep_extra = _looks_like_extra(keep_s)
    loser_extra = _looks_like_extra(loser_s)
    # Never trash a main library file just to keep an extras/bonus clip.
    if keep_extra and not loser_extra:
        return False
    # Stem only — extensions like ".avi" must not count as title overlap.
    keep_name = parse_request(Path(keep_s).stem)
    loser_name = parse_request(Path(loser_s).stem)
    keep_season, keep_episode = _season_episode(keep_s, keep_name)
    loser_season, loser_episode = _season_episode(loser_s, loser_name)
    if (
        keep_season is not None
        and keep_episode is not None
        and loser_season is not None
        and loser_episode is not None
        and (keep_season, keep_episode) != (loser_season, loser_episode)
    ):
        return False
    keep_tokens = set(keep_name.tokens)
    loser_tokens = set(loser_name.tokens)
    # Both opaque: allow. One-sided opaque vs titled: treat as no overlap.
    if not keep_tokens and not loser_tokens:
        return True
    overlap = keep_tokens & loser_tokens
    if not overlap:
        return False
    # If each filename has its own exclusive title words, they are different
    # episodes even when they share show tokens (Doctor Who "cosmic time" vs
    # "firemaker", both tagged S01E04 on the same Plex item).
    keep_only = keep_tokens - loser_tokens
    loser_only = loser_tokens - keep_tokens
    if keep_only and loser_only:
        return False
    union = keep_tokens | loser_tokens
    # Sparse vs verbose names that only share a show word (low Jaccard).
    if len(union) >= 4 and (len(overlap) / len(union)) <= 0.4:
        return False
    return True


def _season_episode(path: str, parsed: Request) -> tuple[int | None, int | None]:
    if parsed.season is not None and parsed.episode is not None:
        return parsed.season, parsed.episode
    fuller = parse_request(path.replace("\\", " ").replace("/", " "))
    if fuller.season is not None and fuller.episode is not None:
        return fuller.season, fuller.episode
    return parsed.season, parsed.episode


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
