from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from plex_queue.place import general_dirs


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

    @property
    def ready(self) -> bool:
        return not self.token.startswith("PUT_") and bool(self.token.strip())


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
    return Config(
        path=path,
        token=str(telegram.get("token") or ""),
        setup_secret=str(telegram.get("setup_secret") or ""),
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
        min_height=int(library.get("min_height") or 1080),
        plex_db=_expand(plex) if plex else None,
        roots=roots,
        search_endpoint=str(search.get("endpoint") or "https://apibay.org/q.php"),
        vm_name=str(vm.get("name") or "UbuntuClean"),
        data_dir=root / "data",
        movie_dirs=movie_dirs,
        show_dirs=show_dirs,
        trash_dir=_expand(str(incoming["trash_dir"])) if incoming.get("trash_dir") else None,
        import_margin=int(margin_gb * 1024 ** 3),
        import_every=every * 60,
        tidy_auto=bool(tidy.get("auto") or False),
        tidy_buckets=derive_buckets(roots, movie_dirs, show_dirs, explicit),
    )


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
