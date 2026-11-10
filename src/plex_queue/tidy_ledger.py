from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from plex_queue.ledger import utc_now_iso
from plex_queue.signed_state import read_signed_json, write_signed_json

log = logging.getLogger(__name__)


class TidyLedger:
    """Persist proposed tidy actions in data/tidy.json."""

    def __init__(self, path: Path, secret: str = ""):
        self.path = path
        self._secret = str(secret or "")
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._dirty = False

    def replace_pending(self, actions: list[dict]) -> None:
        """Replace open rows with a fresh audit; never resurrect skipped/done ids.

        ``failed`` rows are not sticky — a new audit may re-propose the same id
        so the owner can retry after fixing the underlying problem.
        """
        with self._lock:
            data = self._ensure()
            previous = data.get("actions") or {}
            sticky: dict[str, dict] = {}
            for key, row in previous.items():
                status = row.get("status")
                if status in {"done", "skipped"}:
                    sticky[key] = dict(row)
            fresh: dict[str, dict] = {}
            for row in actions:
                key = str(row["id"])
                # User dismissed or successfully finished — do not put it back.
                if key in sticky:
                    continue
                fresh[key] = dict(row)
            # Drop stale pending/blocked/failed; keep sticky history + new open work.
            merged = dict(sticky)
            merged.update(fresh)
            data["actions"] = merged
            data["audited_at"] = utc_now_iso()
            self._dirty = True
            self._flush_unlocked()

    def cancel_related(self, source: str | None, *, except_id: str | None = None) -> int:
        """Mark other pending actions that share a source path as skipped."""
        if not source:
            return 0
        needle = os.path.normcase(os.path.normpath(str(source)))
        cleared = 0
        with self._lock:
            data = self._ensure()
            for key, row in list((data.get("actions") or {}).items()):
                if except_id and key == except_id.lower():
                    continue
                if row.get("status") != "pending":
                    continue
                row_source = row.get("source") or ""
                if os.path.normcase(os.path.normpath(str(row_source))) != needle:
                    continue
                current = dict(row)
                current["status"] = "skipped"
                current["detail"] = (
                    str(current.get("detail") or "")
                    + " (cleared; related action applied)"
                ).strip()
                current["updated_at"] = utc_now_iso()
                data["actions"][key] = current
                cleared += 1
            if cleared:
                self._dirty = True
                self._flush_unlocked()
        return cleared

    def get(self, action_id: str) -> dict | None:
        with self._lock:
            row = self._ensure().get("actions", {}).get(action_id.lower())
            if row is None:
                # Allow short-prefix match when unique.
                matches = [
                    (key, value)
                    for key, value in self._ensure().get("actions", {}).items()
                    if key.startswith(action_id.lower())
                ]
                if len(matches) == 1:
                    return dict(matches[0][1])
                return None
            return dict(row)

    def find(self, needle: str, statuses: set[str] | None = None) -> tuple[str | None, dict | None, str | None]:
        text = needle.strip().lower()
        if not text:
            return None, None, "Usage needs a tidy action id."
        matches = []
        with self._lock:
            for key, row in self._ensure().get("actions", {}).items():
                if statuses and row.get("status") not in statuses:
                    continue
                if key.startswith(text) or key == text:
                    matches.append((key, dict(row)))
        if not matches:
            return None, None, "No tidy action matches that id."
        if len(matches) > 1:
            return None, None, "That id matches more than one tidy action. Use more characters."
        return matches[0][0], matches[0][1], None

    def pending(self, kinds: set[str] | None = None) -> list[dict]:
        return self.list(statuses={"pending"}, kinds=kinds)

    def list(
        self,
        statuses: set[str] | None = None,
        kinds: set[str] | None = None,
    ) -> list[dict]:
        with self._lock:
            rows = []
            for row in self._ensure().get("actions", {}).values():
                if statuses and row.get("status") not in statuses:
                    continue
                if kinds and row.get("kind") not in kinds:
                    continue
                rows.append(dict(row))
        rows.sort(key=lambda row: (int(row.get("priority") or 99), str(row.get("id") or "")))
        return rows

    def mark(self, action_id: str, **fields) -> None:
        with self._lock:
            data = self._ensure()
            actions = data.setdefault("actions", {})
            key = action_id.lower()
            current = dict(actions.get(key) or {})
            if not current:
                # Resolve short prefix under the lock.
                matches = [k for k in actions if k.startswith(key)]
                if len(matches) == 1:
                    key = matches[0]
                    current = dict(actions[key])
            current.update(fields)
            if "updated_at" not in fields:
                current["updated_at"] = utc_now_iso()
            actions[key] = current
            self._dirty = True
            self._flush_unlocked()

    def audited_at(self) -> str | None:
        with self._lock:
            value = self._ensure().get("audited_at")
        return str(value) if value else None

    def flush(self) -> None:
        with self._lock:
            self._flush_unlocked()

    def _ensure(self) -> dict:
        if self._data is None:
            self._data = self._read()
            self._dirty = False
        return self._data

    def _read(self) -> dict:
        raw = read_signed_json(self.path, self._secret, label="tidy ledger")
        if not raw:
            return {"actions": {}}
        raw.setdefault("actions", {})
        if not isinstance(raw.get("actions"), dict):
            raw["actions"] = {}
        return raw

    def _flush_unlocked(self) -> None:
        if self._data is None or not self._dirty:
            return
        write_signed_json(self.path, self._data, self._secret)
        self._dirty = False
