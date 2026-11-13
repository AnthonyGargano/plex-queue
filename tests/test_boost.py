from __future__ import annotations

import time
from pathlib import Path

from plex_queue.access import Access
from plex_queue.boost import (
    MANUAL_STUCK_MIN_AGE_SECONDS,
    MAX_MAGNETS_PER_TITLE,
    clear_boost_siblings,
    is_stuck,
    pick_alternate_hit,
    stuck_torrents,
    title_group_key,
)
from plex_queue.config import Config
from plex_queue.ledger import Ledger
from plex_queue.library import Library
from plex_queue.models import Hit, Torrent
from plex_queue.place import Label
from plex_queue.service import Service


def _torrent(
    digest: str,
    name: str,
    *,
    progress: float = 0.0,
    rate: int = 0,
    state: str = "Queued",
    age_hours: float = 3.0,
) -> Torrent:
    return Torrent(
        digest,
        name,
        state,
        progress,
        0,
        rate,
        time_added=int(time.time() - age_hours * 3600),
    )


def test_stuck_filter_excludes_young_progressing_and_done():
    now = time.time()
    young = _torrent("a" * 40, "Young.1080p", age_hours=0.5)
    moving = _torrent("b" * 40, "Moving.1080p", rate=50_000)
    progressed = _torrent("c" * 40, "Part.1080p", progress=5.0)
    done = _torrent("d" * 40, "Done.1080p", progress=100.0, state="Seeding")
    stuck = _torrent("e" * 40, "Stuck.1080p", age_hours=3.0)
    assert is_stuck(young, now=now) is False
    assert is_stuck(moving, now=now) is False
    assert is_stuck(progressed, now=now) is False
    assert is_stuck(done, now=now) is False
    assert is_stuck(stuck, now=now) is True
    assert stuck_torrents([young, moving, progressed, done, stuck], now=now) == [stuck]
    # Manual /boost: young idle 0% counts as stuck (no age wait).
    assert is_stuck(young, now=now, min_age_seconds=MANUAL_STUCK_MIN_AGE_SECONDS) is True


def test_title_group_key_keeps_identity_and_ultimatum_apart():
    identity = title_group_key(
        kind="movie", title="The Bourne Identity", year=2002,
    )
    other = title_group_key(
        kind="movie", title="The Bourne Ultimatum", year=2007,
    )
    assert identity is not None and other is not None
    assert identity != other


def test_pick_alternate_skips_excluded_and_low_height():
    stuck_hash = "a" * 40
    other = "b" * 40
    low = "c" * 40
    unknown = "d" * 40
    hits = [
        Hit("Title.2019.1080p.A", stuck_hash, 90, 1),
        Hit("Title.2019.720p.B", low, 500, 1),
        Hit("Title.2019.NoRes.CAM", unknown, 800, 1),
        Hit("Title.2019.1080p.C", other, 40, 1),
    ]
    chosen = pick_alternate_hit(
        hits, min_height=1080, exclude_hashes={stuck_hash},
    )
    assert chosen is not None
    assert chosen.info_hash == other


def test_boost_stuck_adds_one_alternate_and_links_group(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    ledger = Ledger(tmp_path / "imports.json")
    stuck_hash = "11" * 20
    alt_hash = "22" * 20
    ledger.remember(stuck_hash, Label("movie", "Example Story", 2019))
    deluge = _Deluge([_torrent(stuck_hash, "Example.Story.2019.1080p.A")])

    class Search:
        def search(self, query, match_as=None):
            return [
                Hit("Example.Story.2019.1080p.A", stuck_hash, 90, 1),
                Hit("Example.Story.2019.1080p.B", alt_hash, 40, 1),
            ]

    library = Library(None, [movies])
    library.loaded = True
    library.copies = []
    service = Service(
        library, Search(), deluge, ledger=ledger, roots=[movies],
    )
    text = service.boost_stuck(user_id=7)
    assert "Boosted 1" in text
    assert alt_hash in "".join(deluge.added)
    assert ledger.state(stuck_hash)["boost_group"] == stuck_hash
    assert ledger.state(alt_hash)["boost_group"] == stuck_hash
    assert ledger.state(alt_hash)["boost_of"] == stuck_hash


def test_manual_boost_includes_young_idle_torrents(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    ledger = Ledger(tmp_path / "imports.json")
    stuck_hash = "66" * 20
    alt_hash = "77" * 20
    ledger.remember(stuck_hash, Label("movie", "Example Story", 2019))
    deluge = _Deluge([
        _torrent(stuck_hash, "Example.Story.2019.1080p.A", age_hours=0.1),
    ])

    class Search:
        def search(self, query, match_as=None):
            return [
                Hit("Example.Story.2019.1080p.A", stuck_hash, 90, 1),
                Hit("Example.Story.2019.1080p.B", alt_hash, 40, 1),
            ]

    access = Access(_config(tmp_path, owner_id=7))
    library = Library(None, [movies])
    library.loaded = True
    service = Service(
        library, Search(), deluge, access=access, ledger=ledger, roots=[movies],
    )
    assert "Boosted 1" in service.boost_stuck(user_id=7)
    assert alt_hash in "".join(deluge.added)
    # Auto-boost still waits for age.
    deluge2 = _Deluge([
        _torrent(stuck_hash, "Example.Story.2019.1080p.A", age_hours=0.1),
    ])
    service2 = Service(
        library, Search(), deluge2, ledger=ledger, roots=[movies],
    )
    assert service2.boost_stuck(only_when_added=True) == ""


def test_boost_same_title_all_stuck_can_add_until_cap(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    ledger = Ledger(tmp_path / "imports.json")
    first = "11" * 20
    second = "22" * 20
    third = "33" * 20
    fourth = "44" * 20
    ledger.remember(first, Label("movie", "The Bourne Identity", 2002))
    ledger.remember(second, Label("movie", "The Bourne Identity", 2002))
    deluge = _Deluge([
        _torrent(first, "The.Bourne.Identity.2002.1080p.BluRay.H264.AAC-RARBG"),
        _torrent(second, "The Bourne Identity (2002) [1080p]"),
    ])

    class Search:
        def search(self, query, match_as=None):
            return [
                Hit("The.Bourne.Identity.2002.1080p.A", first, 90, 1),
                Hit("The.Bourne.Identity.2002.1080p.B", second, 80, 1),
                Hit("The.Bourne.Identity.2002.1080p.C", third, 70, 1),
                Hit("The.Bourne.Identity.2002.1080p.D", fourth, 60, 1),
            ]

    library = Library(None, [movies])
    library.loaded = True
    library.copies = []
    service = Service(
        library, Search(), deluge, ledger=ledger, roots=[movies],
    )
    text = service.boost_stuck()
    assert "Boosted 1" in text
    assert third in "".join(deluge.added)
    assert len(deluge.torrents) == 3
    # At cap of 3 all-stuck magnets: no fourth.
    assert MAX_MAGNETS_PER_TITLE == 3
    text2 = service.boost_stuck()
    assert "already at 3 magnets" in text2
    assert fourth not in "".join(deluge.added)


def test_boost_skips_title_when_one_copy_is_progressing(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    ledger = Ledger(tmp_path / "imports.json")
    stuck_hash = "11" * 20
    moving_hash = "22" * 20
    alt_hash = "33" * 20
    ledger.remember(stuck_hash, Label("movie", "Example Story", 2019))
    ledger.remember(moving_hash, Label("movie", "Example Story", 2019))
    deluge = _Deluge([
        _torrent(stuck_hash, "Example.Story.2019.1080p.A"),
        _torrent(moving_hash, "Example.Story.2019.1080p.B", progress=12.0, rate=1000),
    ])

    class Search:
        def search(self, query, match_as=None):
            return [
                Hit("Example.Story.2019.1080p.A", stuck_hash, 90, 1),
                Hit("Example.Story.2019.1080p.B", moving_hash, 80, 1),
                Hit("Example.Story.2019.1080p.C", alt_hash, 70, 1),
            ]

    library = Library(None, [movies])
    library.loaded = True
    service = Service(
        library, Search(), deluge, ledger=ledger, roots=[movies],
    )
    text = service.boost_stuck()
    assert "Boosted" not in text
    assert deluge.added == []


def test_boost_retries_when_alternate_was_removed_from_deluge(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    ledger = Ledger(tmp_path / "imports.json")
    stuck_hash = "33" * 20
    gone_hash = "44" * 20
    fresh_hash = "55" * 20
    ledger.remember(stuck_hash, Label("movie", "Example Story", 2019))
    ledger.remember(gone_hash, Label("movie", "Example Story", 2019))
    ledger.link_boost_group(stuck_hash, gone_hash)
    deluge = _Deluge([_torrent(stuck_hash, "Example.Story.2019.1080p.A")])

    class Search:
        def search(self, query, match_as=None):
            return [
                Hit("Example.Story.2019.1080p.A", stuck_hash, 90, 1),
                Hit("Example.Story.2019.1080p.Gone", gone_hash, 80, 1),
                Hit("Example.Story.2019.1080p.Fresh", fresh_hash, 50, 1),
            ]

    library = Library(None, [movies])
    library.loaded = True
    library.copies = []
    service = Service(
        library, Search(), deluge, ledger=ledger, roots=[movies],
    )
    text = service.boost_stuck()
    assert "Boosted 1" in text
    assert any(digest in "".join(deluge.added) for digest in (gone_hash, fresh_hash))


def test_clear_removes_linked_group_not_unlinked_or_other_titles(tmp_path):
    ledger = Ledger(tmp_path / "imports.json")
    finished = "aa" * 20
    sibling = "bb" * 20
    peer = "dd" * 20
    unlinked = "ee" * 20
    other_title = "cc" * 20
    ledger.remember(finished, Label("movie", "The Bourne Identity", 2002))
    ledger.remember(sibling, Label("movie", "The Bourne Identity", 2002))
    ledger.remember(peer, Label("movie", "The Bourne Identity", 2002))
    ledger.remember(unlinked, Label("movie", "The Bourne Identity", 2002))
    ledger.remember(other_title, Label("movie", "The Bourne Ultimatum", 2007))
    ledger.link_boost_members(finished, [sibling, peer])
    deluge = _Deluge([
        _torrent(finished, "The.Bourne.Identity.2002.1080p.A", progress=100.0, state="Seeding"),
        _torrent(sibling, "The Bourne Identity (2002) [1080p]", progress=0.0),
        _torrent(peer, "The.Bourne.Identity.2002.1080p.Other", progress=0.0),
        _torrent(unlinked, "The.Bourne.Identity.2002.1080p.Manual", progress=0.0),
        _torrent(other_title, "The.Bourne.Ultimatum.2007.1080p", progress=99.0),
    ])
    notes = clear_boost_siblings(ledger, deluge, finished)
    assert any("Removed incomplete alternate" in note for note in notes)
    assert sibling in deluge.removed
    assert peer in deluge.removed
    assert unlinked not in deluge.removed
    assert finished not in deluge.removed
    assert other_title not in deluge.removed


def test_clear_without_boost_group_does_not_sweep_by_title(tmp_path):
    ledger = Ledger(tmp_path / "imports.json")
    finished = "aa" * 20
    other = "bb" * 20
    ledger.remember(finished, Label("movie", "The Bourne Identity", 2002))
    ledger.remember(other, Label("movie", "The Bourne Identity", 2002))
    deluge = _Deluge([
        _torrent(finished, "The.Bourne.Identity.2002.1080p.A", progress=100.0, state="Seeding"),
        _torrent(other, "The Bourne Identity (2002) [1080p]", progress=0.0),
    ])
    notes = clear_boost_siblings(ledger, deluge, finished)
    assert notes == []
    assert deluge.removed == []
    assert other not in deluge.removed


def test_boost_owner_gate(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    access = Access(_config(tmp_path, owner_id=7))
    library = Library(None, [movies])
    library.loaded = True
    service = Service(library, _Search([]), _Deluge(), access=access)
    assert service.boost_stuck(user_id=99) == "Only the owner can do that."


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


class _Deluge:
    def __init__(self, torrents=None):
        self.torrents = list(torrents or [])
        self.added = []
        self.removed = []
        self.boot_note = ""

    def list_torrents(self):
        return list(self.torrents)

    def add_magnet(self, magnet):
        self.added.append(magnet)
        digest = ""
        if "btih:" in magnet:
            digest = magnet.split("btih:", 1)[1].split("&", 1)[0].lower()
        name = digest or "boost"
        # Keep new magnets "stuck-aged" so a follow-up boost sees them as stuck.
        self.torrents.append(
            Torrent(
                digest or "ff" * 20,
                name,
                "Queued",
                0,
                0,
                0,
                time_added=int(time.time() - 3 * 3600),
            )
        )

    def remove_hash(self, info_hash):
        digest = info_hash.lower()
        self.removed.append(digest)
        self.torrents = [item for item in self.torrents if item.hash.lower() != digest]


class _Search:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, match_as=None):
        return list(self.hits)
