"""Windows subprocess helpers — keep console tools from flashing a black window."""

from __future__ import annotations

import os
import subprocess
from typing import Any


def no_window_kwargs() -> dict[str, Any]:
    """Extra kwargs so child console apps (powershell, ssh, icacls) stay hidden.

    Safe no-op on non-Windows. Callers merge into subprocess.run / Popen.
    """
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def run(*args: Any, **kwargs: Any):
    """subprocess.run with CREATE_NO_WINDOW on Windows."""
    return subprocess.run(*args, **{**no_window_kwargs(), **kwargs})


def popen(*args: Any, **kwargs: Any) -> subprocess.Popen:
    """subprocess.Popen with CREATE_NO_WINDOW on Windows."""
    return subprocess.Popen(*args, **{**no_window_kwargs(), **kwargs})
