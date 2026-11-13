from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from plex_queue.sshutil import ssh_options


class TransferError(RuntimeError):
    pass


_REMOTE_COPY = r"""
import os, sys
remote = sys.argv[1]
roots = sys.argv[2:]
if not roots:
    raise SystemExit(1)
path = os.path.realpath(remote)
if not any(path == root or path.startswith(root + os.sep) for root in roots):
    raise SystemExit(1)
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(path, flags)
try:
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
finally:
    os.close(fd)
""".strip()


def confined_cat_script(remote: str, allowed_roots: tuple[str, ...] | list[str]) -> str:
    """Resolve the remote path and stream it only if it stays under an allowed root.

    Uses realpath + O_NOFOLLOW so a symlink swap between check and open cannot
    redirect the read outside the download tree.
    """
    roots = [root.replace("\\", "/").rstrip("/") for root in allowed_roots if root]
    if not roots:
        raise TransferError("No download roots are configured for remote copies.")
    args = " ".join(shlex.quote(part) for part in (remote, *roots))
    return f"python3 -c {shlex.quote(_REMOTE_COPY)} {args}"


def _roots_or_fail(allowed_roots: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if allowed_roots is None:
        return ("/home/ttt/Downloads",)
    roots = tuple(root for root in allowed_roots if root)
    if not roots:
        raise TransferError("No download roots are configured for remote copies.")
    return roots


def cat_command(
    ssh: str,
    key: Path,
    user: str,
    host: str,
    remote: str,
    known_hosts: Path,
    allowed_roots: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    roots = _roots_or_fail(allowed_roots)
    return [
        ssh,
        "-i", str(key),
        *ssh_options(known_hosts),
        f"{user}@{host}",
        confined_cat_script(remote, roots),
    ]


class SshFetch:
    def __init__(
        self,
        key: Path,
        user: str,
        host: str,
        known_hosts: Path,
        ssh: str | None = None,
        run=None,
        download_roots: tuple[str, ...] | list[str] | None = None,
    ):
        self.key = key
        self.user = user
        self.host = host
        self.known_hosts = known_hosts
        self.ssh = ssh or shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
        if run is None:
            from plex_queue.winproc import run as hidden_run

            run = hidden_run
        self.run = run
        self.download_roots = _roots_or_fail(download_roots)

    def fetch(self, remote: str, local: Path, size: int) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        partial = local.with_name(local.name + ".part")
        command = cat_command(
            self.ssh, self.key, self.user, self.host, remote, self.known_hosts,
            self.download_roots,
        )
        try:
            with partial.open("wb") as handle:
                completed = self.run(command, stdout=handle, stderr=subprocess.PIPE, check=False)
            if completed.returncode != 0:
                detail = _stderr(completed)
                raise TransferError(_safe_remote_detail(detail) or "The copy from the VM failed.")
            copied = partial.stat().st_size
            if copied != size:
                raise TransferError(f"Copied {copied} bytes of {local.name}, expected {size}.")
            partial.replace(local)
        except Exception:
            partial.unlink(missing_ok=True)
            raise


def _stderr(completed) -> str:
    data = getattr(completed, "stderr", b"") or b""
    if isinstance(data, str):
        return data.strip()
    return data.decode("utf-8", "replace").strip()


def _safe_remote_detail(detail: str) -> str:
    text = (detail or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "permission denied" in lowered:
        return "SSH authentication failed."
    if "host key verification failed" in lowered or "remote host identification has changed" in lowered:
        return "SSH host key check failed."
    if "no such file" in lowered:
        return "The file was not found on the VM."
    return "The copy from the VM failed."
