from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from plex_queue.config import Config

log = logging.getLogger(__name__)


def _private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class Access:
    def __init__(self, config: Config):
        self.path = config.data_dir / "access.json"
        self.owner_id = config.owner_id
        self.allowed = set(config.allowed_user_ids)
        self._lock = threading.Lock()
        self._load()

    def is_owner(self, user_id: int) -> bool:
        return self.owner_id != 0 and user_id == self.owner_id

    def is_allowed(self, user_id: int) -> bool:
        return self.is_owner(user_id) or user_id in self.allowed

    def claim(self, user_id: int) -> str:
        with self._lock:
            self._load()
            if self.owner_id:
                return "This bot already has an owner."
            self.owner_id = user_id
            self._save()
            return "You are the owner. Add other people with /allow and their id from /whoami."

    def allow(self, user_id: int) -> str:
        with self._lock:
            self._load()
            self.allowed.add(user_id)
            self._save()
            return f"Allowed {user_id}."

    def deny(self, user_id: int) -> str:
        with self._lock:
            self._load()
            self.allowed.discard(user_id)
            self._save()
            return f"Removed {user_id}."

    def roster(self) -> str:
        people = ", ".join(str(item) for item in sorted(self.allowed)) or "none"
        return f"Owner: {self.owner_id or 'unset'}\nAllowed: {people}"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            bad = self.path.with_suffix(".json.bad")
            log.warning("access file unreadable (%s); moving to %s", exc, bad)
            try:
                self.path.replace(bad)
            except OSError:
                pass
            return
        if not isinstance(raw, dict):
            return
        # Persisted claim wins over the YAML bootstrap owner when present.
        try:
            file_owner = int(raw.get("owner_id") or 0)
        except (TypeError, ValueError):
            file_owner = 0
        if file_owner:
            self.owner_id = file_owner
        try:
            self.allowed = {int(item) for item in raw.get("allowed_user_ids") or []}
        except (TypeError, ValueError):
            self.allowed = set()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"owner_id": self.owner_id, "allowed_user_ids": sorted(self.allowed)},
                indent=2,
            ),
            encoding="utf-8",
        )
        _private(temporary)
        temporary.replace(self.path)
        _private(self.path)
