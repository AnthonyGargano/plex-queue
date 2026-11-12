from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from plex_queue.place import Label
from plex_queue.signed_state import read_signed_json, write_signed_json


def _title_key_for_label(label: Label) -> str | None:
    from plex_queue.boost import title_group_key_from_label

    return title_group_key_from_label(label)

log = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stamp_from_dest(dest: object) -> str | None:
    if not dest:
        return None
    try:
        mtime = Path(str(dest)).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).replace(microsecond=0).isoformat()


class Ledger:
    def __init__(self, path: Path, secret: str = ""):
        self.path = path
        self._secret = str(secret or "")
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._dirty = False

    def remember(
        self,
        info_hash: str,
        label: Label,
        *,
        replace_path: str | None = None,
        replace_height: int | None = None,
    ) -> None:
        with self._lock:
            data = self._ensure()
            digest = info_hash.lower()
            data.setdefault("labels", {})[digest] = {
                "kind": label.kind,
                "title": label.title,
                "year": label.year,
                "season": label.season,
                "episode": label.episode,
            }
            states = data.setdefault("states", {})
            current = dict(states.get(digest) or {})
            title_key = _title_key_for_label(label)
            if title_key:
                current["title_key"] = title_key
            if replace_path:
                current["replace_path"] = replace_path
                current["replace_height"] = replace_height
                current.setdefault("upgrade_status", "awaiting_import")
            states[digest] = current
            self._dirty = True
            self._flush_unlocked()

    def label(self, info_hash: str) -> Label | None:
        with self._lock:
            raw = self._ensure().get("labels", {}).get(info_hash.lower())
        if not raw:
            return None
        return Label(
            kind=raw["kind"],
            title=raw["title"],
            year=raw.get("year"),
            season=raw.get("season"),
            episode=raw.get("episode"),
        )

    def state(self, info_hash: str) -> dict | None:
        with self._lock:
            found = self._ensure().get("states", {}).get(info_hash.lower())
        return dict(found) if found else None

    def states(self) -> dict[str, dict]:
        with self._lock:
            return {key: dict(value) for key, value in self._ensure().get("states", {}).items()}

    def mark(self, info_hash: str, **fields) -> None:
        with self._lock:
            data = self._ensure()
            states = data.setdefault("states", {})
            current = dict(states.get(info_hash.lower()) or {})
            status = fields.get("status")
            if status in {"imported", "copied"} and "imported_at" not in fields:
                # Stamp the first successful transfer; keep it on later mark()s.
                # Prefer dest mtime when backfilling pre-stamp ledger rows.
                fields = dict(fields)
                fields["imported_at"] = (
                    current.get("imported_at")
                    or _stamp_from_dest(fields.get("dest") or current.get("dest"))
                    or utc_now_iso()
                )
            current.update(fields)
            states[info_hash.lower()] = current
            self._dirty = True

    def link_boost_group(self, original: str, alternate: str) -> str:
        """Link two digests so a finished import can clear the incomplete sibling."""
        return self.link_boost_members(original, [alternate])

    def link_boost_members(self, primary: str, others: list[str]) -> str:
        """Put primary + others on one boost_group (same-title stuck set)."""
        with self._lock:
            data = self._ensure()
            states = data.setdefault("states", {})
            orig = primary.lower()
            current = dict(states.get(orig) or {})
            group = str(current.get("boost_group") or orig).lower()
            current["boost_group"] = group
            states[orig] = current
            for item in others:
                digest = str(item).lower()
                if not digest or digest == orig:
                    continue
                row = dict(states.get(digest) or {})
                row["boost_group"] = group
                row.setdefault("boost_of", orig)
                states[digest] = row
            self._dirty = True
            self._flush_unlocked()
            return group

    def boost_group_members(self, info_hash: str) -> list[str]:
        """Return every digest sharing this torrent's boost_group (including itself)."""
        digest = info_hash.lower()
        state = self.state(digest) or {}
        group = str(state.get("boost_group") or "").lower()
        if not group:
            return [digest]
        members = [
            key for key, row in self.states().items()
            if str(row.get("boost_group") or "").lower() == group
        ]
        if digest not in members:
            members.append(digest)
        return sorted(members)

    def clear_boost_links(self, info_hash: str) -> None:
        with self._lock:
            data = self._ensure()
            states = data.setdefault("states", {})
            digest = info_hash.lower()
            current = dict(states.get(digest) or {})
            current.pop("boost_group", None)
            current.pop("boost_of", None)
            current["boost_cleared"] = True
            states[digest] = current
            self._dirty = True

    def flush(self) -> None:
        with self._lock:
            self._flush_unlocked()

    def _ensure(self) -> dict:
        if self._data is None:
            self._data = self._read()
            self._dirty = False
        return self._data

    def _read(self) -> dict:
        return read_signed_json(self.path, self._secret, label="import ledger")

    def _flush_unlocked(self) -> None:
        if self._data is None or not self._dirty:
            return
        write_signed_json(self.path, self._data, self._secret)
        self._dirty = False
