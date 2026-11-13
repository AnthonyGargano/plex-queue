from __future__ import annotations

import os
import subprocess

from plex_queue.winproc import no_window_kwargs


def test_no_window_kwargs_on_windows():
    kwargs = no_window_kwargs()
    if os.name == "nt":
        assert kwargs.get("creationflags") == subprocess.CREATE_NO_WINDOW
    else:
        assert kwargs == {}
