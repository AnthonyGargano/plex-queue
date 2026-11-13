from __future__ import annotations

import sqlite3
from pathlib import Path

from plex_queue.access import Access
from plex_queue.config import Config
from plex_queue.decide import decide
from plex_queue.importer import Importer
from plex_queue.ledger import Ledger
from plex_queue.library import Library
from plex_queue.models import Copy, Hit
from plex_queue.place import Label
from plex_queue.service import Service
from plex_queue.place import CopyJob
from plex_queue.upgrades import move_to_trash, plex_new_is_best, trash_path, video_locals


def test_video_locals_keeps_every_episode_in_a_season_pack(tmp_path):
    jobs = [
        CopyJob("/r/e1.mkv", tmp_path / "S01E01.mkv", 100),
        CopyJob("/r/e2.mkv", tmp_path / "S01E02.mkv", 200),
        CopyJob("/r/e1.srt", tmp_path / "S01E01.srt", 10),
        CopyJob("/r/sample.mkv", tmp_path / "S01E01.sample.mkv", 50),
    ]
    paths = video_locals(jobs)
    assert paths == [str(tmp_path / "S01E02.mkv"), str(tmp_path / "S01E01.mkv")]


def test_ledger_stores_replace_path(tmp_path):
    ledger = Ledger(tmp_path / "imports.json")
    digest = "ab" * 20
    ledger.remember(
        digest,
        Label("movie", "The Bourne Identity", 2002),
        replace_path=str(tmp_path / "old.mkv"),
        replace_height=384,
    )
    state = ledger.state(digest)
    assert state["replace_path"].endswith("old.mkv")
    assert state["replace_height"] == 384
    assert state["upgrade_status"] == "awaiting_import"


def test_plex_new_is_best_requires_new_file_at_min_height(tmp_path):
    db = tmp_path / "plex.db"
    old = str(tmp_path / "old.mkv")
    new = str(tmp_path / "new.mkv")
    _plex_versions(db, [
        (1, old, 900, 384),
        (1, new, 1920, 800),
    ])
    assert plex_new_is_best(db, [new], 1080, replace_path=old, replace_height=384) is True
    assert plex_new_is_best(db, [old], 1080, replace_path=old, replace_height=384) is False


def test_plex_new_is_best_requires_same_metadata_item(tmp_path):
    db = tmp_path / "plex.db"
    old = str(tmp_path / "old.mkv")
    other = str(tmp_path / "other.mkv")
    _plex_versions(db, [
        (1, old, 900, 384),
        (2, other, 1920, 800),
    ])
    assert plex_new_is_best(db, [other], 1080, replace_path=old, replace_height=384) is False


def test_plex_new_is_best_allows_upgrade_when_higher_version_already_exists(tmp_path):
    db = tmp_path / "plex.db"
    old = str(tmp_path / "old.mkv")
    mid = str(tmp_path / "mid.mkv")
    uhd = str(tmp_path / "uhd.mkv")
    _plex_versions(db, [
        (1, old, 900, 384),
        (1, mid, 1920, 800),
        (1, uhd, 3840, 1600),
    ])
    assert plex_new_is_best(db, [mid], 1080, replace_path=old, replace_height=384) is True


def test_plex_new_is_best_rejects_equal_height_duplicate(tmp_path):
    db = tmp_path / "plex.db"
    old = str(tmp_path / "old.mkv")
    twin = str(tmp_path / "twin.mkv")
    _plex_versions(db, [
        (1, old, 1920, 800),
        (1, twin, 1920, 800),
    ])
    assert plex_new_is_best(db, [twin], 1080, replace_path=old, replace_height=1080) is False


def test_plex_new_is_best_waits_until_new_file_is_indexed(tmp_path):
    db = tmp_path / "plex.db"
    old = str(tmp_path / "old.mkv")
    new = str(tmp_path / "new.mkv")
    _plex_versions(db, [(1, old, 900, 384)])
    assert plex_new_is_best(db, [new], 1080, replace_path=old, replace_height=384) is False


def test_request_remembers_replace_path_for_upgrades(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    old = movies / "The Bourne Identity (2002).mkv"
    old.write_bytes(b"sd")
    library = Library(None, [movies])
    library.loaded = True
    library.copies = [
        Copy("movie", "The Bourne Identity", 2002, None, None, None, 384, str(old), ("bourne", "identity")),
    ]
    ledger = Ledger(tmp_path / "imports.json")
    digest = "cd" * 20

    class Deluge:
        def list_torrents(self):
            return []

        def add_magnet(self, magnet):
            self.magnet = magnet

    class Search:
        def search(self, query, match_as=None):
            return [Hit("The.Bourne.Identity.2002.1080p.BluRay", digest, 20, 1)]

    service = Service(
        library, Search(), Deluge(), ledger=ledger, roots=[movies],
    )
    text = service.request("The Bourne Identity 2002", kind="movie")
    assert "upgrade" in text.lower() or "384" in text
    state = ledger.state(digest)
    assert state["replace_path"] == str(old)
    assert state["replace_height"] == 384


def test_promote_upgrade_when_plex_confirms(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    old = movies / "old.mkv"
    new = movies / "new.mkv"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(old), 900, 384),
        (1, str(new), 1920, 800),
    ])
    digest = "ef" * 20
    ledger = Ledger(tmp_path / "imports.json")
    ledger.remember(
        digest,
        Label("movie", "Example", 2019),
        replace_path=str(old),
        replace_height=384,
    )
    ledger.mark(
        digest,
        status="imported",
        dest=str(movies),
        scanned=True,
        new_paths=[str(new)],
        upgrade_status="awaiting_plex",
    )
    importer = Importer(
        deluge=_EmptyDeluge(),
        ledger=ledger,
        movie_dirs=[movies],
        show_dirs=[movies],
        margin=0,
        transport=None,
        plex=_NoPlex(),
        notify=lambda text: None,
        reachable=lambda: True,
        plex_db=db,
        min_height=1080,
        library_roots=[movies],
    )
    notes = importer.cycle(notify=False)
    assert ledger.state(digest)["upgrade_status"] == "pending"
    assert any("Upgrade ready for review" in note for note in notes)


def test_idle_cycle_stays_quiet_while_upgrades_are_pending(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    digest = "ab" * 20
    ledger = Ledger(tmp_path / "imports.json")
    ledger.remember(digest, Label("movie", "Example", 2019))
    ledger.mark(
        digest,
        status="imported",
        dest=str(movies),
        scanned=True,
        upgrade_status="pending",
        new_height=1080,
    )
    notes_sent = []
    importer = Importer(
        deluge=_EmptyDeluge(),
        ledger=ledger,
        movie_dirs=[movies],
        show_dirs=[movies],
        margin=0,
        transport=None,
        plex=_NoPlex(),
        notify=notes_sent.append,
        reachable=lambda: True,
        plex_db=None,
        min_height=1080,
        library_roots=[movies],
    )
    first = importer.cycle(notify=True)
    second = importer.cycle(notify=True)
    assert first == []
    assert second == []
    assert notes_sent == []
    assert ledger.state(digest)["upgrade_status"] == "pending"


def test_drop_moves_old_file_to_trash(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    trash = tmp_path / "trash"
    movies.mkdir()
    trash.mkdir()
    old = movies / "Example (2019).mkv"
    old.write_bytes(b"sd")
    digest = "11" * 20
    ledger = Ledger(tmp_path / "imports.json")
    ledger.remember(
        digest,
        Label("movie", "Example", 2019),
        replace_path=str(old),
        replace_height=384,
    )
    ledger.mark(digest, upgrade_status="pending", new_height=1080)
    access = Access(_config(tmp_path, owner_id=7))
    service = Service(
        Library(None, [movies]),
        None,
        None,
        access=access,
        ledger=ledger,
        roots=[movies],
        trash_dir=trash,
    )

    def fake_recycle(path):
        # Keep the unit test out of the real Recycle Bin on Windows.
        target = trash / path.name
        path.replace(target)

    monkeypatch.setattr("plex_queue.upgrades._send_to_recycle_bin", fake_recycle)
    assert "Only the owner" in service.drop_upgrade(digest[:6], user_id=99)
    text = service.drop_upgrade(digest[:6], user_id=7)
    assert "Moved old copy" in text
    assert not old.exists()
    assert ledger.state(digest)["upgrade_status"] == "trashed"
    assert list(trash.iterdir())


def test_keep_clears_pending_without_moving(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    old = movies / "Example (2019).mkv"
    old.write_bytes(b"sd")
    digest = "22" * 20
    ledger = Ledger(tmp_path / "imports.json")
    ledger.remember(
        digest,
        Label("movie", "Example", 2019),
        replace_path=str(old),
        replace_height=480,
    )
    ledger.mark(digest, upgrade_status="pending")
    access = Access(_config(tmp_path, owner_id=7))
    service = Service(
        Library(None, [movies]),
        None,
        None,
        access=access,
        ledger=ledger,
        roots=[movies],
        trash_dir=tmp_path / "trash",
    )
    text = service.keep_upgrade(digest[:6], user_id=7)
    assert "Kept" in text
    assert old.exists()
    assert ledger.state(digest)["upgrade_status"] == "kept"


def test_trash_path_stays_inside_trash_dir(tmp_path):
    trash = tmp_path / "trash"
    source = tmp_path / "Movies" / "Example.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x")
    dest = trash_path(source, trash, title="Example", year=2019)
    assert dest.parent == trash
    moved = move_to_trash(
        source, trash, [tmp_path / "Movies"], title="Example", year=2019, recycle=False,
    )
    assert moved.parent.resolve() == trash.resolve()
    assert not source.exists()


def test_move_to_trash_can_use_recycle_bin(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    movies.mkdir()
    source = movies / "Example.mkv"
    source.write_bytes(b"x")
    calls = []

    def fake_recycle(path):
        calls.append(path)
        path.unlink()

    monkeypatch.setattr("plex_queue.upgrades._send_to_recycle_bin", fake_recycle)
    dest = move_to_trash(source, None, [movies], recycle=True)
    assert dest.name == "(Recycle Bin)"
    assert calls and calls[0] == source
    assert not source.exists()


def test_move_to_trash_refuses_symlinks(tmp_path, monkeypatch):
    movies = tmp_path / "Movies"
    movies.mkdir()
    target = tmp_path / "secret.mkv"
    target.write_bytes(b"secret")
    link = movies / "Example.mkv"
    try:
        link.symlink_to(target)
    except OSError:
        # Windows may require Developer Mode / admin for file symlinks.
        return
    calls = []
    monkeypatch.setattr(
        "plex_queue.upgrades._send_to_recycle_bin",
        lambda path: calls.append(path),
    )
    try:
        move_to_trash(link, None, [movies], recycle=True)
        assert False, "expected OSError for symlink"
    except OSError as exc:
        assert "symlink" in str(exc).casefold()
    assert calls == []
    assert target.exists()


def test_decide_marks_replacing_copy():
    old = Copy("movie", "Example", 2019, None, None, None, 480, "old.mkv", ("example",))
    hit = Hit("Example.2019.1080p", "ab" * 20, 5, 1)
    decision = decide([old], [hit], 1080)
    assert decision.replacing is old


def _plex_versions(db: Path, rows: list[tuple[int, str, int, int]]) -> None:
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata_items (
                id INTEGER PRIMARY KEY, deleted_at INTEGER
            );
            CREATE TABLE media_items (
                id INTEGER PRIMARY KEY, metadata_item_id INTEGER,
                width INTEGER, height INTEGER, deleted_at INTEGER
            );
            CREATE TABLE media_parts (
                id INTEGER PRIMARY KEY, media_item_id INTEGER,
                file TEXT, deleted_at INTEGER
            );
            """
        )
        meta_ids = sorted({row[0] for row in rows})
        for meta_id in meta_ids:
            connection.execute(
                "INSERT INTO metadata_items (id, deleted_at) VALUES (?, NULL)",
                (meta_id,),
            )
        for index, (meta_id, file_path, width, height) in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO media_items (id, metadata_item_id, width, height, deleted_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (index, meta_id, width, height),
            )
            connection.execute(
                "INSERT INTO media_parts (id, media_item_id, file, deleted_at) "
                "VALUES (?, ?, ?, NULL)",
                (index, index, file_path),
            )
        connection.commit()
    finally:
        connection.close()


class _EmptyDeluge:
    def snapshot(self):
        return [], set()

    def remove_hash(self, info_hash):
        return None

    def list_torrents(self):
        return []


class _NoPlex:
    def refresh_dirs(self, directories):
        return [], []


def _config(tmp_path: Path, owner_id: int = 0) -> Config:
    return Config(
        path=tmp_path / "config.yaml",
        token="token",
        setup_secret="secret-secret-16",
        owner_id=owner_id,
        allowed_user_ids=(),
        deluge_username="localclient",
        deluge_password="pw",
        deluge_download_location="",
        ssh_host="127.0.0.1",
        ssh_user="ttt",
        ssh_key=tmp_path / "key",
        ssh_local_port=15884,
        ssh_remote_port=58846,
        ssh_known_hosts=tmp_path / "known_hosts",
        ssh_allow_tofu=False,
        min_height=1080,
        plex_db=None,
        roots=(),
        search_endpoint="https://example.test/q.php",
        vm_name="UbuntuClean",
        data_dir=tmp_path / "data",
        movie_dirs=(),
        show_dirs=(),
        trash_dir=None,
        import_margin=0,
        import_every=600,
        tidy_auto=False,
        tidy_buckets={},
        guest_hourly_limit=5,
        guest_max_active=3,
        otel_enabled=False,
        otel_endpoint="http://127.0.0.1:4317",
        otel_service_name="plex-queue",
    )
