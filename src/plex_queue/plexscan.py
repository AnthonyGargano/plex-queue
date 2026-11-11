from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from plex_queue.errors import public_error


@dataclass(frozen=True)
class PlexActivity:
    type: str
    title: str
    subtitle: str
    progress: int
    section_id: int | None = None


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


def _plex_request(
    url: str,
    token: str,
    urlopen,
    *,
    method: str = "GET",
) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"X-Plex-Token": token, "Accept": "application/json"},
        method=method,
    )
    with urlopen(request, timeout=30) as response:
        return response.read() or b""


def parse_activities(payload: bytes | str) -> list[PlexActivity]:
    """Parse Plex ``/activities`` JSON or XML into activity rows."""
    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    text = (text or "").strip()
    if not text:
        return []
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            return _activities_from_json(data)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    return _activities_from_xml(root)


def _activities_from_json(data: object) -> list[PlexActivity]:
    container = data
    if isinstance(data, dict):
        container = data.get("MediaContainer", data)
    rows = []
    if isinstance(container, dict):
        raw = container.get("Activity") or container.get("activity") or []
        if isinstance(raw, dict):
            raw = [raw]
        rows = raw if isinstance(raw, list) else []
    elif isinstance(container, list):
        rows = container
    out: list[PlexActivity] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        context = row.get("Context") or row.get("context") or {}
        if isinstance(context, list):
            context = context[0] if context and isinstance(context[0], dict) else {}
        if not isinstance(context, dict):
            context = {}
        section = context.get("librarySectionID") or context.get("librarySectionId")
        try:
            section_id = int(section) if section is not None else None
        except (TypeError, ValueError):
            section_id = None
        try:
            progress = int(row.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        out.append(
            PlexActivity(
                type=str(row.get("type") or ""),
                title=str(row.get("title") or ""),
                subtitle=str(row.get("subtitle") or ""),
                progress=max(0, min(progress, 100)),
                section_id=section_id,
            )
        )
    return out


def _activities_from_xml(root: ET.Element) -> list[PlexActivity]:
    out: list[PlexActivity] = []
    for node in root.findall(".//Activity"):
        context = node.find("Context")
        section_id = None
        if context is not None:
            raw = context.attrib.get("librarySectionID") or context.attrib.get(
                "librarySectionId"
            )
            try:
                section_id = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                section_id = None
        try:
            progress = int(node.attrib.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0
        out.append(
            PlexActivity(
                type=str(node.attrib.get("type") or ""),
                title=str(node.attrib.get("title") or ""),
                subtitle=str(node.attrib.get("subtitle") or ""),
                progress=max(0, min(progress, 100)),
                section_id=section_id,
            )
        )
    return out


def is_scan_activity(item: PlexActivity) -> bool:
    kind = (item.type or "").lower()
    title = (item.title or "").lower()
    if "library.update" in kind or kind.endswith(".scan"):
        return True
    return title.startswith("scanning") or "scanning " in title


def format_scan_activities(activities: list[PlexActivity]) -> str:
    """Human summary of scan-related Plex activities."""
    scans = [item for item in activities if is_scan_activity(item)]
    others = [item for item in activities if item not in scans][:3]
    if not scans and not others:
        return "No active Plex background tasks."
    lines: list[str] = []
    for item in scans[:6]:
        lines.append(_format_activity_line(item))
    for item in others:
        lines.append(_format_activity_line(item))
    return "\n".join(lines)


def _format_activity_line(item: PlexActivity) -> str:
    title = (item.title or "Plex activity").strip() or "Plex activity"
    subtitle = (item.subtitle or "").strip()
    detail = f"{title} — {subtitle}" if subtitle else title
    if item.progress:
        return f"• {detail} ({item.progress}%)"
    return f"• {detail}"


def refresh_section(
    base: str,
    section: int,
    directory: Path | None,
    token: str,
    urlopen,
) -> None:
    """Ask Plex to scan one library section, optionally limited to ``directory``."""
    url = f"{base.rstrip('/')}/library/sections/{section}/refresh"
    if directory is not None:
        url = f"{url}?{urllib.parse.urlencode({'path': str(directory)})}"
    _plex_request(url, token, urlopen, method="GET")


def cancel_all_refreshes(base: str, token: str, urlopen) -> None:
    """Stop every in-progress library refresh (Plex DELETE …/sections/all/refresh)."""
    url = f"{base.rstrip('/')}/library/sections/all/refresh"
    _plex_request(url, token, urlopen, method="DELETE")


class PlexScan:
    def __init__(self, db: Path | None, token_reader=read_plex_token, urlopen=None, base: str = "http://127.0.0.1:32400"):
        self.db = db
        self.token_reader = token_reader
        self.urlopen = urlopen or urllib.request.urlopen
        self.base = base

    def _token_and_sections(self) -> tuple[str | None, list[tuple[int, Path]], list[str]]:
        token = self.token_reader()
        if not token:
            return None, [], ["Plex was not scanned because the server token is missing."]
        try:
            sections = load_sections(self.db) if self.db else []
        except (OSError, sqlite3.Error) as exc:
            return None, [], [f"Plex DB could not be read: {public_error(exc)}"]
        return token, sections, []

    def refresh_dirs(self, directories: list[Path]) -> tuple[list[str], list[Path]]:
        """Ask Plex to scan folders. Returns (notes, directories that were actually refreshed)."""
        token, sections, early = self._token_and_sections()
        if token is None:
            notes = [
                note if note.startswith("Plex DB") else f"Files are in place. {note}"
                for note in early
            ]
            return notes or [
                "Files are in place. Plex was not scanned because the server token is missing."
            ], []
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
                notes.append(
                    f"Files are in {directory}. Plex scan failed: {public_error(exc)}"
                )
                continue
            notes.append(f"Asked Plex to scan {directory}.")
            refreshed.append(directory)
        return notes, refreshed

    def refresh_all(self, *, restart: bool = True) -> tuple[list[str], list[int]]:
        """Ask Plex to fully refresh every library section.

        When ``restart`` is true (default), cancel any in-progress scans first so
        a scan started before large renames does not keep walking stale paths.

        Returns (notes, section ids that were successfully kicked off).
        """
        token, sections, early = self._token_and_sections()
        if token is None:
            return early or ["Plex was not scanned because the server token is missing."], []
        if not sections:
            return ["No Plex library sections found in the database."], []
        notes: list[str] = []
        if restart:
            try:
                cancel_all_refreshes(self.base, token, self.urlopen)
                notes.append("Stopped any in-progress Plex library scans.")
            except Exception as exc:
                notes.append(
                    f"Could not stop in-progress Plex scans: {public_error(exc)} "
                    "(continuing with a fresh refresh anyway)."
                )
        by_sid: dict[int, list[Path]] = {}
        for sid, root in sections:
            by_sid.setdefault(sid, []).append(root)
        refreshed: list[int] = []
        for sid in sorted(by_sid):
            roots = by_sid[sid]
            label = ", ".join(str(root) for root in roots[:3])
            if len(roots) > 3:
                label = f"{label}, +{len(roots) - 3} more"
            try:
                refresh_section(self.base, sid, None, token, self.urlopen)
            except Exception as exc:
                notes.append(f"Plex refresh failed for section {sid} ({label}): {public_error(exc)}")
                continue
            notes.append(f"Asked Plex to refresh section {sid} ({label}).")
            refreshed.append(sid)
        return notes, refreshed

    def list_activities(self) -> list[PlexActivity] | None:
        """Return current Plex activities, or ``None`` if the request failed.

        An empty list means Plex answered and reported no running activities.
        """
        token = self.token_reader()
        if not token:
            return None
        url = f"{self.base.rstrip('/')}/activities"
        try:
            body = _plex_request(url, token, self.urlopen, method="GET")
        except Exception:
            return None
        try:
            return parse_activities(body)
        except Exception:
            return None
