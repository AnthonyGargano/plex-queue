from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from plex_queue.titles import (
    EP_WORD,
    IMDB_ID,
    IMDB_URL,
    LEADING_KIND,
    SEASON_EP,
    SEASON_ONLY,
    SEASON_WORD,
    TAGS,
    TRAILING_MOVIE,
    X_EP,
    YEAR,
    parse_request,
)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt"}
SPECIAL = ("kid", "anime", "christmas", "documentar", "standup", "workout", "pwnage")


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int


@dataclass(frozen=True)
class Download:
    hash: str
    name: str
    save_path: str
    files: tuple[RemoteFile, ...]


@dataclass(frozen=True)
class Label:
    kind: str
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None


@dataclass(frozen=True)
class CopyJob:
    remote: str
    local: Path
    size: int


@dataclass(frozen=True)
class SpaceReport:
    root: Path | None
    lines: tuple[str, ...]


def general_dirs(roots, kind: str) -> tuple[Path, ...]:
    needle = "movie" if kind == "movie" else "show"
    chosen = []
    for root in roots:
        name = str(root).lower()
        leaf = Path(root).name.lower()
        if needle not in leaf or any(word in name for word in SPECIAL):
            continue
        chosen.append(Path(root))
    return tuple(chosen)


def cleaned_title(text: str) -> str:
    work = text.strip()
    suffix = Path(work).suffix.lower()
    if suffix in VIDEO_EXTENSIONS or suffix in SUBTITLE_EXTENSIONS:
        work = work[: -len(suffix)]
    work = IMDB_URL.sub(" ", work)
    work = IMDB_ID.sub(" ", work)
    work = LEADING_KIND.sub(" ", work)
    work = TRAILING_MOVIE.sub(" ", work)
    work = SEASON_EP.sub(" ", work)
    work = X_EP.sub(" ", work)
    work = SEASON_WORD.sub(" ", work)
    work = EP_WORD.sub(" ", work)
    work = SEASON_ONLY.sub(" ", work)
    work = YEAR.sub(" ", work)
    # Drop release-group brackets like [eztv.re] before tokenizing.
    work = re.sub(r"\[[^\]]*\]", " ", work)
    work = re.sub(r"\{[^}]*\}", " ", work)
    work = re.sub(r"[._]+", " ", work)
    kept = []
    for word in work.split():
        token = word.strip(" -()[]")
        if not token:
            continue
        lower = token.lower()
        if lower in TAGS or f".{lower}" in VIDEO_EXTENSIONS:
            continue
        # Hyphenated tags: x265-RARBG → drop when any segment is a known tag.
        parts = [part for part in re.split(r"[-]+", lower) if part]
        if parts and all(part in TAGS or part.isdigit() for part in parts):
            continue
        if any(part in TAGS for part in parts) and any(
            part in {"rarbg", "eztv", "ettv", "yts", "yify", "sparks", "flux", "ntb", "cakes"}
            for part in parts
        ):
            continue
        if SEASON_ONLY.fullmatch(token):
            continue
        kept.append(token)
    title = " ".join(kept)
    title = re.sub(r"\(\s*\)", " ", title)
    title = re.sub(r"\s+", " ", title).strip(" -")
    return title


def safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]', "", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        return "untitled"
    return cleaned


def safe_filename(name: str) -> str:
    return safe_name(Path(name).stem) + Path(name).suffix


def safe_relative(relative: str) -> str | None:
    """Torrent-relative path that cannot escape the download folder."""
    text = relative.replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == ".." or ":" in part:
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def confined(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def under_roots(path: Path, roots: list[Path] | tuple[Path, ...]) -> bool:
    """True when path resolves under any configured library root."""
    for root in roots:
        if confined(path, Path(root)):
            return True
    return False


def gb(amount: int) -> str:
    return f"{amount / 1024 ** 3:.1f} GB"


def is_complete(state: str, progress: float) -> bool:
    """True when Deluge has the full payload ready to copy off the VM.

    Deluge often leaves finished torrents in Queued at 100% (not Seeding).
    /done already treats those as finished; import must too.
    """
    label = state.lower()
    if progress < 99.9:
        return False
    # Still working on the data — wait for a settled finished state.
    if label in {"downloading", "checking", "allocating", "moving", "error"}:
        return False
    return True


def survey(roots, needed: int, margin: int, free_of) -> SpaceReport:
    lines = []
    best: Path | None = None
    best_free = -1
    for root in roots:
        path = Path(root)
        try:
            if not path.exists():
                lines.append(f"{path}: not connected")
                continue
            free = int(free_of(path))
        except OSError:
            lines.append(f"{path}: not connected")
            continue
        lines.append(f"{path}: {gb(free)} free")
        if free >= needed + margin and free > best_free:
            best = path
            best_free = free
    return SpaceReport(best, tuple(lines))


def movie_folder(label: Label) -> str:
    title = safe_name(label.title)
    if label.year:
        return f"{title} ({label.year})"
    return title


def show_folder(label: Label) -> str:
    """Show library folder without a leftover S01 from a season pack name."""
    title = cleaned_title(label.title) or label.title
    return safe_name(title)


def video_destination(label: Label, relative: str, root: Path) -> Path | None:
    name = safe_filename(Path(relative).name)
    parsed = parse_request(relative.replace("\\", " ").replace("/", " "))
    if label.kind == "movie":
        return root / movie_folder(label) / name
    season = parsed.season if parsed.season is not None else label.season
    if season is None:
        return None
    episode = parsed.episode if parsed.episode is not None else label.episode
    folder = show_folder(label)
    filename = name
    if episode and parsed.episode is None:
        filename = f"{folder} S{season:02d}E{episode:02d}{Path(name).suffix}"
    return root / folder / f"Season {season:02d}" / filename


DEFAULT_DOWNLOAD_ROOTS = ("/home/ttt/Downloads",)


def plan(
    download: Download,
    label: Label,
    root: Path,
    download_roots: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[CopyJob], str | None]:
    roots = tuple(download_roots) if download_roots is not None else DEFAULT_DOWNLOAD_ROOTS
    videos = []
    subtitles = []
    for item in download.files:
        if _extra(item.path):
            continue
        if safe_relative(item.path) is None:
            return [], (
                f"Finished, but a file path in {download.name} leaves the download folder, "
                "so I did not copy it."
            )
        suffix = Path(item.path).suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            videos.append(item)
        elif suffix in SUBTITLE_EXTENSIONS:
            subtitles.append(item)
    if not videos:
        return [], f"Finished, but {download.name} has no video file to move."
    if safe_save_path(download.save_path, roots) is None:
        return [], (
            f"Finished, but the Deluge folder for {download.name} is outside the download root, "
            "so I did not copy it."
        )
    root = Path(root)
    jobs = []
    stems = {}
    for video in videos:
        dest = video_destination(label, video.path, root)
        if dest is None:
            return [], (
                f"I know {label.title} is a show, but not which season "
                f"{Path(video.path).name} belongs in."
            )
        remote = remote_join(download.save_path, video.path, roots)
        if remote is None:
            return [], (
                f"Finished, but a file path in {download.name} leaves the download folder, "
                "so I did not copy it."
            )
        jobs.append(CopyJob(remote, dest, video.size))
        stems[Path(video.path).stem.lower()] = dest.parent
    parents = {job.local.parent for job in jobs}
    only = next(iter(parents)) if len(parents) == 1 else None
    for sub in subtitles:
        folder = stems.get(Path(sub.path).stem.lower()) or only
        if folder is None:
            continue
        remote = remote_join(download.save_path, sub.path, roots)
        if remote is None:
            continue
        jobs.append(CopyJob(remote, folder / safe_filename(Path(sub.path).name), sub.size))
    for job in jobs:
        if not confined(job.local, root):
            return [], f"Refusing to write outside {root}."
    return jobs, None


def safe_save_path(save_path: str, allowed_roots: tuple[str, ...] | list[str] | None = None) -> str | None:
    """Absolute download folder that cannot climb with .. segments."""
    text = save_path.replace("\\", "/").strip()
    if not text.startswith("/") or text.startswith("//"):
        return None
    parts = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == ".." or ":" in part:
            return None
        parts.append(part)
    if not parts:
        return None
    base = "/" + "/".join(parts)
    roots = [root.replace("\\", "/").rstrip("/") for root in (allowed_roots or ()) if root]
    # Fail closed: a caller that forgets roots must not skip the prefix check.
    if not roots:
        return None
    if not any(base == root or base.startswith(root + "/") for root in roots):
        return None
    return base


def remote_join(
    save_path: str,
    relative: str,
    allowed_roots: tuple[str, ...] | list[str] | None = None,
) -> str | None:
    roots = DEFAULT_DOWNLOAD_ROOTS if allowed_roots is None else tuple(allowed_roots)
    clean = safe_relative(relative)
    base = safe_save_path(save_path, roots)
    if clean is None or base is None:
        return None
    return f"{base}/{clean}"


def common_parent(paths: list[Path]) -> Path:
    shared: list[str] = []
    for group in zip(*(path.parts for path in paths)):
        if len(set(group)) != 1:
            break
        shared.append(group[0])
    if not shared:
        return paths[0]
    return Path(*shared)


def parse_finished(status: dict) -> tuple[list[Download], set[str]]:
    finished = []
    hashes = set()
    for info_hash, fields in status.items():
        if not isinstance(fields, dict):
            continue
        digest = _field(fields, "hash") or _text(info_hash)
        digest = digest.lower()
        hashes.add(digest)
        name = _field(fields, "name")
        state = _field(fields, "state")
        progress = float(_field(fields, "progress") or 0)
        if not is_complete(state, progress):
            continue
        files = []
        for item in fields.get("files") or fields.get(b"files") or []:
            if not isinstance(item, dict):
                continue
            path = _field(item, "path").replace("\\", "/")
            size = int(_field(item, "size") or 0)
            if path and size > 0:
                files.append(RemoteFile(path, size))
        finished.append(Download(digest, name, _field(fields, "save_path"), tuple(files)))
    return finished, hashes


def _extra(path: str) -> bool:
    stem = Path(path).stem.lower()
    return stem.endswith("sample") or "trailer" in stem


def _field(fields: dict, name: str) -> str:
    if name in fields:
        return _text(fields[name])
    return _text(fields.get(name.encode()))


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")
