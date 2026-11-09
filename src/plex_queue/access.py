from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from pathlib import Path

from plex_queue.config import Config, setup_secret_ok
from plex_queue.securefs import protect_dir, protect_file

log = logging.getLogger(__name__)


class Access:
    def __init__(self, config: Config):
        self.path = config.data_dir / "access.json"
        self._secret = str(config.setup_secret or "")
        # YAML is the intentional bootstrap; claim/file may set owner only when YAML is unset.
        self._yaml_owner = int(config.owner_id or 0)
        self._yaml_allowed = {int(item) for item in config.allowed_user_ids}
        self.owner_id = self._yaml_owner
        self._extra_allowed: set[int] = set()
        self._lock = threading.Lock()
        protect_dir(config.data_dir)
        self._load()

    @property
    def allowed(self) -> set[int]:
        return set(self._yaml_allowed) | set(self._extra_allowed)

    @allowed.setter
    def allowed(self, values: set[int]) -> None:
        # Tests and older callers assign the full set; keep YAML ids sticky.
        self._extra_allowed = {int(item) for item in values} - self._yaml_allowed

    def is_owner(self, user_id: int) -> bool:
        return self.owner_id != 0 and user_id == self.owner_id

    def is_allowed(self, user_id: int) -> bool:
        return self.is_owner(user_id) or user_id in self.allowed

    def claim(self, user_id: int) -> str:
        """Take ownership after the caller verified setup_secret.

        First successful claim wins. Secret-backed reclaim is intentionally
        disabled (corporate posture): rotate ownership via ``owner_id`` in
        config.yaml, or delete a verified-corrupt ``access.json`` offline.
        """
        with self._lock:
            self._load()
            if self._yaml_owner and self._yaml_owner != user_id:
                return (
                    "Owner is set in config.yaml (owner_id). "
                    "Change that value if you need a different owner."
                )
            if self._yaml_owner == user_id:
                self.owner_id = user_id
                self._save()
                return "You are the owner (owner_id in config.yaml)."
            if self.owner_id and self.owner_id != user_id:
                return (
                    "This bot already has an owner. "
                    "Set telegram.owner_id in config.yaml, or delete data/access.json "
                    "only after you have verified it should be reset."
                )
            previous = self.owner_id
            self.owner_id = user_id
            self._save()
            if previous == user_id:
                return "You are already the owner."
            return "You are the owner. Add other people with /allow and their id from /whoami."

    def allow(self, user_id: int) -> str:
        with self._lock:
            self._load()
            self._extra_allowed.add(user_id)
            self._save()
            return f"Allowed {user_id}."

    def deny(self, user_id: int) -> str:
        with self._lock:
            self._load()
            if user_id in self._yaml_allowed:
                return (
                    f"{user_id} is listed in config.yaml allowed_user_ids; "
                    "remove them there and restart to revoke."
                )
            self._extra_allowed.discard(user_id)
            self._save()
            return f"Removed {user_id}."

    def roster(self) -> str:
        people = ", ".join(str(item) for item in sorted(self.allowed)) or "none"
        return f"Owner: {self.owner_id or 'unset'}\nAllowed: {people}"

    def _load(self) -> None:
        if not self.path.exists():
            return
        # Unsigned state is never trusted when the setup secret is not production-ready.
        if not setup_secret_ok(self._secret):
            self._quarantine("setup_secret not ready; refusing unsigned access.json")
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._quarantine(exc)
            return
        if not isinstance(raw, dict):
            self._quarantine("access.json root is not an object")
            return
        mac = str(raw.get("mac") or "")
        body = {key: value for key, value in raw.items() if key != "mac"}
        expected = self._mac(body)
        if not mac or not hmac.compare_digest(mac, expected):
            self._quarantine("access.json MAC missing or invalid")
            return
        # New format stores only /allow extras. Legacy mixed lists are quarantined
        # so removing a YAML id cannot leave a sticky guest after restart.
        if "extra_allowed_user_ids" not in body:
            self._quarantine("legacy access.json; re-claim / re-allow after upgrade")
            return
        try:
            file_owner = int(body.get("owner_id") or 0)
        except (TypeError, ValueError):
            file_owner = 0
        if self._yaml_owner:
            self.owner_id = self._yaml_owner
            if file_owner and file_owner != self._yaml_owner:
                log.warning(
                    "Ignoring access.json owner_id=%s; config.yaml owner_id=%s takes precedence",
                    file_owner,
                    self._yaml_owner,
                )
        elif file_owner:
            if not self.owner_id:
                self.owner_id = file_owner
            elif file_owner != self.owner_id:
                log.warning(
                    "Ignoring access.json owner_id=%s; keeping in-memory owner_id=%s",
                    file_owner,
                    self.owner_id,
                )
        try:
            file_extras = {int(item) for item in body.get("extra_allowed_user_ids") or []}
        except (TypeError, ValueError):
            file_extras = set()
        self._extra_allowed = file_extras - self._yaml_allowed

    def _save(self) -> None:
        if not setup_secret_ok(self._secret):
            raise RuntimeError("Refusing to write access.json without a ready setup_secret.")
        protect_dir(self.path.parent)
        body = {
            "owner_id": self.owner_id,
            "extra_allowed_user_ids": sorted(self._extra_allowed),
        }
        body["mac"] = self._mac(body)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(body, indent=2), encoding="utf-8")
        protect_file(temporary)
        temporary.replace(self.path)
        protect_file(self.path)

    def _mac(self, body: dict) -> str:
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def _quarantine(self, reason) -> None:
        bad = self.path.with_suffix(".json.bad")
        log.warning("access file unreadable (%s); moving to %s", reason, bad)
        try:
            self.path.replace(bad)
        except OSError:
            pass
