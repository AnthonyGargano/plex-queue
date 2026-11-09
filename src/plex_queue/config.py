from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import yaml

from plex_queue.place import general_dirs

# Placeholder values allowed at load time so setup can finish editing config.yaml.
_SETUP_SECRET_PLACEHOLDERS = frozenset({"", "change-this-secret"})
_MIN_SETUP_SECRET_LEN = 16


@dataclass(frozen=True)
class Config:
    path: Path
    token: str
    setup_secret: str
    owner_id: int
    allowed_user_ids: tuple[int, ...]
    deluge_username: str
    deluge_password: str
    deluge_download_location: str
    ssh_host: str
    ssh_user: str
    ssh_key: Path
    ssh_local_port: int
    ssh_remote_port: int
    ssh_known_hosts: Path
    ssh_allow_tofu: bool
    min_height: int
    plex_db: Path | None
    roots: tuple[Path, ...]
    search_endpoint: str
    vm_name: str
    data_dir: Path
    movie_dirs: tuple[Path, ...]
    show_dirs: tuple[Path, ...]
    trash_dir: Path | None
    import_margin: int
    import_every: int
    tidy_auto: bool
    tidy_buckets: dict[str, tuple[Path, ...]]
    guest_hourly_limit: int
    guest_max_active: int

    @property
    def ready(self) -> bool:
        return not self.token.startswith("PUT_") and bool(self.token.strip())

    @property
    def setup_secret_ready(self) -> bool:
        return setup_secret_ok(self.setup_secret)


def setup_secret_ok(value: str) -> bool:
    text = str(value or "")
    if text in _SETUP_SECRET_PLACEHOLDERS:
        return False
    return len(text) >= _MIN_SETUP_SECRET_LEN


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else Path("config.yaml")
    if not path.exists():
        raise SystemExit(
            f"Missing {path}. Copy config.example.yaml to config.yaml and fill it in."
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    telegram = raw.get("telegram") or {}
    deluge = raw.get("deluge") or {}
    ssh = deluge.get("ssh") or {}
    library = raw.get("library") or {}
    search = raw.get("search") or {}
    vm = raw.get("vm") or {}
    incoming = raw.get("import") or {}
    tidy = raw.get("tidy") or {}
    root = path.parent
    plex = library.get("plex_db")
    roots = tuple(_expand(item) for item in library.get("roots") or [])
    every = max(1, int(incoming.get("every_minutes") or 10))
    margin_gb = float(incoming.get("margin_gb") or 2)
    movie_dirs = _dirs(incoming.get("movies"), roots, "movie")
    show_dirs = _dirs(incoming.get("shows"), roots, "show")
    from plex_queue.tidy import derive_buckets

    explicit = tidy.get("buckets") or None
    if explicit:
        explicit = {
            str(name): tuple(_expand(item) for item in (values or []))
            for name, values in explicit.items()
        }
    setup_secret = str(telegram.get("setup_secret") or "")
    if setup_secret not in _SETUP_SECRET_PLACEHOLDERS and not setup_secret_ok(setup_secret):
        raise SystemExit(
            f"telegram.setup_secret must be at least {_MIN_SETUP_SECRET_LEN} characters "
            "(or leave the placeholder until you set a real one)."
        )
    return Config(
        path=path,
        token=str(telegram.get("token") or ""),
        setup_secret=setup_secret,
        owner_id=int(telegram.get("owner_id") or 0),
        allowed_user_ids=tuple(int(item) for item in telegram.get("allowed_user_ids") or []),
        deluge_username=str(deluge.get("username") or "localclient"),
        deluge_password=str(deluge.get("password") or ""),
        deluge_download_location=str(deluge.get("download_location") or ""),
        ssh_host=str(ssh.get("host") or ""),
        ssh_user=str(ssh.get("user") or ""),
        ssh_key=_ssh_key(ssh.get("key_path")),
        ssh_local_port=int(ssh.get("local_port") or 15884),
        ssh_remote_port=int(ssh.get("remote_port") or 58846),
        ssh_known_hosts=_expand(str(ssh.get("known_hosts") or "")) if ssh.get("known_hosts") else root / "data" / "ssh_known_hosts",
        ssh_allow_tofu=bool(ssh.get("allow_tofu") or False),
        min_height=int(library.get("min_height") or 1080),
        plex_db=_expand(plex) if plex else None,
        roots=roots,
        search_endpoint=validate_search_endpoint(
            str(search.get("endpoint") or "https://apibay.org/q.php")
        ),
        vm_name=str(vm.get("name") or "UbuntuClean"),
        data_dir=root / "data",
        movie_dirs=movie_dirs,
        show_dirs=show_dirs,
        trash_dir=_expand(str(incoming["trash_dir"])) if incoming.get("trash_dir") else None,
        import_margin=int(margin_gb * 1024 ** 3),
        import_every=every * 60,
        tidy_auto=bool(tidy.get("auto") or False),
        tidy_buckets=derive_buckets(roots, movie_dirs, show_dirs, explicit),
        guest_hourly_limit=max(1, int(telegram.get("guest_hourly_limit") or 5)),
        guest_max_active=max(0, int(telegram.get("guest_max_active") or 3)),
    )


def validate_search_endpoint(endpoint: str, *, resolve: bool = True) -> str:
    """Require https and refuse private/special targets (SSRF via config).

    When ``resolve`` is true (default), DNS answers must also be public —
    hostnames that resolve only to RFC1918/link-local/etc. are rejected.
    """
    text = str(endpoint or "").strip()
    if not text:
        raise SystemExit("search.endpoint is empty.")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme.lower() != "https":
        raise SystemExit("search.endpoint must use https://")
    if parsed.username or parsed.password:
        raise SystemExit("search.endpoint must not include credentials.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SystemExit("search.endpoint is missing a host.")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost"):
        raise SystemExit(f"search.endpoint host is not allowed: {host}")
    if host.endswith(".local") or host.endswith(".internal"):
        raise SystemExit(f"search.endpoint host is not allowed: {host}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        _reject_special_ip(ip, host)
    elif resolve:
        _assert_public_dns(host)
    return text


def _reject_special_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, label: str) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise SystemExit(f"search.endpoint must not target a private or special IP: {label}")


def _assert_public_dns(host: str) -> None:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SystemExit(f"search.endpoint host could not be resolved: {host}") from exc
    if not answers:
        raise SystemExit(f"search.endpoint host could not be resolved: {host}")
    for entry in answers:
        address = entry[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        _reject_special_ip(ip, f"{host} -> {address}")


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def _ssh_key(value) -> Path:
    text = str(value or "").strip()
    if not text:
        raise SystemExit("Set deluge.ssh.key_path in config.yaml to the plex-queue private key.")
    path = _expand(text)
    if path == Path(".") or str(path) in {"", "."}:
        raise SystemExit("deluge.ssh.key_path cannot be empty.")
    return path


def download_roots(config: Config) -> tuple[str, ...]:
    location = (config.deluge_download_location or "").replace("\\", "/").rstrip("/")
    if location:
        return (location,)
    return ("/home/ttt/Downloads",)


def _dirs(values, roots, kind: str) -> tuple[Path, ...]:
    if values:
        return tuple(_expand(item) for item in values)
    return general_dirs(roots, kind)
