from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from plex_queue.place import Label

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


def _private(path: Path) -> None:
    from plex_queue.securefs import protect_dir, protect_file

    protect_dir(path.parent)
    protect_file(path)


class Ledger:
    def __init__(self, path: Path):
        self.path = path
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
            if replace_path:
                states = data.setdefault("states", {})
                current = dict(states.get(digest) or {})
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

    def flush(self) -> None:
        with self._lock:
            self._flush_unlocked()

    def _ensure(self) -> dict:
        if self._data is None:
            self._data = self._read()
            self._dirty = False
        return self._data

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            bad = self.path.with_suffix(".json.bad")
            log.warning("import ledger unreadable (%s); moving to %s", exc, bad)
            try:
                self.path.replace(bad)
            except OSError:
                pass
            return {}
        return raw if isinstance(raw, dict) else {}

    def _flush_unlocked(self) -> None:
        if self._data is None or not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        _private(temporary)
        temporary.replace(self.path)
        _private(self.path)
        self._dirty = False
