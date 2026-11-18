from __future__ import annotations

import ipaddress
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

# Hyper-V Default Switch hands out a new DHCP address after the host reboots.
# Reported guest IPs are often empty, so also match the VM NIC MAC in the
# host neighbor table. The name is passed only through PLEX_QUEUE_VM.
DISCOVER_SCRIPT = r"""
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'
Import-Module Hyper-V -DisableNameChecking | Out-Null
$adapters = @(Get-VMNetworkAdapter -VMName $env:PLEX_QUEUE_VM)
foreach ($adapter in $adapters) {
  foreach ($ip in @($adapter.IPAddresses)) {
    if ($ip -match '^\d{1,3}(\.\d{1,3}){3}$') { Write-Output $ip }
  }
  $hex = ([string]$adapter.MacAddress) -replace '[^0-9A-Fa-f]',''
  if ($hex.Length -ne 12) { continue }
  $pairs = for ($i = 0; $i -lt 12; $i += 2) { $hex.Substring($i, 2) }
  $mac = ($pairs -join '-').ToUpper()
  $neighbors = @(Get-NetNeighbor -LinkLayerAddress $mac -AddressFamily IPv4 -ErrorAction SilentlyContinue)
  foreach ($neighbor in $neighbors) {
    $state = $neighbor.State.ToString()
    if ($state -in 'Reachable','Permanent','Stale') { Write-Output $neighbor.IPAddress }
  }
}
"""


class GuestAddress:
    """Configured SSH host, plus the address that actually answered."""

    def __init__(self, configured: str):
        self.configured = (configured or "").strip()
        self.current = self.configured


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


def usable_guest_ipv4(value: str) -> bool:
    """True for a unicast IPv4 address we can safely pass to SSH."""
    try:
        ip = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return False
    if not isinstance(ip, ipaddress.IPv4Address):
        return False
    if (
        ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
    ):
        return False
    return bool(ip.is_private or ip.is_global)


def parse_guest_ips(lines: list[str]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in lines:
        text = line.strip()
        if not usable_guest_ipv4(text) or text in seen:
            continue
        seen.add(text)
        found.append(text)
    return found


def discover_guest_ips(name: str) -> list[str]:
    """IPv4 addresses Hyper-V currently associates with this VM."""
    if not NAME.fullmatch(name or ""):
        return []
    try:
        lines = powershell_lines(DISCOVER_SCRIPT, name)
    except DelugeError:
        log.warning("Could not list guest addresses for %s", name)
        return []
    return parse_guest_ips(lines)


def guest_answering(
    name: str,
    host: str,
    port: int = 22,
    reachable=guest_reachable,
    discover=discover_guest_ips,
) -> bool:
    """True when the saved host or a current guest address accepts TCP."""
    return _first_answering(name, host, port, reachable, discover) is not None


def _first_answering(name, host, port, reachable, discover) -> str | None:
    if host and reachable(host, port):
        return host
    try:
        candidates = discover(name) if discover else []
    except DelugeError:
        log.warning("Could not list guest addresses for %s", name)
        candidates = []
    for ip in candidates:
        if not usable_guest_ipv4(ip) or ip == host:
            continue
        if reachable(ip, port):
            return ip
    return None


def _remember(address: GuestAddress | None, found: str) -> None:
    if address is not None and found:
        address.current = found


def ensure_running(
    name: str,
    host: str,
    port: int = 22,
    timeout: int = 180,
    control: HyperV | None = None,
    reachable=guest_reachable,
    sleep=time.sleep,
    clock=time.time,
    discover=discover_guest_ips,
    address: GuestAddress | None = None,
) -> str:
    """Return 'running' or 'started'. Start the Hyper-V VM when it is not up.

    The saved host is tried first. If it does not answer, a current guest
    address from Hyper-V is used instead (Default Switch DHCP changes).
    """
    if not NAME.fullmatch(name or ""):
        raise DelugeError(f"VM name is not valid: {name!r}")
    found = _first_answering(name, host, port, reachable, discover)
    if found:
        if found != host:
            log.info("%s answered on %s; saved address %s did not", name, found, host)
        _remember(address, found)
        return "running"
    hyperv = control or HyperV(name)
    booted = False
    deadline = clock() + timeout
    while clock() < deadline:
        found = _first_answering(name, host, port, reachable, discover)
        if found:
            if found != host:
                log.info("%s answered on %s; saved address %s did not", name, found, host)
            _remember(address, found)
            return "started" if booted else "running"
        if not booted:
            with _lock:
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
    lines = powershell_lines(script, vm_name)
    return lines[-1] if lines else ""


def powershell_lines(script: str, vm_name: str) -> list[str]:
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
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
