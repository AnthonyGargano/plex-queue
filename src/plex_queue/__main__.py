from __future__ import annotations

import argparse
import logging
import sys
import threading
from pathlib import Path

from plex_queue.access import Access
from plex_queue.bot import build_application
from plex_queue.config import download_roots, load_config
from plex_queue.deluge_remote import Deluge, DelugeError
from plex_queue.fetch import SshFetch
from plex_queue.importer import Importer, send_telegram
from plex_queue.ledger import Ledger
from plex_queue.library import Library
from plex_queue.plexscan import PlexScan, read_plex_token
from plex_queue.search import fetch_hits
from plex_queue.service import Service
from plex_queue.sshutil import ensure_known_host
from plex_queue.tidy_ledger import TidyLedger
from plex_queue.vm import ensure_running, guest_reachable


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="plex-queue")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "check", "tidy"),
        default="run",
    )
    parser.add_argument("tidy_args", nargs="*", help="tidy subcommand args")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)
    config = load_config(Path(args.config))
    if args.command == "check":
        check(config)
        return
    if args.command == "tidy":
        tidy_cli(config, args.tidy_args)
        return
    if not config.ready:
        raise SystemExit("Put the Telegram bot token in config.yaml first.")
    _boot(config)
    _pin_host(config)
    library = Library(config.plex_db, list(config.roots))
    search = _Search(config.search_endpoint)
    deluge = Deluge(config)
    access = Access(config)
    ledger = Ledger(config.data_dir / "imports.json")
    tidy_ledger = TidyLedger(config.data_dir / "tidy.json")
    plex = PlexScan(config.plex_db, read_plex_token)
    service = Service(
        library,
        search,
        deluge,
        config.min_height,
        access,
        ledger,
        roots=config.roots,
        trash_dir=config.trash_dir,
        tidy_ledger=tidy_ledger,
        plex=plex,
        tidy_buckets=config.tidy_buckets,
        import_margin=config.import_margin,
        plex_db=config.plex_db,
    )
    importer = Importer(
        deluge=deluge,
        ledger=ledger,
        movie_dirs=config.movie_dirs,
        show_dirs=config.show_dirs,
        margin=config.import_margin,
        transport=SshFetch(
            config.ssh_key, config.ssh_user, config.ssh_host, config.ssh_known_hosts,
            download_roots=download_roots(config),
        ),
        plex=plex,
        notify=lambda text: send_telegram(
            config.token, access.owner_id or config.owner_id, text,
        ),
        reachable=lambda: guest_reachable(config.ssh_host, 22),
        library=library,
        vm_name=config.vm_name,
        interval=config.import_every,
        download_roots=download_roots(config),
        plex_db=config.plex_db,
        min_height=config.min_height,
        library_roots=config.roots,
    )
    app = build_application(config, service, access, importer)
    stop = threading.Event()
    threading.Thread(target=importer.serve, args=(stop,), name="plex-queue-import", daemon=True).start()
    try:
        app.run_polling(drop_pending_updates=True)
    finally:
        stop.set()
        deluge.close()


def tidy_cli(config, argv: list[str] | None = None) -> None:
    """Owner-facing local tidy without Telegram: audit / apply / skip."""
    from plex_queue.access import Access as _Access

    argv = list(argv or [])
    library = Library(config.plex_db, list(config.roots))
    access = _Access(config)
    # CLI uses the configured owner id; require it so apply cannot run anonymously.
    owner = access.owner_id or config.owner_id
    if not owner:
        raise SystemExit("Set telegram.owner_id (or /claim) before using tidy apply.")
    ledger = Ledger(config.data_dir / "imports.json")
    tidy_ledger = TidyLedger(config.data_dir / "tidy.json")
    plex = PlexScan(config.plex_db, read_plex_token)

    class _NoDeluge:
        boot_note = ""

        def list_torrents(self):
            return []

    service = Service(
        library,
        _Search(config.search_endpoint),
        _NoDeluge(),
        config.min_height,
        access,
        ledger,
        roots=config.roots,
        trash_dir=config.trash_dir,
        tidy_ledger=tidy_ledger,
        plex=plex,
        tidy_buckets=config.tidy_buckets,
        import_margin=config.import_margin,
        plex_db=config.plex_db,
    )
    if not argv:
        print(service.tidy(owner, refresh=True))
        return
    verb = argv[0].lower()
    rest = " ".join(argv[1:]).strip()
    if verb in {"apply", "do"}:
        print(service.tidy_apply(rest or "", owner))
        return
    if verb in {"skip", "keep"}:
        if not rest:
            raise SystemExit("Usage: python -m plex_queue tidy skip <id>")
        print(service.tidy_skip(rest, owner))
        return
    if verb in {"refresh", "audit"}:
        print(service.tidy(owner, refresh=True))
        return
    raise SystemExit(
        "Usage: python -m plex_queue tidy\n"
        "       python -m plex_queue tidy apply <id|dupes|orphans>\n"
        "       python -m plex_queue tidy skip <id>"
    )


def check(config) -> None:
    _boot(config)
    _pin_host(config)
    library = Library(config.plex_db, list(config.roots))
    print("Reading Plex and the video drives...")
    library.refresh()
    for note in library.notes:
        print(note)
    print(f"Copies indexed: {len(library.copies)}")
    problems = []
    if not config.ready:
        problems.append("Telegram token is still the placeholder.")
    if not config.deluge_password or config.deluge_password.startswith("PUT_"):
        problems.append("Deluge password is still the placeholder.")
    if config.setup_secret in {"", "change-this-secret"}:
        problems.append("Choose a private telegram.setup_secret before starting the bot.")
    if not config.ssh_key.exists():
        problems.append(f"SSH key is missing: {config.ssh_key}")
    if problems:
        print("Deluge was not contacted:")
        for problem in problems:
            print(f"- {problem}")
        return
    deluge = Deluge(config)
    try:
        torrents = deluge.list_torrents()
    except DelugeError as exc:
        print(exc)
        sys.exit(1)
    finally:
        deluge.close()
    print(f"Deluge connected: {len(torrents)} torrents")


def _boot(config) -> None:
    logging.info("Checking %s", config.vm_name)
    try:
        status = ensure_running(config.vm_name, config.ssh_host)
    except DelugeError as exc:
        logging.error("%s", exc)
        print(exc)
        return
    message = f"Started {config.vm_name}." if status == "started" else f"{config.vm_name} is running."
    logging.info(message)
    print(message)


def _pin_host(config) -> None:
    if ensure_known_host(
        config.ssh_host,
        config.ssh_known_hosts,
        key=config.ssh_key,
        user=config.ssh_user,
    ):
        logging.info("SSH host key pinned in %s", config.ssh_known_hosts)
        return
    raise SystemExit(
        f"Could not pin the SSH host key for {config.ssh_host}. "
        f"Start the VM and confirm key login works, then restart the bot."
    )


class _Search:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def search(self, query: str, match_as: str | None = None):
        return fetch_hits(query, self.endpoint, match_as=match_as)


if __name__ == "__main__":
    main()
