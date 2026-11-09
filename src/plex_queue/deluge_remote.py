from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from deluge_client import DelugeRPCClient

from plex_queue.config import Config
from plex_queue.models import Torrent, pick_for_removal
from plex_queue.place import is_complete, parse_finished
from plex_queue.sshutil import ssh_options
from plex_queue.vm import DelugeError, ensure_running

log = logging.getLogger(__name__)


class Deluge:
    def __init__(self, config: Config):
        self.config = config
        self._process: subprocess.Popen | None = None
        self._client: DelugeRPCClient | None = None
        self._lock = threading.Lock()
        self.boot_note = ""

    def close(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
        self._process = None
        self._client = None

    def list_torrents(self) -> list[Torrent]:
        status = self._call(
            "core.get_torrents_status",
            {},
            ["name", "state", "progress", "eta", "download_payload_rate", "hash"],
        )
        torrents = []
        for info_hash, fields in status.items():
            torrents.append(
                Torrent(
                    hash=_text(info_hash).lower(),
                    name=_text(fields.get("name") or fields.get(b"name")),
                    state=_text(fields.get("state") or fields.get(b"state")),
                    progress=float(fields.get("progress") or fields.get(b"progress") or 0),
                    eta=int(fields.get("eta") or fields.get(b"eta") or 0),
                    rate=int(
                        fields.get("download_payload_rate")
                        or fields.get(b"download_payload_rate")
                        or 0
                    ),
                )
            )
        torrents.sort(key=lambda item: item.name.lower())
        return torrents

    def add_magnet(self, magnet: str) -> None:
        options = {}
        if self.config.deluge_download_location:
            options["download_location"] = self.config.deluge_download_location
        self._call("core.add_torrent_magnet", magnet, options)

    def snapshot(self):
        # Light pass for every torrent, then pull file lists only for finished ones.
        light = self._call(
            "core.get_torrents_status",
            {},
            ["name", "state", "progress", "save_path", "hash"],
        )
        hashes: set[str] = set()
        finished_ids: list[str] = []
        for info_hash, fields in light.items():
            if not isinstance(fields, dict):
                continue
            digest = _text(fields.get("hash") or fields.get(b"hash") or info_hash).lower()
            hashes.add(digest)
            state = _text(fields.get("state") or fields.get(b"state"))
            progress = float(fields.get("progress") or fields.get(b"progress") or 0)
            if is_complete(state, progress):
                finished_ids.append(digest)
        if not finished_ids:
            return [], hashes
        rich = self._call(
            "core.get_torrents_status",
            {"id": finished_ids},
            ["name", "state", "progress", "save_path", "hash", "files"],
        )
        finished, _ = parse_finished(rich)
        return finished, hashes

    def remove_hash(self, info_hash: str) -> None:
        self._call("core.remove_torrent", info_hash.lower(), True)

    def remove(self, needle: str, delete_files: bool) -> str:
        torrents = self.list_torrents()
        chosen, error = pick_for_removal(torrents, needle)
        if error or chosen is None:
            return error or "Nothing in Deluge matches that."
        self._call("core.remove_torrent", chosen.hash, delete_files)
        kept = "Deleted the files." if delete_files else "Left the files on disk."
        return f"Removed {chosen.name}. {kept}"

    def _connect(self) -> None:
        self._ensure_tunnel()
        client = DelugeRPCClient(
            "127.0.0.1",
            self.config.ssh_local_port,
            self.config.deluge_username,
            self.config.deluge_password,
            decode_utf8=True,
        )
        client.connect()
        self._client = client

    def _call(self, method: str, *args):
        with self._lock:
            if self._client is None:
                self._connect()
            assert self._client is not None
            try:
                return self._client.call(method, *args)
            except Exception:
                log.warning("deluge call failed, reconnecting", exc_info=True)
                self._client = None
                self._connect()
                assert self._client is not None
                return self._client.call(method, *args)

    def _ensure_tunnel(self) -> None:
        status = ensure_running(self.config.vm_name, self.config.ssh_host)
        if status == "started":
            self.boot_note = f"Started {self.config.vm_name}."
        if self._process and self._process.poll() is None and _port_open(self.config.ssh_local_port):
            return
        if self._process and self._process.poll() is None:
            self._process.terminate()
        key = self.config.ssh_key
        if not key.exists():
            raise DelugeError(
                f"SSH key not found at {key}. Run scripts\\setup.ps1, then install the key on the VM."
            )
        ssh = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
        if not Path(ssh).exists():
            raise DelugeError("OpenSSH client is not installed on Windows.")
        command = [
            ssh, "-N",
            "-i", str(key),
            *ssh_options(self.config.ssh_known_hosts),
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-L",
            f"127.0.0.1:{self.config.ssh_local_port}:127.0.0.1:{self.config.ssh_remote_port}",
            f"{self.config.ssh_user}@{self.config.ssh_host}",
        ]
        log.info("opening SSH tunnel to %s", self.config.ssh_host)
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._process.poll() is not None:
                error = ""
                if self._process.stderr:
                    error = self._process.stderr.read().decode("utf-8", "replace").strip()
                # Keep raw SSH stderr out of Telegram; log it for the operator.
                if error:
                    log.warning("ssh tunnel stderr: %s", error[:300])
                raise DelugeError(
                    "SSH to the VM failed. Install the plex-queue key for ttt, then try again."
                )
            if _port_open(self.config.ssh_local_port):
                return
            time.sleep(0.2)
        raise DelugeError("SSH tunnel to Deluge did not open.")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")
