from __future__ import annotations

import re

_SENSITIVE_PATH = re.compile(
    r"(?i)(?:"
    r"/home/[^\s\"']+"
    r"|/Users/[^\s\"']+"
    r"|[A-Za-z]:\\[^\s\"']+"
    r"|\\\\[^\s\"']+"
    r"|\.ssh/[^\s\"']+"
    r")"
)


def public_error(exc: BaseException | str) -> str:
    """User-facing error text without SSH path or host-key detail."""
    text = (exc if isinstance(exc, str) else str(exc)).strip() or (
        type(exc).__name__ if not isinstance(exc, str) else "error"
    )
    lowered = text.lower()
    if "permission denied" in lowered:
        return "SSH authentication failed."
    if "host key verification failed" in lowered or "remote host identification has changed" in lowered:
        return "SSH host key check failed."
    if "connection refused" in lowered or "timed out" in lowered or "network is unreachable" in lowered:
        return "Could not reach the VM over SSH."
    cleaned = _SENSITIVE_PATH.sub("[path]", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:180] or "Something went wrong."
