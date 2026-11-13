"""Shared denylist for secrets that must not appear in logs or OTEL fields."""

from __future__ import annotations

# Exact key names (matched case-insensitively).
SENSITIVE_KEYS = frozenset(
    {
        "token",
        "password",
        "passwd",
        "secret",
        "setup_secret",
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "deluge_password",
        "plex_token",
        "access_token",
        "auth_token",
        "refresh_token",
        "private_key",
        "credential",
        "credentials",
        "bearer",
    }
)

# Substrings that mark a key as sensitive (with benign exceptions).
_SENSITIVE_SUBSTR = ("token", "secret", "password", "passwd", "credential", "apikey")
_BENIGN_KEYS = frozenset(
    {
        "action_id",
        "torrent_id",
        "user_id",
        "section_ids",
        "kind",
        "kinds",
        "status",
        "event",
        "service",
        "ts",
        "duration_ms",
        "pending",
        "counts",
        "notes",
        "selector",
        "confidence",
        "error",
        "reason",
        "recycle",
        "requested",
        "ok",
        "failed",
        "count",
        "sections",
        "operation",
    }
)


def is_sensitive_key(key: str) -> bool:
    name = str(key or "").strip().lower()
    if not name or name in _BENIGN_KEYS:
        return False
    if name in SENSITIVE_KEYS:
        return True
    return any(part in name for part in _SENSITIVE_SUBSTR)
