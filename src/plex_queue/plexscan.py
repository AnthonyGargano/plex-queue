from __future__ import annotations

import os
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path


def read_plex_token() -> str:
    if os.name != "nt":
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server") as key:
            value, _kind = winreg.QueryValueEx(key, "PlexOnlineToken")
    except OSError:
        return ""
    return str(value or "")


def load_sections(db: Path) -> list[tuple[int, Path]]:
    if not db.exists():
        return []
    uri = db.resolve().as_posix()
    connection = sqlite3.connect(f"file:{uri}?mode=ro&immutable=1", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT library_section_id, root_path
            FROM section_locations
            WHERE root_path IS NOT NULL
            """
        ).fetchall()
    finally:
        connection.close()
    return [(int(section_id), Path(root)) for section_id, root in rows]


def section_id(sections: list[tuple[int, Path]], directory: Path) -> int | None:
    try:
        target = str(Path(directory).resolve()).lower().rstrip("\\/")
    except OSError:
        target = str(directory).lower().rstrip("\\/")
    best: tuple[int, int] | None = None
    for sid, root in sections:
        try:
            folder = str(Path(root).resolve()).lower().rstrip("\\/")
        except OSError:
            folder = str(root).lower().rstrip("\\/")
        if target == folder or target.startswith(folder + "\\") or target.startswith(folder + "/"):
            if best is None or len(folder) > best[1]:
                best = (sid, len(folder))
    return None if best is None else best[0]


def refresh_section(base: str, section: int, directory: Path, token: str, urlopen) -> None:
    query = urllib.parse.urlencode({"path": str(directory)})
    request = urllib.request.Request(
        f"{base.rstrip('/')}/library/sections/{section}/refresh?{query}",
        headers={"X-Plex-Token": token, "Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=30) as response:
        response.read()


class PlexScan:
    def __init__(self, db: Path | None, token_reader=read_plex_token, urlopen=None, base: str = "http://127.0.0.1:32400"):
        self.db = db
        self.token_reader = token_reader
        self.urlopen = urlopen or urllib.request.urlopen
        self.base = base

    def refresh_dirs(self, directories: list[Path]) -> tuple[list[str], list[Path]]:
        """Ask Plex to scan folders. Returns (notes, directories that were actually refreshed)."""
        token = self.token_reader()
        if not token:
            return (
                ["Files are in place. Plex was not scanned because the server token is missing."],
                [],
            )
        sections = load_sections(self.db) if self.db else []
        notes = []
        refreshed: list[Path] = []
        seen = set()
        for directory in directories:
            sid = section_id(sections, directory)
            key = (sid, str(directory))
            if key in seen:
                continue
            seen.add(key)
            if sid is None:
                notes.append(
                    f"Files are in {directory}. That folder is not in a Plex library, so I did not scan it."
                )
                continue
            try:
                refresh_section(self.base, sid, directory, token, self.urlopen)
            except Exception as exc:
                notes.append(f"Files are in {directory}. Plex scan failed: {exc}")
                continue
            notes.append(f"Asked Plex to scan {directory}.")
            refreshed.append(directory)
        return notes, refreshed
