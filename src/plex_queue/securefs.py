"""Restrict permissions on state under data/ (POSIX mode + Windows ACL)."""

from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def protect_file(path: Path) -> None:
    """Restrict a state file to the current user when the OS allows it."""
    if not path.exists():
        return
    mode = 0o700 if path.is_dir() else 0o600
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log.warning("chmod %s failed for %s: %s", oct(mode), path, exc)
    if os.name == "nt":
        _windows_restrict(path)


def protect_dir(path: Path) -> None:
    """Create ``path`` if needed and restrict the directory itself."""
    path.mkdir(parents=True, exist_ok=True)
    protect_file(path)


def _windows_restrict(path: Path) -> None:
    """Drop inherited ACLs and grant full control to the current user only."""
    user = _windows_user()
    if not user or not path.exists():
        return
    # (OI)(CI) for directories so new children inherit the tightened ACL.
    rights = "(OI)(CI)(F)" if path.is_dir() else "(F)"
    try:
        completed = subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:{rights}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        log.warning("icacls failed for %s: %s", path, exc)
        return
    if completed.returncode != 0:
        log.warning(
            "icacls returned %s for %s: %s",
            completed.returncode,
            path,
            (completed.stderr or completed.stdout or "").strip(),
        )


def _windows_user() -> str:
    """Prefer DOMAIN\\user so icacls targets the correct principal."""
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        return ""
    domain = (os.environ.get("USERDOMAIN") or "").strip()
    if domain and domain.upper() not in {user.upper(), "WORKGROUP"}:
        return f"{domain}\\{user}"
    return user


def is_world_readable(path: Path) -> bool:
    """True when others can read the path (POSIX check; always False on Windows)."""
    if os.name == "nt" or not path.exists():
        return False
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH))
