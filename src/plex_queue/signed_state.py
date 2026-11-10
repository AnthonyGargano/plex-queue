"""HMAC helpers for JSON state files under data/."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from pathlib import Path

from plex_queue.config import setup_secret_ok
from plex_queue.securefs import protect_dir, protect_file

log = logging.getLogger(__name__)


def state_mac(secret: str, body: dict) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def read_signed_json(path: Path, secret: str, *, label: str) -> dict:
    """Load JSON object; quarantine and return {} when MAC is required but invalid.

    When ``setup_secret`` is production-ready, a missing/invalid MAC quarantines
    the file. During setup (placeholder secret), unsigned files still load so
    local tooling can run before a secret is chosen.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _quarantine(path, f"{label} unreadable ({exc})")
        return {}
    if not isinstance(raw, dict):
        _quarantine(path, f"{label} root is not an object")
        return {}
    body = {key: value for key, value in raw.items() if key != "mac"}
    if not setup_secret_ok(secret):
        # Setup/dev only: bot and tidy CLI refuse to start without a ready secret,
        # so production never serves this branch. Tests may use an empty secret.
        return body
    mac = str(raw.get("mac") or "")
    if not mac:
        # Do not auto-trust unsigned bodies — an attacker who can write data/
        # could plant replace_path / tidy sources and wait for a silent resign.
        # Restore from a known-good backup, or delete the .bad file and rebuild.
        _quarantine(path, f"{label} unsigned (missing MAC)")
        return {}
    expected = state_mac(secret, body)
    if not hmac.compare_digest(mac, expected):
        _quarantine(path, f"{label} MAC invalid")
        return {}
    return body


def write_signed_json(path: Path, data: dict, secret: str) -> None:
    """Atomically write JSON; attach MAC when setup_secret is ready."""
    body = {key: value for key, value in data.items() if key != "mac"}
    if setup_secret_ok(secret):
        body = dict(body)
        body["mac"] = state_mac(secret, body)
    protect_dir(path.parent)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(body, indent=2), encoding="utf-8")
    protect_file(temporary)
    temporary.replace(path)
    protect_file(path)


def _quarantine(path: Path, reason: str) -> None:
    bad = path.with_suffix(path.suffix + ".bad")
    log.warning("%s; moving to %s", reason, bad)
    try:
        path.replace(bad)
    except OSError:
        pass
