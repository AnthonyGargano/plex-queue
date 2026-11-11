"""Regression tests for corporate-strict hardening controls."""

from __future__ import annotations

from pathlib import Path

from plex_queue.library import Library
from plex_queue.models import Hit, Torrent
from plex_queue.service import Service
from plex_queue.tidy import apply_action
from plex_queue.upgrades import path_key


class _Search:
    def __init__(self, hits: list[Hit]):
        self.hits = hits

    def search(self, query, match_as=None):
        return list(self.hits)


class _Deluge:
    def __init__(self, torrents=None):
        self.torrents = list(torrents or [])
        self.added = []

    def list_torrents(self):
        return list(self.torrents)

    def add_magnet(self, magnet):
        self.added.append(magnet)
        digest = magnet.split("btih:")[1].split("&")[0]
        self.torrents.append(Torrent(digest, "queued", "Downloading", 0.0, 0, 0))


def test_guest_max_active_blocks_add_magnet(tmp_path):
    library = Library(None, [])
    active = Torrent("a" * 40, "Busy", "Downloading", 10.0, 0, 0)
    deluge = _Deluge([active, active, active])
    hit = Hit("Example.Story.2019.1080p", "b" * 40, 20, 1)
    service = Service(
        library,
        _Search([hit]),
        deluge,
        guest_max_active=3,
    )
    text = service.request("Example Story", kind="movie", as_guest=True)
    assert "guest limit" in text.lower()
    assert deluge.added == []


def test_guest_max_active_allows_owner(tmp_path):
    library = Library(None, [])
    active = Torrent("a" * 40, "Busy", "Downloading", 10.0, 0, 0)
    deluge = _Deluge([active, active, active])
    hit = Hit("Example.Story.2019.1080p", "b" * 40, 20, 1)
    service = Service(
        library,
        _Search([hit]),
        deluge,
        guest_max_active=3,
    )
    text = service.request("Example Story", kind="movie", as_guest=False)
    assert text.lower().startswith("not in the library") or "queued" in text.lower()
    assert len(deluge.added) == 1


def test_guest_max_active_blocks_season_episode_fanout(tmp_path):
    library = Library(None, [])
    active = [
        Torrent("1" * 40, "Busy1", "Downloading", 10.0, 0, 0),
        Torrent("2" * 40, "Busy2", "Downloading", 10.0, 0, 0),
    ]
    deluge = _Deluge(active)
    hits = [
        Hit("Example.Show.S01E01.1080p", "c" * 40, 30, 1),
        Hit("Example.Show.S01E02.1080p", "d" * 40, 30, 1),
    ]
    service = Service(library, _Search(hits), deluge, guest_max_active=2)
    text = service.request("Example Show S01", kind="show", as_guest=True)
    assert "guest limit" in text.lower() or "guest download limit" in text.lower()
    assert deluge.added == []


def test_guest_season_fanout_is_all_or_nothing(tmp_path):
    library = Library(None, [])
    active = [Torrent("1" * 40, "Busy1", "Downloading", 10.0, 0, 0)]
    deluge = _Deluge(active)
    hits = [
        Hit("Example.Show.S01E01.1080p", "c" * 40, 30, 1),
        Hit("Example.Show.S01E02.1080p", "d" * 40, 30, 1),
        Hit("Example.Show.S01E03.1080p", "e" * 40, 30, 1),
    ]
    service = Service(library, _Search(hits), deluge, guest_max_active=3)
    text = service.request("Example Show S01", kind="show", as_guest=True)
    assert "guest limit" in text.lower() or "guest download limit" in text.lower()
    assert deluge.added == []


def test_recent_hides_paths_from_guests(tmp_path):
    from plex_queue.ledger import Ledger
    from plex_queue.place import Label

    ledger = Ledger(tmp_path / "imports.json")
    digest = "ab" * 20
    dest = str(tmp_path / "Movies" / "Secret")
    ledger.remember(digest, Label("movie", "Secret Film", 2020, None, None))
    ledger.mark(digest, status="imported", dest=dest, imported_at="2099-01-01T00:00:00+00:00")
    service = Service(Library(None, []), _Search([]), _Deluge(), ledger=ledger)
    guest = service.recent("month", reveal_paths=False)
    owner = service.recent("month", reveal_paths=True)
    assert "Secret Film" in guest
    assert dest not in guest
    assert dest in owner


def test_apply_trash_allows_larger_lower_res_file(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    keep = root / "Example.Story.1080p.mkv"
    loser = root / "Example.Story.720p.mkv"
    keep.write_bytes(b"small")
    loser.write_bytes(b"much-larger-duplicate-file")
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    row = {
        "id": "big72001",
        "kind": "trash",
        "priority": 1,
        "title": "Example",
        "detail": "larger lower-res encode",
        "source": str(loser),
        "keep": str(keep),
        "status": "pending",
        "keep_height": 1080,
        "source_height": 720,
    }
    apply_action(row, roots=[root], trash_dir=trash_dir, recycle=False)
    assert keep.exists()
    assert not loser.exists()


def test_apply_trash_refuses_divergent_episode_titles(tmp_path):
    root = tmp_path / "Shows"
    root.mkdir()
    keep = root / "Doctor Who - old - time - s01e04 - cosmic time.avi"
    loser = root / "Doctor Who - old - doctor who - s01e04 - the firemaker.avi"
    keep.write_bytes(b"a" * 50)
    loser.write_bytes(b"b" * 80)
    row = {
        "id": "dwbad001",
        "kind": "trash",
        "priority": 1,
        "title": "Doctor Who",
        "detail": "bad plex merge",
        "source": str(loser),
        "keep": str(keep),
        "status": "pending",
        "keep_height": 432,
        "source_height": 384,
    }
    try:
        apply_action(row, roots=[root], trash_dir=tmp_path / "trash", recycle=False)
        assert False, "expected OSError"
    except OSError as exc:
        assert "same title" in str(exc).lower()
    assert loser.exists()
    assert keep.exists()


def test_apply_trash_refuses_active_upgrade_path(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    keep = root / "Example.Story.1080p.mkv"
    loser = root / "Example.Story.720p.mkv"
    keep.write_bytes(b"keep")
    loser.write_bytes(b"lose")
    row = {
        "id": "upg00001",
        "kind": "trash",
        "priority": 1,
        "title": "Example",
        "detail": "claimed by upgrade",
        "source": str(loser),
        "keep": str(keep),
        "status": "pending",
        "keep_height": 1080,
        "source_height": 720,
    }
    try:
        apply_action(
            row,
            roots=[root],
            trash_dir=tmp_path / "trash",
            recycle=False,
            upgrade_paths={path_key(loser)},
        )
        assert False, "expected OSError"
    except OSError as exc:
        assert "upgrade" in str(exc).lower()
    assert loser.exists()


def test_apply_trash_refuses_higher_source_height(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    keep = root / "Example.Story.720p.mkv"
    better = root / "Example.Story.1080p.mkv"
    keep.write_bytes(b"a" * 100)
    better.write_bytes(b"b" * 50)
    row = {
        "id": "swap0002",
        "kind": "trash",
        "priority": 1,
        "title": "Example",
        "detail": "tampered height swap",
        "source": str(better),
        "keep": str(keep),
        "status": "pending",
        "keep_height": 720,
        "source_height": 1080,
    }
    try:
        apply_action(row, roots=[root], trash_dir=tmp_path / "trash", recycle=False)
        assert False, "expected OSError"
    except OSError as exc:
        assert "resolution" in str(exc).lower()
    assert better.exists()
    assert keep.exists()


def test_fetch_hits_blocks_redirect(monkeypatch):
    import pytest
    import urllib.error

    from plex_queue import search as search_mod

    monkeypatch.setattr(
        search_mod,
        "validate_search_endpoint",
        lambda endpoint, resolve=True: endpoint,
    )

    def boom(url, timeout=30):
        raise urllib.error.URLError("search.endpoint redirected (302); redirects are blocked")

    monkeypatch.setattr(search_mod, "_https_get_json_pinned", boom)
    with pytest.raises(urllib.error.URLError, match="redirect"):
        search_mod.fetch_hits("Example", "https://example.test/q.php")


def test_fetch_pins_resolved_ip(monkeypatch):
    from plex_queue import search as search_mod

    calls = []

    monkeypatch.setattr(
        search_mod,
        "validate_search_endpoint",
        lambda endpoint, resolve=True: endpoint,
    )

    def fake_get(url, timeout=30):
        calls.append(url)
        return [
            {
                "name": "Example.Story.2019.1080p",
                "info_hash": "ab" * 20,
                "seeders": 3,
                "size": 1,
                "id": "1",
            }
        ]

    monkeypatch.setattr(search_mod, "_https_get_json_pinned", fake_get)
    search_mod._HITS_CACHE.clear()
    hits = search_mod.fetch_hits("Example Story", "https://example.test/q.php")
    assert hits and calls
