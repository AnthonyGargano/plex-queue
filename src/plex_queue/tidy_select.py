from __future__ import annotations

import re

KIND_WORDS = {
    "dupes": "trash",
    "duplicates": "trash",
    "trash": "trash",
    "orphans": "scan",
    "orphan": "scan",
    "scans": "scan",
    "scan": "scan",
    "renames": "rename",
    "rename": "rename",
    "moves": "move",
    "move": "move",
}

_SEASON = re.compile(
    r"^s(?:eason)?\s*(\d{1,2})(?:\s*[:\-]\s*s?(?:eason)?\s*(\d{1,2}))?$",
    re.IGNORECASE,
)
_RANGE = re.compile(r"^(\d+)\s*[:\-]\s*(\d+)$")
_INDEX = re.compile(r"^\d+$")
_EPISODE = re.compile(r"^s(\d{1,2})e(\d{1,2})$", re.IGNORECASE)


def parse_batch(needle: str) -> tuple[str | None, str]:
    """Split ``renames 1-50`` into kind + selector.

    Returns ``(None, original)`` when this is a single action id, not a batch.
    """
    text = (needle or "").strip()
    if not text:
        return None, ""
    parts = text.split(None, 1)
    mapped = KIND_WORDS.get(parts[0].lower())
    if mapped is None:
        return None, text
    selector = parts[1].strip() if len(parts) > 1 else ""
    return mapped, selector


def select_rows(rows: list[dict], selector: str) -> tuple[list[dict], str | None]:
    """Filter pending rows by index list/range, count, or season code.

    Indices are 1-based in the given ``rows`` order.
    Empty selector means the full list (caller should require ``all`` for
    large rename/move batches when appropriate).
    """
    text = (selector or "").strip().lower()
    if not text or text in {"all", "*"}:
        return list(rows), None

    episode = _EPISODE.fullmatch(text.replace(" ", ""))
    if episode:
        season = int(episode.group(1))
        ep = int(episode.group(2))
        chosen = [row for row in rows if _has_episode(row, season, ep)]
        if not chosen:
            return [], f"No pending items matched S{season:02d}E{ep:02d}."
        return chosen, None

    season_match = _SEASON.fullmatch(text.replace(" ", ""))
    if season_match and "," not in text:
        start = int(season_match.group(1))
        end = int(season_match.group(2) or start)
        if end < start:
            start, end = end, start
        chosen = [row for row in rows if _season_in_range(row, start, end)]
        if not chosen:
            return [], (
                f"No pending items matched S{start:02d}"
                + (f"-S{end:02d}" if end != start else "")
                + "."
            )
        return chosen, None

    if "," in text:
        indexes: set[int] = set()
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            ranged = _RANGE.fullmatch(part)
            if ranged:
                left, right = int(ranged.group(1)), int(ranged.group(2))
                if left < 1 or right < 1:
                    return [], "Indexes are 1-based."
                if right < left:
                    left, right = right, left
                indexes.update(range(left, right + 1))
                continue
            if not _INDEX.fullmatch(part):
                return [], f"Bad index in list: {part}"
            indexes.add(int(part))
        return _by_indexes(rows, indexes)

    ranged = _RANGE.fullmatch(text)
    if ranged:
        left, right = int(ranged.group(1)), int(ranged.group(2))
        if left < 1 or right < 1:
            return [], "Indexes are 1-based."
        if right < left:
            left, right = right, left
        return _by_indexes(rows, set(range(left, right + 1)))

    if _INDEX.fullmatch(text):
        count = int(text)
        if count < 1:
            return [], "Count must be at least 1."
        return rows[:count], None

    return [], (
        "Selector examples: 50   1-100   1:50   1,5,987   s01   s01:05   s01e03   all"
    )


def _by_indexes(rows: list[dict], indexes: set[int]) -> tuple[list[dict], str | None]:
    if not indexes:
        return [], "No indexes given."
    missing = sorted(i for i in indexes if i < 1 or i > len(rows))
    if missing:
        return [], (
            f"Index out of range (1-{len(rows)}): "
            + ", ".join(str(i) for i in missing[:8])
        )
    chosen = [rows[i - 1] for i in sorted(indexes)]
    return chosen, None


def _blob(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("title", "detail", "source", "dest", "keep")
    ).lower()


def _season_in_range(row: dict, start: int, end: int) -> bool:
    blob = _blob(row)
    for season in range(start, end + 1):
        code = f"s{season:02d}"
        # Match S01, S01E02; avoid s01 matching inside s10/s11.
        if re.search(
            rf"(?<![0-9a-z]){re.escape(code)}(?:e\d{{1,2}}|(?![0-9a-z]))",
            blob,
        ):
            return True
        if re.search(rf"\bseason\s*0*{season}\b", blob):
            return True
    return False


def _has_episode(row: dict, season: int, episode: int) -> bool:
    blob = _blob(row)
    code = f"s{season:02d}e{episode:02d}"
    loose = f"s{season}e{episode}"
    if re.search(rf"(?<![0-9a-z]){re.escape(code)}(?![0-9a-z])", blob):
        return True
    return bool(re.search(rf"(?<![0-9a-z]){re.escape(loose)}(?![0-9a-z])", blob))
