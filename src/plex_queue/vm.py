from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import threading
import time

class DelugeError(RuntimeError):
    pass


log = logging.getLogger(__name__)
_lock = threading.Lock()

NAME = re.compile(r"[A-Za-z0-9 _.-]+")
START_STATES = {
    "Off", "Saved", "FastSaved", "OffCritical", "SavedCritical", "FastSavedCritical",
}
RESUME_STATES = {"Paused", "PausedCritical"}

STATE_SCRIPT = r"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
Import-Module Hyper-V -DisableNameChecking | Out-Null
(Get-VM -Name $env:PLEX_QUEUE_VM).State.ToString()
"""

START_SCRIPT = r"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
Import-Module Hyper-V -DisableNameChecking | Out-Null
Start-VM -Name $env:PLEX_QUEUE_VM
"""

RESUME_SCRIPT = r"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
Import-Module Hyper-V -DisableNameChecking | Out-Null
Resume-VM -Name $env:PLEX_QUEUE_VM
"""


class HyperV:
    def __init__(self, name: str):
        self.name = name

    def state(self) -> str:
        return powershell(STATE_SCRIPT, self.name)

    def start(self) -> None:
        powershell(START_SCRIPT, self.name)

    def resume(self) -> None:
        powershell(RESUME_SCRIPT, self.name)


def guest_reachable(host: str, port: int = 22, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_running(
    name: str,
    host: str,
    port: int = 22,
    timeout: int = 180,
    control: HyperV | None = None,
    reachable=guest_reachable,
    sleep=time.sleep,
    clock=time.time,
) -> str:
    """Return 'running' or 'started'. Start the Hyper-V VM when it is not up."""
    if not NAME.fullmatch(name or ""):
        raise DelugeError(f"VM name is not valid: {name!r}")
    with _lock:
        if reachable(host, port):
            return "running"
        hyperv = control or HyperV(name)
        booted = False
        deadline = clock() + timeout
        while clock() < deadline:
            if reachable(host, port):
                return "started" if booted else "running"
            if not booted:
                state = hyperv.state()
                if state in RESUME_STATES:
                    log.info("Resuming %s", name)
                    hyperv.resume()
                    booted = True
                elif state not in {"Running", "RunningCritical", "Starting", "Resuming"}:
                    if state in START_STATES or state not in {
                        "Stopping", "Saving", "Pausing", "Reset", "ForceShutdown", "ForceReboot",
                    }:
                        log.info("Starting %s from %s", name, state)
                        hyperv.start()
                        booted = True
            sleep(2)
    raise DelugeError(
        f"{name} did not answer on {host}:{port}. "
        "It may still be booting, or its address changed."
    )


def powershell(script: str, vm_name: str) -> str:
    from plex_queue.winproc import no_window_kwargs

    env = os.environ.copy()
    env["PLEX_QUEUE_VM"] = vm_name
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        **no_window_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if detail:
            log.warning("Hyper-V command failed for %s: %s", vm_name, detail[:300])
        raise DelugeError(f"Hyper-V command failed for {vm_name}.")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return lines[-1] if lines else ""
