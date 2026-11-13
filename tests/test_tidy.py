from __future__ import annotations

import sqlite3
from pathlib import Path

from plex_queue.access import Access
from plex_queue.config import Config
from plex_queue.models import Copy
from plex_queue.service import Service
from plex_queue.tidy import (
    Action,
    apply_action,
    audit,
    bucket_of,
    classify_copy,
    derive_buckets,
    expected_path,
    format_report,
)
from plex_queue.tidy_ledger import TidyLedger
from plex_queue.upgrades import path_key


def test_derive_buckets_from_roots(tmp_path):
    movies = tmp_path / "Movies"
    shows = tmp_path / "Shows"
    kids = tmp_path / "Kids Movies"
    anime = tmp_path / "Anime" / "Shows"
    for path in (movies, shows, kids, anime):
        path.mkdir(parents=True)
    buckets = derive_buckets(
        [movies, shows, kids, anime],
        movie_dirs=[movies],
        show_dirs=[shows],
    )
    assert movies in buckets["movies"]
    assert shows in buckets["shows"]
    assert kids in buckets["kids_movies"]
    assert anime in buckets["anime_shows"]


def test_duplicate_keeps_highest_quality(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    low = root / "Film (2020)" / "Film.720p.mkv"
    high = root / "Film (2020)" / "Film.1080p.mkv"
    low.parent.mkdir(parents=True)
    low.write_bytes(b"a" * 10)
    high.write_bytes(b"b" * 100)
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(low), 1280, 720),
        (1, str(high), 1920, 1080),
    ])
    copies = [
        Copy("movie", "Film", 2020, None, None, None, 720, str(low), ("film",)),
        Copy("movie", "Film", 2020, None, None, None, 1080, str(high), ("film",)),
    ]
    buckets = derive_buckets([root], movie_dirs=[root])
    actions, _notes = audit(copies, plex_db=db, buckets=buckets, roots=[root])
    trash = [row for row in actions if row.kind == "trash"]
    assert len(trash) == 1
    assert path_key(trash[0].source) == path_key(low)
    assert path_key(trash[0].keep) == path_key(high)


def test_duplicate_skips_upgrade_replace_path(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    low = root / "old.mkv"
    high = root / "new.mkv"
    low.write_bytes(b"a" * 10)
    high.write_bytes(b"b" * 100)
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(low), 1280, 720),
        (1, str(high), 1920, 1080),
    ])
    copies = [
        Copy("movie", "Film", 2020, None, None, None, 720, str(low), ("film",)),
        Copy("movie", "Film", 2020, None, None, None, 1080, str(high), ("film",)),
    ]
    buckets = derive_buckets([root], movie_dirs=[root])
    actions, _notes = audit(
        copies,
        plex_db=db,
        buckets=buckets,
        roots=[root],
        upgrade_paths={path_key(low)},
    )
    assert not [row for row in actions if row.kind == "trash"]


def test_duplicate_skips_missing_plex_parts(tmp_path):
    root = tmp_path / "Shows"
    root.mkdir()
    keep = root / "Show" / "Season 01" / "Show - S01E01 - Pilot.mkv"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"b" * 100)
    ghost = root / "Show" / "old" / "Show.S01E01.720p.mkv"
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(ghost), 1280, 720),
        (1, str(keep), 1920, 1080),
    ])
    copies = [
        Copy("episode", "Pilot", None, "Show", 1, 1, 1080, str(keep), ("show", "pilot")),
    ]
    buckets = derive_buckets([root], show_dirs=[root])
    actions, _notes = audit(copies, plex_db=db, buckets=buckets, roots=[root])
    assert not [row for row in actions if row.kind == "trash"]


def test_duplicate_skips_extras_over_main_episode(tmp_path):
    root = tmp_path / "Shows"
    root.mkdir()
    episode = root / "Futurama (1999)" / "Season 1" / "Futurama - S01E04 - Love.mkv"
    extra = root / "Futurama (1999)" / "Extras" / "04 - Futurama Live Post-Show.mp4"
    episode.parent.mkdir(parents=True)
    extra.parent.mkdir(parents=True)
    episode.write_bytes(b"a" * 50)
    extra.write_bytes(b"b" * 80)
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(episode), 800, 600),
        (1, str(extra), 1280, 720),
    ])
    copies = [
        Copy("episode", "Love", None, "Futurama", 1, 4, 600, str(episode), ("futurama", "love")),
        Copy("episode", "Post", None, "Futurama", 1, 4, 720, str(extra), ("futurama", "post")),
    ]
    buckets = derive_buckets([root], show_dirs=[root])
    actions, _notes = audit(copies, plex_db=db, buckets=buckets, roots=[root])
    trash = [row for row in actions if row.kind == "trash"]
    assert trash == []


def test_duplicate_skips_divergent_episode_titles(tmp_path):
    root = tmp_path / "Shows"
    root.mkdir()
    keep = root / "MythBusters" / "S16E04 - Driven to Destruction.mkv"
    other = root / "MythBusters" / "MythBusters - S16E04 - Spike In The Road.mkv"
    keep.parent.mkdir(parents=True)
    keep.write_bytes(b"b" * 100)
    other.write_bytes(b"a" * 40)
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(other), 720, 404),
        (1, str(keep), 1280, 720),
    ])
    copies = [
        Copy("episode", "Driven", None, "MythBusters", 16, 4, 720, str(keep), ("mythbusters", "driven")),
        Copy("episode", "Spike", None, "MythBusters", 16, 4, 404, str(other), ("mythbusters", "spike")),
    ]
    buckets = derive_buckets([root], show_dirs=[root])
    actions, _notes = audit(copies, plex_db=db, buckets=buckets, roots=[root])
    assert not [row for row in actions if row.kind == "trash"]


def test_duplicate_skips_same_number_different_episode_titles(tmp_path):
    """Plex sometimes merges unrelated shorts that only share a numeric prefix."""
    root = tmp_path / "Shows"
    root.mkdir()
    keep = root / "Happy Tree Friends" / "others" / "10 - star kringle.mp4"
    other = root / "Happy Tree Friends" / "2006 tv" / "10 - easy for you to sleigh.mp4"
    keep.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    keep.write_bytes(b"b" * 50)
    other.write_bytes(b"a" * 80)
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(other), 1280, 720),
        (1, str(keep), 1920, 1080),
    ])
    copies = [
        Copy("episode", "Star", None, "Happy Tree Friends", None, None, 1080, str(keep), ("star",)),
        Copy("episode", "Sleigh", None, "Happy Tree Friends", None, None, 720, str(other), ("easy",)),
    ]
    buckets = derive_buckets([root], show_dirs=[root])
    actions, _notes = audit(copies, plex_db=db, buckets=buckets, roots=[root])
    assert not [row for row in actions if row.kind == "trash"]


def test_duplicate_skips_doctor_who_false_same_item(tmp_path):
    root = tmp_path / "Shows"
    root.mkdir()
    keep = (
        root / "Doctor Who (1963)" / "017 - The Time Meddler"
        / "Doctor Who - old - time - s01e04 - cosmic time.avi"
    )
    other = (
        root / "Doctor Who (1963)" / "001 - An Unearthly Child"
        / "Doctor Who - old - doctor who - s01e04 - the firemaker.avi"
    )
    keep.parent.mkdir(parents=True)
    other.parent.mkdir(parents=True)
    keep.write_bytes(b"b" * 50)
    other.write_bytes(b"a" * 80)
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(other), 512, 384),
        (1, str(keep), 576, 432),
    ])
    copies = [
        Copy("episode", "Cosmic", None, "Doctor Who", 1, 4, 432, str(keep), ("cosmic",)),
        Copy("episode", "Firemaker", None, "Doctor Who", 1, 4, 384, str(other), ("firemaker",)),
    ]
    buckets = derive_buckets([root], show_dirs=[root])
    actions, _notes = audit(copies, plex_db=db, buckets=buckets, roots=[root])
    assert not [row for row in actions if row.kind == "trash"]


def test_apply_trash_reports_which_path_is_missing(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    keep = root / "Keep.mkv"
    keep.write_bytes(b"keep")
    row = {
        "id": "misssrc1",
        "kind": "trash",
        "priority": 1,
        "title": "Example",
        "detail": "missing source",
        "source": str(root / "Gone.mkv"),
        "keep": str(keep),
        "status": "pending",
    }
    try:
        apply_action(row, roots=[root], trash_dir=tmp_path / "trash", recycle=False)
        assert False, "expected OSError"
    except OSError as exc:
        assert "Source path is missing" in str(exc)


def test_orphan_proposes_scan(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    orphan = root / "Loose.mkv"
    orphan.write_bytes(b"x")
    db = tmp_path / "plex.db"
    _plex_sections(db, [(7, str(root))])
    copies = [
        Copy("file", "Loose", None, None, None, None, None, str(orphan), ("loose",)),
    ]
    buckets = derive_buckets([root], movie_dirs=[root])
    actions, _notes = audit(copies, plex_db=db, buckets=buckets, roots=[root])
    scans = [row for row in actions if row.kind == "scan"]
    assert len(scans) == 1
    assert path_key(scans[0].source) == path_key(root)


def test_layout_expected_movie_path(tmp_path):
    root = tmp_path / "Movies"
    messy = root / "Film.2020.1080p.mkv"
    copy = Copy("movie", "Film", 2020, None, None, None, 1080, str(messy), ("film",))
    expected = expected_path(copy, root)
    assert expected == root / "Film (2020)" / "Film.2020.1080p.mkv"


def test_layout_only_cleans_release_junk_movie_folders(tmp_path):
    from plex_queue.tidy import _layout_actions

    movies = tmp_path / "Movies"
    movies.mkdir()
    junk = (
        movies / "Road House (2024) [1080p] [WEBRip] [YTS.MX]"
        / "Road.House.2024.1080p.WEBRip.x265.mp4"
    )
    this_is_40 = (
        movies / "This Is 40 (2012) (1080p BluRay x265 HEVC 10bit AAC 5.1 Tigole)"
        / "This Is 40 (2012).mkv"
    )
    rush = (
        movies / "Rush Hour 3 (2007) [1080p] [BluRay] [YTS.MX]"
        / "Rush.Hour.3.2007.mp4"
    )
    clean = movies / "Hard Target 2 (2016)" / "Hard Target 2 (2016).mp4"
    pack = (
        movies / "Twilight.Saga.Complete.2008-2012.1080p.Rifftrax"
        / "Twilight2.New.Moon.2009.mkv"
    )
    episode = (
        movies.parent / "Shows" / "ER (1994)" / "Season 11"
        / "ER - S11E07 - White Guy, Dark Hair.avi"
    )
    shows = tmp_path / "Shows"
    for path in (junk, this_is_40, rush, clean, pack, episode):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 40)
    buckets = derive_buckets(
        [movies, shows], movie_dirs=[movies], show_dirs=[shows],
    )
    copies = [
        Copy("movie", "Road House", 2024, None, None, None, 1080, str(junk), ("road",)),
        Copy("movie", "This Is 40", 2012, None, None, None, 1080, str(this_is_40), ("this",)),
        Copy("movie", "Rush Hour 3", 2007, None, None, None, 1080, str(rush), ("rush",)),
        Copy(
            "movie", "Hard Target 2", 2016, None, None, None, 1080, str(clean), ("hard",),
        ),
        Copy(
            "movie", "The Twilight Saga: New Moon", 2009, None, None, None, 1080, str(pack),
            ("twilight",),
        ),
        Copy(
            "episode", "White Guy, Dark Hair", None, "ER", 11, 7, 480, str(episode), ("er",),
        ),
    ]
    actions = _layout_actions(copies, buckets)
    by_src = {action.source: action for action in actions}
    assert str(junk) in by_src
    assert by_src[str(junk)].dest == str(
        movies / "Road House (2024)" / "Road.House.2024.1080p.WEBRip.x265.mp4"
    )
    # Numbers in the title must survive (cleaned_title would strip them).
    assert by_src[str(this_is_40)].dest == str(
        movies / "This Is 40 (2012)" / "This Is 40 (2012).mkv"
    )
    assert by_src[str(rush)].dest == str(
        movies / "Rush Hour 3 (2007)" / "Rush.Hour.3.2007.mp4"
    )
    assert str(clean) not in by_src
    assert str(pack) not in by_src
    assert str(episode) not in by_src


def test_layout_dotted_release_folder_keeps_title_digits(tmp_path):
    from plex_queue.tidy import _layout_actions

    movies = tmp_path / "Movies"
    movies.mkdir()
    dotted = movies / "This.Is.40.2012.1080p.BluRay.x264" / "This.Is.40.2012.mkv"
    dotted.parent.mkdir(parents=True)
    dotted.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies], movie_dirs=[movies])
    copy = Copy(
        "movie", "This Is 40", 2012, None, None, None, 1080, str(dotted), ("this",),
    )
    actions = _layout_actions([copy], buckets)
    assert len(actions) == 1
    assert actions[0].dest == str(movies / "This Is 40 (2012)" / "This.Is.40.2012.mkv")


def test_layout_dotted_folder_humanizes_mangled_plex_title(tmp_path):
    from plex_queue.tidy import _layout_actions

    movies = tmp_path / "Movies"
    movies.mkdir()
    folder = (
        "Some.Like.It.Hot.1959.REMASTERED.1080p.BluRay.H264.AAC-RARBG"
    )
    dotted = movies / folder / f"{folder}.mp4"
    dotted.parent.mkdir(parents=True)
    dotted.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies], movie_dirs=[movies])
    # Plex sometimes stores the release folder as the title.
    copy = Copy(
        "movie", folder, 1959, None, None, None, 1080, str(dotted), ("some",),
    )
    actions = _layout_actions([copy], buckets)
    assert len(actions) == 1
    assert actions[0].dest == str(
        movies / "Some Like It Hot (1959)" / f"{folder}.mp4"
    )


def test_layout_root_file_empty_title_uses_stem_not_bucket(tmp_path):
    from plex_queue.tidy import _layout_actions

    movies = tmp_path / "Movies"
    movies.mkdir()
    loose = movies / "Some.Like.It.Hot.1959.1080p.BluRay.x264.mkv"
    loose.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies], movie_dirs=[movies])
    copy = Copy(
        "movie", "", 1959, None, None, None, 1080, str(loose), ("some",),
    )
    actions = _layout_actions([copy], buckets)
    assert len(actions) == 1
    dest = Path(actions[0].dest)
    assert dest.parent.name == "Some Like It Hot (1959)"
    assert "Movies" not in dest.parent.name
    assert dest.name == loose.name


def test_layout_skips_clean_title_year_file_on_root(tmp_path):
    from plex_queue.tidy import _layout_actions

    movies = tmp_path / "Movies"
    movies.mkdir()
    loose = movies / "Dragon Ball Super Super Hero (2022).mkv"
    loose.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies], movie_dirs=[movies])
    copy = Copy(
        "movie",
        "Dragon Ball Super: Super Hero",
        2022,
        None,
        None,
        None,
        1080,
        str(loose),
        ("dragon",),
    )
    assert _layout_actions([copy], buckets) == []


def test_layout_skips_cosmetic_movie_folder_tweaks(tmp_path):
    from plex_queue.tidy import _layout_actions

    movies = tmp_path / "Movies"
    movies.mkdir()
    path = movies / "Alita - Battle Angel (2019)" / "Alita - Battle Angel (2019).mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies], movie_dirs=[movies])
    copy = Copy(
        "movie", "Alita: Battle Angel", 2019, None, None, None, 1080, str(path), ("alita",),
    )
    assert _layout_actions([copy], buckets) == []


def test_layout_keeps_collection_nesting_when_cleaning_junk(tmp_path):
    from plex_queue.tidy import _layout_actions

    movies = tmp_path / "Movies"
    nested = (
        movies / "Marvel" / "Road House (2024) [1080p] [YTS.MX]"
        / "Road.House.2024.mp4"
    )
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies], movie_dirs=[movies])
    copy = Copy(
        "movie", "Road House", 2024, None, None, None, 1080, str(nested), ("road",),
    )
    actions = _layout_actions([copy], buckets)
    assert len(actions) == 1
    assert actions[0].dest == str(
        movies / "Marvel" / "Road House (2024)" / "Road.House.2024.mp4"
    )


def test_layout_does_not_double_title_folder_under_clean_parent(tmp_path):
    from plex_queue.tidy import _layout_actions

    movies = tmp_path / "Movies"
    nested = (
        movies / "Road House (2024)" / "Road House (2024) [1080p]"
        / "Road.House.2024.mp4"
    )
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies], movie_dirs=[movies])
    copy = Copy(
        "movie", "Road House", 2024, None, None, None, 1080, str(nested), ("road",),
    )
    actions = _layout_actions([copy], buckets)
    assert len(actions) == 1
    assert actions[0].dest == str(
        movies / "Road House (2024)" / "Road.House.2024.mp4"
    )


def test_misplaced_kids_movie(tmp_path):
    from plex_queue.tidy import _misplaced_actions

    movies = tmp_path / "Movies"
    kids = tmp_path / "Kids Movies"
    movies.mkdir()
    kids.mkdir()
    path = movies / "Toy Story (1995)" / "Toy Story.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * 50)
    copy = Copy(
        "movie", "Toy Story", 1995, None, None, None, 1080, str(path), ("toy", "story"),
    )
    buckets = derive_buckets([movies, kids], movie_dirs=[movies])
    assert bucket_of(path, buckets) == "movies"
    assert classify_copy(copy, {"kids", "animation"}) == "kids_movies"
    # family alone is too weak; family+animation or kids/children qualifies.
    assert classify_copy(copy, {"family"}) == "movies"
    assert classify_copy(copy, {"family", "animation"}) == "kids_movies"
    assert kids in buckets["kids_movies"]
    actions = _misplaced_actions(
        [copy],
        buckets,
        genres_by_path={path_key(path): {"kids", "animation"}},
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert len(actions) == 1
    assert actions[0].kind == "move"
    assert actions[0].bucket == "kids_movies"


def test_misplaced_does_not_leave_specialized_shelves(tmp_path):
    """Anime/kids/christmas placements are trusted even when genres disagree."""
    from plex_queue.tidy import _misplaced_actions

    shows = tmp_path / "Shows"
    anime = tmp_path / "Anime" / "Shows"
    kids = tmp_path / "Kid Shows"
    christmas = tmp_path / "Christmas Movies"
    kids_movies = tmp_path / "Kids Movies"
    for path in (shows, anime, kids, christmas, kids_movies):
        path.mkdir(parents=True)
    dbz = anime / "Dragon Ball Z (1989)" / "Dragon Ball Z - 59.mkv"
    malcolm = kids / "Malcolm" / "Season 5" / "Malcolm S05E20.mkv"
    white = christmas / "White Christmas (1954)" / "White.Christmas.1954.mp4"
    for path in (dbz, malcolm, white):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"a" * 40)
    buckets = derive_buckets(
        [shows, anime, kids, christmas, kids_movies],
        movie_dirs=[kids_movies],
        show_dirs=[shows],
    )
    assert bucket_of(dbz, buckets) == "anime_shows"
    assert bucket_of(malcolm, buckets) == "kids_shows"
    assert bucket_of(white, buckets) == "christmas"
    copies = [
        Copy("episode", "Destination Guru", None, "Dragon Ball Z", 1, 59, 480, str(dbz), ("dragon",)),
        Copy("episode", "Victor", None, "Malcolm in the Middle", 5, 20, 1080, str(malcolm), ("malcolm",)),
        Copy("movie", "White Christmas", 1954, None, None, None, 1080, str(white), ("white", "christmas")),
    ]
    # Genres would pull White Christmas to kids_movies; thin genres pull shows to generic.
    genres = {
        path_key(white): {"family", "kids", "musical"},
    }
    actions = _misplaced_actions(
        copies,
        buckets,
        genres_by_path=genres,
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert actions == []


def test_misplaced_keeps_show_extras_and_holiday_episodes(tmp_path):
    """Extras/featurettes and holiday episodes stay on the show shelf."""
    from plex_queue.tidy import _misplaced_actions

    shows = tmp_path / "Shows"
    movies = tmp_path / "Movies"
    christmas = tmp_path / "Christmas Movies"
    for path in (shows, movies, christmas):
        path.mkdir(parents=True)

    myth = (
        shows / "Mythbusters" / "Bonus Scenes" / "Mythbusters After-show" / "Extra 1.mkv"
    )
    outtakes = shows / "Black Books (2000)" / "Season 2" / "Extras" / "Out-Takes.avi"
    blogs = (
        shows / "Battlestar Galactica" / "Featurettes" / "Season 2"
        / "David Eick Video Blogs - Episode - 205 Day Two.mkv"
    )
    south = (
        shows / "South Park (1997)" / "Season 23"
        / "South Park - S23E10 - Christmas Snow.mkv"
    )
    family = (
        shows / "All in the Family (1971)"
        / "Archie Bunker's Place - S02E13 - Murray Klein's Place.mkv"
    )
    downloads = tmp_path / "Downloads" / "Benchy.mp4"
    for path in (myth, outtakes, blogs, south, family, downloads):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 40)

    buckets = derive_buckets(
        [shows, movies, christmas],
        movie_dirs=[movies],
        show_dirs=[shows],
    )
    copies = [
        Copy("movie", "Extra", None, None, None, None, None, str(myth), ("extra",)),
        Copy("movie", "Out-Takes", None, None, None, None, None, str(outtakes), ("out",)),
        Copy(
            "movie", "David Eick Video Blogs Episode Day Two", None, None, None, None,
            None, str(blogs), ("david",),
        ),
        Copy(
            "episode", "Christmas Snow", None, "South Park", 23, 10, 1080, str(south),
            ("south", "park"),
        ),
        Copy(
            "episode", "Murray Klein's Place", None, "All in the Family", 2, 13, 480,
            str(family), ("all", "family"),
        ),
        Copy(
            "episode", "Downloads S02E10", None, "Downloads", 2, 10, None, str(downloads),
            ("downloads",),
        ),
    ]
    genres = {
        path_key(south): {"holiday", "animation"},
        path_key(family): {"holiday", "comedy"},
        path_key(myth): set(),
        path_key(outtakes): set(),
        path_key(blogs): set(),
    }
    actions = _misplaced_actions(
        copies,
        buckets,
        genres_by_path=genres,
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert actions == []
    assert classify_copy(copies[3], {"holiday"}) == "shows"
    assert classify_copy(
        Copy("movie", "Parenthood", 1989, None, None, None, None, "x", ("parenthood",)),
        {"family"},
    ) == "movies"


def test_misplaced_does_not_yank_show_episodes_to_documentary(tmp_path):
    from plex_queue.tidy import _misplaced_actions

    shows = tmp_path / "Shows"
    docs = tmp_path / "Documentary"
    movies = tmp_path / "Movies"
    for path in (shows, docs, movies):
        path.mkdir(parents=True)
    ep = shows / "Planet Earth" / "Season 01" / "Planet Earth S01E01.mkv"
    ep.parent.mkdir(parents=True)
    ep.write_bytes(b"x" * 40)
    buckets = derive_buckets(
        [shows, docs, movies],
        movie_dirs=[movies],
        show_dirs=[shows],
    )
    copy = Copy(
        "episode", "From Pole to Pole", None, "Planet Earth", 1, 1, 1080, str(ep),
        ("planet", "earth"),
    )
    actions = _misplaced_actions(
        [copy],
        buckets,
        genres_by_path={path_key(ep): {"documentary"}},
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert actions == []


def test_misplaced_movie_to_documentary(tmp_path):
    from plex_queue.tidy import _misplaced_actions

    movies = tmp_path / "Movies"
    docs = tmp_path / "Documentary"
    for path in (movies, docs):
        path.mkdir(parents=True)
    film = movies / "Born in China (2016)" / "Born in China (2016).mp4"
    film.parent.mkdir(parents=True)
    film.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies, docs], movie_dirs=[movies])
    copy = Copy(
        "movie", "Born in China", 2016, None, None, None, 1080, str(film), ("born", "china"),
    )
    actions = _misplaced_actions(
        [copy],
        buckets,
        genres_by_path={path_key(film): {"documentary"}},
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert len(actions) == 1
    assert actions[0].kind == "move"
    assert actions[0].bucket == "documentary"


def test_classify_roman_holiday_is_not_christmas():
    copy = Copy(
        "movie", "Roman Holiday", 1953, None, None, None, 1080, "x", ("roman", "holiday"),
    )
    assert classify_copy(copy, set()) == "movies"
    assert classify_copy(copy, {"romance", "comedy"}) == "movies"
    # Explicit Plex holiday/christmas genre still counts.
    assert classify_copy(copy, {"holiday"}) == "christmas"
    assert classify_copy(
        Copy("movie", "Elf", 2003, None, None, None, 1080, "x", ("elf",)),
        set(),
    ) == "movies"
    assert classify_copy(
        Copy("movie", "The Christmas Chronicles", 2018, None, None, None, 1080, "x", ("christmas",)),
        set(),
    ) == "christmas"


def test_classify_standup_specials_not_documentary():
    leanne = Copy(
        "movie", "Leanne Morgan: I'm Every Woman", 2023, None, None, None, 1080, "x",
        ("leanne",),
    )
    katt = Copy(
        "movie", "Katt Williams: It's Pimpin' Pimpin'", 2008, None, None, None, 1080, "x",
        ("katt",),
    )
    making_of = Copy(
        "movie", "Obi-Wan Kenobi: A Jedi's Return", 2022, None, None, None, 1080, "x",
        ("obi",),
    )
    concert = Copy(
        "movie", "South Park: The 25th Anniversary Concert", 2022, None, None, None, 1080, "x",
        ("south",),
    )
    assert classify_copy(leanne, {"documentary", "comedy"}) == "standup"
    assert classify_copy(katt, {"documentary", "comedy"}) == "standup"
    # Comedy alone is not enough — that yanked Star Wars / Guardians into Standup.
    assert classify_copy(
        Copy(
            "movie", "Brad Williams: Daddy Issues", 2016, None, None, None, 1080, "x",
            ("brad",),
        ),
        {"comedy"},
    ) == "movies"
    assert classify_copy(
        Copy(
            "movie", "Brad Williams: Daddy Issues", 2016, None, None, None, 1080, "x",
            ("brad",),
        ),
        {"stand-up", "comedy"},
    ) == "standup"
    assert classify_copy(
        Copy("movie", "Louis C.K.: Hilarious", 2010, None, None, None, 1080, "x", ("louis",)),
        {"documentary", "comedy"},
    ) == "standup"
    assert classify_copy(
        Copy(
            "movie", "Star Wars: Episode I - The Phantom Menace", 1999, None, None, None,
            1080, "x", ("star",),
        ),
        {"comedy", "adventure"},
    ) == "movies"
    assert classify_copy(
        Copy(
            "movie", "Men in Black: International", 2019, None, None, None, 1080, "x",
            ("men",),
        ),
        {"comedy", "action"},
    ) == "movies"
    assert classify_copy(
        Copy(
            "movie", "Jackass Presents: Bad Grandpa", 2013, None, None, None, 1080, "x",
            ("jackass",),
        ),
        {"documentary", "comedy"},
    ) == "documentary"
    assert classify_copy(
        Copy(
            "movie", "Airplane II: The Sequel", 1982, None, None, None, 1080, "x",
            ("airplane",),
        ),
        {"documentary", "comedy"},
    ) == "documentary"
    assert classify_copy(making_of, {"documentary"}) == "documentary"
    assert classify_copy(concert, {"documentary", "comedy"}) == "documentary"
    assert classify_copy(
        Copy("movie", "Stand Up to Cancer", 2020, None, None, None, 1080, "x", ("stand",)),
        {"documentary"},
    ) == "documentary"
    assert classify_copy(
        Copy("movie", "Frasier S01Extras The Mystery Of Maris", None, None, None, None, None, "x", ("frasier",)),
        set(),
    ) == "shows"


def test_recovery_undoes_bad_shows_to_movies_and_special_exits(tmp_path):
    from plex_queue.tidy import _recovery_actions

    shows = tmp_path / "Shows"
    movies = tmp_path / "Movies"
    kids = tmp_path / "Kid Shows"
    standup = tmp_path / "Standup"
    for path in (shows, movies, kids, standup):
        path.mkdir(parents=True)

    # Extra ripped into Movies; original Featurettes folder still exists.
    orig_extra = (
        shows / "Veep" / "Featurettes" / "Season 7" / "Character Retrospectives" / "Jonah Ryan.mkv"
    )
    orig_extra.parent.mkdir(parents=True)
    current_extra = movies / "Jonah Ryan" / "Jonah Ryan.mkv"
    current_extra.parent.mkdir(parents=True)
    current_extra.write_bytes(b"x" * 40)

    # Real movie that happened to live under Shows — do not undo.
    ice = movies / "The Ice Pirates (1984)" / "The Ice Pirates.mkv"
    ice.parent.mkdir(parents=True)
    ice.write_bytes(b"y" * 40)
    ice_orig = shows / "Misc" / "The Ice Pirates.mkv"

    # Kids episode pulled onto general Shows.
    kid_now = shows / "Rugrats" / "Season 03" / "Rugrats S03E46.avi"
    kid_now.parent.mkdir(parents=True)
    kid_now.write_bytes(b"z" * 40)
    kid_orig = kids / "Rugrats" / "Season 3" / "Rugrats - S03E46.avi"
    kid_orig.parent.mkdir(parents=True)

    # Standup special yanked into Movies.
    brad_now = movies / "Brad Williams Daddy Issues (2016)" / "Brad.Williams.mkv"
    brad_now.parent.mkdir(parents=True)
    brad_now.write_bytes(b"w" * 40)
    brad_orig = standup / "Brad Williams Daddy Issues (2016)" / "Brad.Williams.mkv"
    brad_orig.parent.mkdir(parents=True)

    buckets = derive_buckets(
        [shows, movies, kids, standup],
        movie_dirs=[movies],
        show_dirs=[shows],
    )
    copies = [
        Copy("movie", "Jonah Ryan", None, None, None, None, None, str(current_extra), ("jonah",)),
        Copy("movie", "The Ice Pirates", 1984, None, None, None, 1080, str(ice), ("ice",)),
        Copy("episode", "KidTV", None, "Rugrats", 3, 46, 480, str(kid_now), ("rugrats",)),
        Copy(
            "movie", "Brad Williams: Daddy Issues", 2016, None, None, None, 1080, str(brad_now),
            ("brad",),
        ),
    ]
    prior = [
        {
            "kind": "move",
            "status": "done",
            "source": str(orig_extra),
            "dest": str(current_extra),
            "detail": "Misplaced in shows; move to movies.",
            "title": "Jonah Ryan",
        },
        {
            "kind": "move",
            "status": "done",
            "source": str(ice_orig),
            "dest": str(ice),
            "detail": "Misplaced in shows; move to movies.",
            "title": "The Ice Pirates BONE (1984)",
        },
        {
            "kind": "move",
            "status": "done",
            "source": str(kid_orig),
            "dest": str(kid_now),
            "detail": "Misplaced in kids_shows; move to shows.",
            "title": "Rugrats S03E46",
        },
        {
            "kind": "move",
            "status": "done",
            "source": str(brad_orig),
            "dest": str(brad_now),
            "detail": "Misplaced in standup; move to movies.",
            "title": "Brad Williams: Daddy Issues (2016)",
        },
    ]
    actions = _recovery_actions(
        copies,
        buckets,
        prior,
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    by_title = {action.title: action for action in actions}
    assert "Jonah Ryan" in by_title
    assert by_title["Jonah Ryan"].dest == str(orig_extra)
    assert by_title["Jonah Ryan"].bucket == "shows"
    assert "The Ice Pirates (1984)" not in by_title
    assert "Rugrats S03E46" in by_title or any("Rugrats" in a.title for a in actions)
    rugrats = next(a for a in actions if "Rugrats" in a.title)
    assert rugrats.bucket == "kids_shows"
    assert rugrats.dest == str(kid_orig)
    brad = next(a for a in actions if "Brad" in a.title)
    assert brad.bucket == "standup"
    assert brad.dest == str(brad_orig)


def test_recovery_undoes_false_standup_promotions(tmp_path):
    from plex_queue.tidy import _recovery_actions

    movies = tmp_path / "Movies"
    standup = tmp_path / "Standup"
    for path in (movies, standup):
        path.mkdir()
    star_now = standup / "Star Wars Episode I" / "Star Wars.mp4"
    star_orig = movies / "Star Wars Episode I" / "Star Wars.mp4"
    gaff_now = standup / "Jim Gaffigan The Skinny" / "Jim.Gaffigan.mp4"
    gaff_orig = movies / "Jim Gaffigan The Skinny" / "Jim.Gaffigan.mp4"
    # Renamed after the bad move — ledger dest is stale, filename still under Standup.
    pirates_ledger = standup / "Pirates of the Caribbean Dead Men" / "Pirates.mp4"
    pirates_now = standup / "Pirates of the Caribbean Dead Men Tell No Tales (2017)" / "Pirates.mp4"
    pirates_orig = movies / "Pirates" / "Pirates.mp4"
    for path in (star_now, star_orig, gaff_now, gaff_orig, pirates_now, pirates_orig):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies, standup], movie_dirs=[movies])
    copies = [
        Copy(
            "movie", "Star Wars: Episode I - The Phantom Menace", 1999, None, None, None,
            1080, str(star_now), ("star",),
        ),
        Copy(
            "movie", "Jim Gaffigan: The Skinny", 2024, None, None, None, 1080, str(gaff_now),
            ("jim",),
        ),
        Copy(
            "movie", "Pirates of the Caribbean: Dead Men Tell No Tales", 2017, None, None, None,
            1080, str(pirates_now), ("pirates",),
        ),
    ]
    prior = [
        {
            "kind": "move",
            "status": "done",
            "source": str(star_orig),
            "dest": str(star_now),
            "detail": "Misplaced in movies; move to standup.",
        },
        {
            "kind": "move",
            "status": "done",
            "source": str(gaff_orig),
            "dest": str(gaff_now),
            "detail": "Misplaced in movies; move to standup.",
        },
        {
            "kind": "move",
            "status": "done",
            "source": str(pirates_orig),
            "dest": str(pirates_ledger),
            "detail": "Misplaced in movies; move to standup.",
        },
    ]
    actions = _recovery_actions(
        copies,
        buckets,
        prior,
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    titles = {action.title for action in actions}
    assert any("Star Wars" in title for title in titles)
    assert any("Pirates" in title for title in titles)
    # Real person-special title stays on standup (no undo).
    assert not any("Gaffigan" in title for title in titles)


def test_recovery_undoes_episode_yanked_to_movies_without_extra_folder(tmp_path):
    from plex_queue.tidy import _recovery_actions

    shows = tmp_path / "Shows"
    movies = tmp_path / "Movies"
    for path in (shows, movies):
        path.mkdir()
    orig = shows / "Ted Lasso" / "Season 02" / "Ted Lasso - S02E01 - Goodbye Earl.mkv"
    orig.parent.mkdir(parents=True)
    current = movies / "Ted Lasso Goodbye Earl" / "Ted Lasso - S02E01 - Goodbye Earl.mkv"
    current.parent.mkdir(parents=True)
    current.write_bytes(b"x" * 40)
    buckets = derive_buckets([shows, movies], movie_dirs=[movies], show_dirs=[shows])
    copy = Copy(
        "episode", "Goodbye Earl", None, "Ted Lasso", 2, 1, 1080, str(current), ("ted",),
    )
    actions = _recovery_actions(
        [copy],
        buckets,
        [
            {
                "kind": "move",
                "status": "done",
                "source": str(orig),
                "dest": str(current),
                "detail": "Misplaced in shows; move to movies.",
            }
        ],
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert len(actions) == 1
    assert actions[0].bucket == "shows"
    assert actions[0].dest == str(orig)


def test_format_report_tail_breaks_down_kinds():
    from plex_queue.tidy import format_report

    rows = []
    for i in range(5):
        rows.append(
            Action(
                id=f"scan{i:04d}",
                kind="scan",
                priority=2,
                title=f"Scan {i}",
                detail="orphan",
                source=f"/s/{i}",
                dest=f"/s/{i}",
            ).as_row()
        )
    for i in range(10):
        rows.append(
            Action(
                id=f"move{i:04d}",
                kind="move",
                priority=3,
                title=f"Move {i}",
                detail="misplaced",
                source=f"/m/{i}",
                dest=f"/d/{i}",
            ).as_row()
        )
    for i in range(50):
        rows.append(
            Action(
                id=f"ren{i:04d}",
                kind="rename",
                priority=4,
                title=f"Rename {i}",
                detail="layout",
                source=f"/r/{i}",
                dest=f"/r2/{i}",
            ).as_row()
        )
    text = format_report(rows, limit=40)
    assert "Pending: 5 scan, 10 move, 50 rename" in text
    # Priority order fills the page with scans+moves first; the hidden tail is renames.
    assert "…and 25 more (25 rename; showing first 40" in text


def test_misplaced_skips_trash_tree(tmp_path):
    from plex_queue.tidy import _misplaced_actions

    movies = tmp_path / "Movies"
    shows = tmp_path / "Shows"
    for path in (movies, shows):
        path.mkdir()
    trash_ep = movies / "_trash" / "ted lasso — Ted Lasso - S02E01 - Goodbye Earl.mkv"
    trash_ep.parent.mkdir(parents=True)
    trash_ep.write_bytes(b"x" * 40)
    buckets = derive_buckets([movies, shows], movie_dirs=[movies], show_dirs=[shows])
    copy = Copy(
        "episode", "Goodbye Earl", None, "Ted Lasso", 2, 1, 1080, str(trash_ep), ("ted",),
    )
    actions = _misplaced_actions(
        [copy],
        buckets,
        genres_by_path={},
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert actions == []


def test_apply_trash_refuses_outside_roots(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    outside = tmp_path / "elsewhere.mkv"
    outside.write_bytes(b"x")
    row = Action(
        id="abcd1234",
        kind="trash",
        priority=1,
        title="x",
        detail="dupe",
        source=str(outside),
        keep=str(root / "keep.mkv"),
    ).as_row()
    (root / "keep.mkv").write_bytes(b"keep")
    try:
        apply_action(row, roots=[root], trash_dir=tmp_path / "trash", recycle=False)
        assert False, "expected OSError"
    except OSError as exc:
        assert "outside" in str(exc).lower()


def test_apply_move_stays_confined(tmp_path):
    movies = tmp_path / "Movies"
    kids = tmp_path / "Kids Movies"
    movies.mkdir()
    kids.mkdir()
    source = movies / "Toy Story (1995)" / "Toy Story.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    dest = kids / "Toy Story (1995)" / "Toy Story.mkv"
    row = {
        "id": "move0001",
        "kind": "move",
        "priority": 3,
        "title": "Toy Story (1995)",
        "detail": "misplaced",
        "source": str(source),
        "dest": str(dest),
        "status": "pending",
    }
    message = apply_action(
        row,
        roots=[movies, kids],
        trash_dir=None,
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert dest.is_file()
    assert not source.exists()
    assert "Moved" in message


def test_cleanup_partial_dest_removes_incomplete_copy(tmp_path):
    from plex_queue.tidy import _cleanup_partial_dest

    root = tmp_path / "Movies"
    root.mkdir()
    source = root / "Keep.mkv"
    dest = tmp_path / "Other" / "Partial.mkv"
    dest.parent.mkdir()
    source.write_bytes(b"full")
    dest.write_bytes(b"partial")
    _cleanup_partial_dest(source, dest)
    assert source.exists()
    assert not dest.exists()


def test_apply_move_does_not_reread_plex_via_invalidate(tmp_path, monkeypatch):
    from plex_queue.library import Library

    movies = tmp_path / "Movies"
    kids = tmp_path / "Kids Movies"
    movies.mkdir()
    kids.mkdir()
    source = movies / "Film (2020)" / "Film.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    dest = kids / "Film (2020)" / "Film.mkv"
    library = Library(None, [movies, kids])
    library.loaded = True
    library.copies = [
        Copy("movie", "Film", 2020, None, None, None, 1080, str(source), ("film",)),
    ]
    calls = {"refresh": 0}

    def boom(*_args, **_kwargs):
        calls["refresh"] += 1
        raise AssertionError("batch moves must not re-read Plex after each file")

    monkeypatch.setattr(library, "_refresh_unlocked", boom)
    row = {
        "id": "move0002",
        "kind": "move",
        "priority": 3,
        "title": "Film (2020)",
        "detail": "misplaced",
        "source": str(source),
        "dest": str(dest),
        "status": "pending",
    }
    apply_action(
        row,
        roots=[movies, kids],
        trash_dir=None,
        library=library,
        free_of=lambda _path: 10 * 1024 ** 3,
        margin=0,
    )
    assert calls["refresh"] == 0
    assert dest.is_file()
    assert all(copy.path != str(source) for copy in library.copies)
    assert any(Path(copy.path) == dest for copy in library.copies)


def test_tidy_ledger_and_service_owner_gate(tmp_path):
    movies = tmp_path / "Movies"
    movies.mkdir()
    low = movies / "A.720p.mkv"
    high = movies / "A.1080p.mkv"
    low.write_bytes(b"a" * 10)
    high.write_bytes(b"b" * 100)
    db = tmp_path / "plex.db"
    _plex_versions(db, [
        (1, str(low), 1280, 720),
        (1, str(high), 1920, 1080),
    ])
    from plex_queue.library import Library

    library = Library(db, [movies])
    copies = [
        Copy("movie", "A", 2020, None, None, None, 720, str(low), ("a",)),
        Copy("movie", "A", 2020, None, None, None, 1080, str(high), ("a",)),
    ]

    def fake_refresh(include_disk: bool = True) -> None:
        library.copies = list(copies)
        library.notes = ["test index"]
        library.loaded = True

    library.refresh = fake_refresh  # type: ignore[method-assign]
    config = _config(tmp_path, owner_id=42)
    access = Access(config)
    tidy_ledger = TidyLedger(tmp_path / "tidy.json")
    service = Service(
        library,
        search=None,
        deluge=_NoDeluge(),
        access=access,
        roots=[movies],
        trash_dir=tmp_path / "trash",
        tidy_ledger=tidy_ledger,
        plex=_NoPlex(),
        tidy_buckets=derive_buckets([movies], movie_dirs=[movies]),
        plex_db=db,
    )
    denied = service.tidy(user_id=7)
    assert "Only the owner" in denied
    report = service.tidy(user_id=42, refresh=True)
    assert "id " in report.lower() or "Pending" in report or "tidy" in report.lower()
    pending = tidy_ledger.pending({"trash"})
    assert pending
    loser_id = pending[0]["id"]
    skipped = service.tidy_skip(loser_id, user_id=42)
    assert "Skipped" in skipped
    assert tidy_ledger.get(loser_id)["status"] == "skipped"


def test_format_report_empty():
    text = format_report([])
    assert "tidy" in text.lower()


def test_replace_pending_does_not_resurrect_skipped(tmp_path):
    ledger = TidyLedger(tmp_path / "tidy.json")
    row = {
        "id": "abcd1234",
        "kind": "trash",
        "priority": 1,
        "title": "Film",
        "detail": "dupe",
        "source": str(tmp_path / "old.mkv"),
        "keep": str(tmp_path / "new.mkv"),
        "status": "pending",
    }
    ledger.replace_pending([row])
    ledger.mark("abcd1234", status="skipped")
    ledger.replace_pending([{**row, "status": "pending"}])
    assert ledger.get("abcd1234")["status"] == "skipped"
    assert ledger.pending({"trash"}) == []


def test_apply_scan_refuses_outside_roots(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    row = {
        "id": "scan0001",
        "kind": "scan",
        "priority": 2,
        "title": "x",
        "detail": "orphan",
        "source": str(outside),
        "dest": str(outside),
        "status": "pending",
    }
    try:
        apply_action(row, roots=[root], trash_dir=None, plex=_NoPlex())
        assert False, "expected OSError"
    except OSError as exc:
        assert "outside" in str(exc).lower()


def test_apply_scan_fails_when_plex_does_not_refresh(tmp_path):
    root = tmp_path / "Movies"
    root.mkdir()
    row = {
        "id": "scan0002",
        "kind": "scan",
        "priority": 2,
        "title": "x",
        "detail": "orphan",
        "source": str(root),
        "dest": str(root),
        "status": "pending",
    }

    class QuietPlex:
        def refresh_dirs(self, directories):
            return ["Plex token missing."], []

    try:
        apply_action(row, roots=[root], trash_dir=None, plex=QuietPlex())
        assert False, "expected OSError"
    except OSError as exc:
        assert "token" in str(exc).lower() or "could not" in str(exc).lower()


def test_failed_action_can_be_reproposed(tmp_path):
    ledger = TidyLedger(tmp_path / "tidy.json")
    row = {
        "id": "fail1234",
        "kind": "scan",
        "priority": 2,
        "title": "x",
        "detail": "orphan",
        "source": str(tmp_path / "Movies"),
        "status": "pending",
    }
    ledger.replace_pending([row])
    ledger.mark("fail1234", status="failed", error="token missing")
    ledger.replace_pending([{**row, "status": "pending"}])
    assert ledger.get("fail1234")["status"] == "pending"


def _plex_versions(db: Path, rows):
    connection = sqlite3.connect(db)
    try:
        connection.executescript(
            """
            CREATE TABLE metadata_items (
              id INTEGER PRIMARY KEY, deleted_at integer
            );
            CREATE TABLE media_items (
              id INTEGER PRIMARY KEY, metadata_item_id integer,
              width integer, height integer, deleted_at integer
            );
            CREATE TABLE media_parts (
              id INTEGER PRIMARY KEY, media_item_id integer,
              file text, deleted_at integer
            );
            """
        )
        for index, (metadata_id, file_path, width, height) in enumerate(rows, start=1):
            connection.execute(
                "INSERT OR IGNORE INTO metadata_items(id, deleted_at) VALUES (?, NULL)",
                (metadata_id,),
            )
            connection.execute(
                "INSERT INTO media_items(id, metadata_item_id, width, height, deleted_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (index, metadata_id, width, height),
            )
            connection.execute(
                "INSERT INTO media_parts(id, media_item_id, file, deleted_at) "
                "VALUES (?, ?, ?, NULL)",
                (index, index, file_path),
            )
        connection.commit()
    finally:
        connection.close()


def _plex_sections(db: Path, rows):
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS section_locations (
              id INTEGER PRIMARY KEY,
              library_section_id integer,
              root_path text
            )
            """
        )
        # Ensure metadata tables exist for load_plex / parts queries.
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata_items (
              id INTEGER PRIMARY KEY, deleted_at integer,
              title text, year integer, metadata_type integer, parent_id integer
            );
            CREATE TABLE IF NOT EXISTS media_items (
              id INTEGER PRIMARY KEY, metadata_item_id integer,
              width integer, height integer, deleted_at integer
            );
            CREATE TABLE IF NOT EXISTS media_parts (
              id INTEGER PRIMARY KEY, media_item_id integer,
              file text, deleted_at integer
            );
            """
        )
        for index, (section, root) in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO section_locations(id, library_section_id, root_path) "
                "VALUES (?, ?, ?)",
                (index, section, root),
            )
        connection.commit()
    finally:
        connection.close()


class _NoDeluge:
    boot_note = ""

    def list_torrents(self):
        return []


class _NoPlex:
    def refresh_dirs(self, directories):
        return [f"Asked Plex to scan {path}" for path in directories], list(directories)


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
