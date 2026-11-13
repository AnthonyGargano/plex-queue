from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

HOST_LINE = re.compile(r"^[^\s|#]+")


def ssh_options(known_hosts: Path) -> list[str]:
    """Strict host-key checks against a dedicated known_hosts file."""
    empty = "NUL" if os.name == "nt" else "/dev/null"
    return [
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", f"GlobalKnownHostsFile={empty}",
    ]


def host_pinned(host: str, known_hosts: Path) -> bool:
    if not known_hosts.exists():
        return False
    needle = host.strip().lower()
    for line in known_hosts.read_text(encoding="utf-8").splitlines():
        match = HOST_LINE.match(line.strip())
        if not match:
            continue
        names = match.group(0).lower().split(",")
        if needle in names:
            return True
    return False


def ensure_known_host(
    host: str,
    known_hosts: Path,
    *,
    key: Path | None = None,
    user: str = "",
    allow_tofu: bool = False,
    keyscan: str | None = None,
    ssh: str | None = None,
    run=None,
) -> bool:
    """Pin the VM host key once. Later connects refuse a changed key.

    Prefers ssh-keyscan lines that name this host. The accept-new SSH fallback
    (TOFU) runs only when ``allow_tofu`` is true — otherwise the operator must
    pin via keyscan or a trusted manual known_hosts entry. Fingerprints are
    logged so the first pin can be verified out of band.
    """
    if run is None:
        from plex_queue.winproc import run as hidden_run

        run = hidden_run
    if not host.strip():
        return False
    if host_pinned(host, known_hosts):
        return True
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    if _keyscan_pin(host, known_hosts, keyscan=keyscan, run=run):
        _log_fingerprints(host, known_hosts, source="ssh-keyscan", run=run)
        return True
    if not allow_tofu:
        log.error(
            "SSH host key for %s is not pinned and deluge.ssh.allow_tofu is false. "
            "Run ssh-keyscan on a trusted network into %s, or set allow_tofu: true "
            "for a one-time accept-new pin you will verify.",
            host,
            known_hosts,
        )
        return False
    if key is None or not user:
        return False
    if not _ssh_pin(host, known_hosts, key=key, user=user, ssh=ssh, run=run):
        return False
    _log_fingerprints(host, known_hosts, source="accept-new TOFU", run=run)
    return True


def _line_for_host(line: str, host: str) -> bool:
    match = HOST_LINE.match(line.strip())
    if not match:
        return False
    needle = host.strip().lower()
    return needle in match.group(0).lower().split(",")


def _keyscan_pin(host: str, known_hosts: Path, *, keyscan: str | None, run) -> bool:
    scanner = keyscan or shutil.which("ssh-keyscan") or _default_keyscan()
    if Path(scanner).is_absolute() and not Path(scanner).exists():
        return False
    completed = run(
        [scanner, "-T", "5", "-t", "ed25519,rsa,ecdsa", host],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [
        line.strip()
        for line in (completed.stdout or "").splitlines()
        if line.strip()
        and not line.lstrip().startswith("#")
        and _line_for_host(line, host)
    ]
    if not lines:
        return False
    _append_lines(known_hosts, lines)
    return host_pinned(host, known_hosts)


def _ssh_pin(
    host: str,
    known_hosts: Path,
    *,
    key: Path,
    user: str,
    ssh: str | None,
    run,
) -> bool:
    client = ssh or shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
    empty = "NUL" if os.name == "nt" else "/dev/null"
    command = [
        client,
        "-i", str(key),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", f"GlobalKnownHostsFile={empty}",
        f"{user}@{host}",
        "true",
    ]
    completed = run(command, capture_output=True, text=True, check=False)
    return completed.returncode == 0 and host_pinned(host, known_hosts)


def _log_fingerprints(host: str, known_hosts: Path, *, source: str, run) -> None:
    prints = host_fingerprints(host, known_hosts, run=run)
    if prints:
        log.warning(
            "Pinned SSH host key for %s via %s. Verify fingerprint(s): %s",
            host, source, "; ".join(prints),
        )
    else:
        log.warning(
            "Pinned SSH host key for %s via %s. Confirm data/ssh_known_hosts on a trusted network.",
            host, source,
        )


def host_fingerprints(host: str, known_hosts: Path, run=None) -> list[str]:
    if run is None:
        from plex_queue.winproc import run as hidden_run

        run = hidden_run
    if not known_hosts.exists():
        return []
    keygen = shutil.which("ssh-keygen") or _default_keygen()
    if Path(keygen).is_absolute() and not Path(keygen).exists():
        return []
    completed = run(
        [keygen, "-l", "-f", str(known_hosts)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    needle = host.strip().lower()
    found = []
    for line in (completed.stdout or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if needle and needle not in text.lower():
            # ssh-keygen -l lines usually include the host comment; keep unmatched as fallback.
            continue
        found.append(text)
    if found:
        return found
    return [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]


def _append_lines(known_hosts: Path, lines: list[str]) -> None:
    existing = known_hosts.read_text(encoding="utf-8") if known_hosts.exists() else ""
    with known_hosts.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        for line in lines:
            handle.write(line + "\n")


def _default_keyscan() -> str:
    return r"C:\Windows\System32\OpenSSH\ssh-keyscan.exe"


def _default_keygen() -> str:
    return r"C:\Windows\System32\OpenSSH\ssh-keygen.exe"
