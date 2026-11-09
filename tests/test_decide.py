from pathlib import Path

from plex_queue.decide import (
    AlreadyHave,
    Enqueue,
    NoUpgrade,
    NothingFound,
    UnknownQuality,
    decide,
    season_covered,
)
from plex_queue.models import Copy, Hit, Torrent, pick_for_removal
from plex_queue.library import Library, copy_from_file
from plex_queue.search import matching_hits
from plex_queue.imdb import Identity, collection_from_rows
from plex_queue.service import Clarify, Service
from plex_queue.titles import parse_request


def movie(title, year, height, path="D:/Movies/file.mkv"):
    return Copy("movie", title, year, None, None, None, height, path)


def hit(name, seeders, info_hash="abc123abc123abc123abc123abc123abc123abcd"):
    return Hit(name, info_hash, seeders, 1)


def test_1080_copy_is_enough():
    decision = decide([movie("Example Story", 2019, 1080)], [hit("Example.Story.2019.2160p", 50)])
    assert isinstance(decision, AlreadyHave)
    assert decision.height == 1080


def test_720_copy_queues_a_1080_release():
    low = hit("Example.Story.2019.720p", 500, "1" * 40)
    high = hit("Example.Story.2019.1080p.BluRay", 40, "2" * 40)
    decision = decide([movie("Example Story", 2019, 720)], [low, high])
    assert isinstance(decision, Enqueue)
    assert decision.hit is high
    assert decision.replacing is not None


def test_720_copy_without_a_better_release_stays():
    decision = decide(
        [movie("Example Story", 2019, 720)],
        [hit("Example.Story.2019.720p", 500)],
    )
    assert isinstance(decision, NoUpgrade)


def test_missing_title_queues_the_best_seeded_1080():
    decision = decide(
        [],
        [
            hit("Example.Story.2019.720p", 900, "1" * 40),
            hit("Example.Story.2019.1080p", 20, "2" * 40),
        ],
    )
    assert isinstance(decision, Enqueue)
    assert decision.hit.info_hash == "2" * 40
    assert decision.replacing is None


def test_missing_title_with_no_hits():
    assert isinstance(decide([], []), NothingFound)


def test_unknown_height_does_not_queue_a_second_copy():
    decision = decide([movie("Example Story", 2019, None)], [hit("Example.Story.2019.1080p", 20)])
    assert isinstance(decision, UnknownQuality)


def episode(show, season, number, height, path="E:/Shows/ep.mkv"):
    return Copy("episode", f"E{number}", 2006, show, season, number, height, path)


def test_one_good_episode_is_not_a_covered_season():
    copies = [
        episode("Example Show", 1, 1, 1080, "E:/Shows/e1.mkv"),
        episode("Example Show", 1, 2, 480, "E:/Shows/e2.mkv"),
    ]
    covered, replacing = season_covered(copies, 1080)
    assert covered is False
    assert replacing is not None
    assert replacing.episode == 2
    assert replacing.height == 480
    decision = decide(copies, [hit("Example.Show.S01.1080p", 40)], season_coverage=True)
    assert isinstance(decision, Enqueue)
    assert decision.replacing is replacing


def test_season_replace_target_is_the_weak_episode_not_the_tallest():
    weak = episode("Example Show", 1, 1, 480, "E:/Shows/e1.mkv")
    strong = episode("Example Show", 1, 2, 2160, "E:/Shows/e2.mkv")
    covered, replacing = season_covered([strong, weak], 1080)
    assert covered is False
    assert replacing is weak


def test_contiguous_1080_episodes_cover_a_season():
    copies = [
        episode("Example Show", 1, 1, 1080),
        episode("Example Show", 1, 2, 1080),
        episode("Example Show", 1, 3, 1080),
    ]
    assert season_covered(copies, 1080)[0] is True
    decision = decide(copies, [hit("Example.Show.S01.2160p", 40)], season_coverage=True)
    assert isinstance(decision, AlreadyHave)


def test_a_gap_in_episode_numbers_does_not_cover_a_season():
    copies = [
        episode("Example Show", 1, 1, 1080),
        episode("Example Show", 1, 3, 1080),
    ]
    assert season_covered(copies, 1080)[0] is False
    decision = decide(copies, [hit("Example.Show.S01.1080p", 12)], season_coverage=True)
    assert isinstance(decision, Enqueue)


def test_loose_file_keeps_its_episode_number():
    copy = copy_from_file(Path("E:/Shows/Example Show S01E03 720p.mkv"))
    assert copy.season == 1
    assert copy.episode == 3
    assert copy.height == 720
    library = Library(None, [])
    library.loaded = True
    library.copies = [copy]
    found = library.find(parse_request("example show s01e03"))
    assert found == [copy]


def test_find_matches_movie_and_show_episode():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        movie("Example Story", 2019, 1080),
        movie("Example Story Reloaded", 2003, 720),
        Copy("episode", "Pilot", 2006, "Example Show", 1, 1, 720, "E:/Shows/pilot.mkv"),
        Copy("episode", "Later", 2006, "Example Show", 1, 2, 1080, "E:/Shows/later.mkv"),
    ]
    found = library.find(parse_request("example story"))
    assert [copy.title for copy in found] == ["Example Story"]
    show = library.find(parse_request("example show"))
    assert len(show) == 2
    one = library.find(parse_request("example show s01e01"))
    assert len(one) == 1
    assert one[0].episode == 1


def _matrix_set():
    return [
        Identity("tt0133093", "The Matrix", 1999, "movie"),
        Identity("tt0234215", "The Matrix Reloaded", 2003, "movie"),
        Identity("tt0242653", "The Matrix Revolutions", 2003, "movie"),
        Identity("tt10838180", "The Matrix Resurrections", 2021, "movie"),
    ]


def test_a_trilogy_leaves_the_movies_you_already_have():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        movie("The Matrix", 1999, 1080),
        movie("The Matrix Reloaded", 2003, 1080),
        movie("The Matrix Revolutions", 2003, 1080),
        movie("The Matrix Resurrections", 2021, 1080),
    ]

    class Search:
        def search(self, query, match_as=None):
            raise AssertionError(query)

    text = Service(library, Search(), _Deluge()).request(
        "matrix trilogy", collection_resolver=lambda name: _matrix_set(),
    )
    assert 'Found 3 movie(s) for "matrix"' in text
    assert "You already have all 3" in text
    assert "Reloaded" in text
    assert "Revolutions" in text
    assert "Resurrections" not in text
    one = library.find(parse_request("The Matrix"))
    assert [copy.title for copy in one] == ["The Matrix"]


def test_all_of_the_movies_queues_only_the_gap():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        movie("Clerks", 1994, 1080),
        movie("Clerks II", 2006, 720),
    ]
    deluge = _Deluge()
    queries = []

    class Search:
        def search(self, query, match_as=None):
            queries.append(match_as or query)
            return matching_hits(match_as or query, [
                hit("Clerks.III.2022.1080p", 9, "ab" * 20),
                hit("Clerks.II.2006.1080p", 8, "cd" * 20),            ])

    members = [
        Identity("tt0109445", "Clerks", 1994, "movie"),
        Identity("tt0424345", "Clerks II", 2006, "movie"),
        Identity("tt11128440", "Clerks III", 2022, "movie"),
    ]
    text = Service(library, Search(), deluge).request(
        "all of the clerks movies", collection_resolver=lambda name: members,
    )
    assert "already covered" in text
    assert "Clerks (1994): Already on Plex" in text
    assert "Clerks III" in text
    assert deluge.added
    assert len(deluge.added) == 2
    assert any("III" in query or "2022" in query for query in queries)
    assert any("II" in query or "2006" in query for query in queries)


def test_suggestion_rows_keep_the_feature_films_in_the_franchise():
    rows = [
        {"id": "tt0109445", "l": "Clerks", "y": 1994, "qid": "movie"},
        {"id": "tt0424345", "l": "Clerks II", "y": 2006, "qid": "movie"},
        {"id": "tt11128440", "l": "Clerks III", "y": 2022, "qid": "movie"},
        {"id": "tt0210413", "l": "Clerks", "y": 2000, "qid": "tvSeries"},
        {"id": "tt3449200", "l": "Shooting Clerks", "y": 2019, "qid": "movie"},
        {"id": "tt0433537", "l": "Clerks: The Lost Scene", "y": 2004, "qid": "video"},
        {"id": "tt31998838", "l": "Matrix 5", "y": None, "qid": "movie"},
    ]
    titles = [item.title for item in collection_from_rows("clerks", rows)]
    assert titles == ["Clerks", "Clerks II", "Clerks III"]


def test_collection_summary_does_not_claim_all_when_some_were_not_filled():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        movie("Clerks", 1994, 1080),
        movie("Clerks II", 2006, 720),
    ]
    members = [
        Identity("tt0109445", "Clerks", 1994, "movie"),
        Identity("tt0424345", "Clerks II", 2006, "movie"),
        Identity("tt11128440", "Clerks III", 2022, "movie"),
    ]

    class Search:
        def search(self, query, match_as=None):
            # Only a low release for II; nothing for III.
            return matching_hits(match_as or query, [hit("Clerks.II.2006.720p", 8, "ee" * 20)])

    text = Service(library, Search(), _Deluge()).request(
        "all of the clerks movies",
        collection_resolver=lambda name: members,
    )
    assert "not filled" in text
    assert "already have all" not in text
    assert 'Found 3 movie(s) for "clerks"' in text
    assert "Clerks (1994): Already on Plex" in text
    assert "Clerks II (2006):" in text
    assert "Clerks III (2022):" in text


def test_all_of_a_tv_show_checks_each_season():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        episode("The Office", 1, 1, 1080),
        episode("The Office", 1, 2, 1080),
        episode("Ted Lasso", 3, 1, 1080),
        episode("Ted Lasso", 3, 2, 1080),
    ]

    class Search:
        def search(self, query, match_as=None):
            needle = (match_as or query).upper()
            if "S01" in needle and "office" in query.casefold():
                return []
            if "S02" in needle and "office" in query.casefold():
                return [hit("The.Office.S02.1080p", 20, "22" * 20)]
            return []

    text = Service(library, Search(), _Deluge()).request("All of the office tv show")
    assert 'Found' in text and 'season' in text.casefold()
    assert "S01:" in text
    assert "Already on Plex" in text
    assert "S02:" in text
    assert "Queued" in text
    assert "S03:" not in text


def test_all_of_without_the_parses_show_title_cleanly():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        episode("Ted Lasso", 3, 1, 1080),
        episode("Ted Lasso", 3, 2, 1080),
    ]

    class Search:
        def search(self, query, match_as=None):
            return []

    text = Service(library, Search(), _Deluge()).request("All of ted lasso tv show")
    assert "of ted lasso" not in text.casefold()
    assert 'for "ted lasso"' in text.casefold()
    assert "S03:" in text
    assert "Already on Plex at 1080p — Ted Lasso S03" in text
    assert "S03E01" not in text


def test_bare_all_of_title_asks_movie_or_show():
    library = Library(None, [])
    library.loaded = True
    result = Service(library, _Search([]), _Deluge()).request("all of ted lasso")
    assert isinstance(result, Clarify)


def test_collection_summary_counts_deluge_as_in_progress_not_owned():
    library = Library(None, [])
    library.loaded = True
    info = "ab" * 20
    deluge = _Deluge([Torrent(info, "Clerks.III.2022.1080p", "Downloading", 10, 0, 0)])
    members = [
        Identity("tt0109445", "Clerks", 1994, "movie"),
        Identity("tt11128440", "Clerks III", 2022, "movie"),
    ]
    library.copies = [movie("Clerks", 1994, 1080)]

    class Search:
        def search(self, query, match_as=None):
            return matching_hits(match_as or query, [hit("Clerks.III.2022.1080p", 12, info)])

    text = Service(library, Search(), deluge).request(
        "all of the clerks movies",
        collection_resolver=lambda name: members,
    )
    assert "already in Deluge" in text
    assert "already have all" not in text


def test_known_franchise_includes_titles_without_the_same_prefix():
    from plex_queue.imdb import known_collection

    catalog = {
        "tt0372784": Identity("tt0372784", "Batman Begins", 2005, "movie"),
        "tt0468569": Identity("tt0468569", "The Dark Knight", 2008, "movie"),
        "tt1345836": Identity("tt1345836", "The Dark Knight Rises", 2012, "movie"),
    }
    titles = [
        item.title
        for item in known_collection("dark knight", lambda imdb_id, timeout=20: catalog[imdb_id])
    ]
    assert titles == ["Batman Begins", "The Dark Knight", "The Dark Knight Rises"]


def test_bourne_franchise_lists_every_feature():
    from plex_queue.imdb import known_collection

    catalog = {
        "tt0258463": Identity("tt0258463", "The Bourne Identity", 2002, "movie"),
        "tt0372183": Identity("tt0372183", "The Bourne Supremacy", 2004, "movie"),
        "tt0440963": Identity("tt0440963", "The Bourne Ultimatum", 2007, "movie"),
        "tt1194173": Identity("tt1194173", "The Bourne Legacy", 2012, "movie"),
        "tt1232829": Identity("tt1232829", "Jason Bourne", 2016, "movie"),
    }
    titles = [
        item.title
        for item in known_collection("jason bourne", lambda imdb_id, timeout=20: catalog[imdb_id])
    ]
    assert titles == [
        "The Bourne Identity",
        "The Bourne Supremacy",
        "The Bourne Ultimatum",
        "The Bourne Legacy",
        "Jason Bourne",
    ]


def test_franchise_members_drop_unrelated_titles():
    from plex_queue.service import _franchise_members

    members = [
        Identity("tt0258463", "The Bourne Identity", 2002, "movie"),
        Identity("tt1232829", "Jason Bourne", 2016, "movie"),
        Identity("tt1232826", "21 Jump Street", 2012, "movie"),
    ]
    titles = [item.title for item in _franchise_members("jason bourne", members)]
    assert titles == ["The Bourne Identity", "Jason Bourne"]


def test_release_filename_matches_clean_title_query():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        copy_from_file(Path("D:/Movies/The.Bourne.Identity.2002.1080p.BluRay.mkv")),
    ]
    found = library.find(parse_request("movie The Bourne Identity 2002"))
    assert len(found) == 1
    assert "Bourne" in found[0].title


def test_a_bare_show_asks_for_a_season_instead_of_already_have():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        episode("Example Show", 1, 1, 1080),
        episode("Example Show", 1, 2, 480),
        episode("Example Show", 2, 1, 1080),
    ]

    class Search:
        def search(self, query, match_as=None):
            raise AssertionError(query)

    text = Service(library, Search(), _Deluge()).request("Example Show", kind="show")
    assert 'Found "Example Show" on Plex with 2 season(s)' in text
    assert "needs a season or episode" in text
    assert "S01" in text
    assert "S02" in text


def test_a_season_with_a_low_episode_queues_a_pack():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        episode("Example Show", 1, 1, 1080),
        episode("Example Show", 1, 2, 480),
    ]
    deluge = _Deluge()
    queries = []

    class Search:
        def search(self, query, match_as=None):
            queries.append(match_as or query)
            return matching_hits(match_as or query, [hit("Example.Show.S01.1080p.BluRay", 20, "ab" * 20)])

    text = Service(library, Search(), deluge).request("Example Show S01", kind="show")
    assert "Queued" in text or "higher-quality" in text
    assert deluge.added
    assert any("S01" in query for query in queries)


def test_a_complete_season_at_1080_is_left_alone():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        episode("Example Show", 1, 1, 1080, r"E:\Shows\e1.mkv"),
        episode("Example Show", 1, 2, 1080, r"E:\Shows\e2.mkv"),
    ]

    class Search:
        def search(self, query, match_as=None):
            raise AssertionError(query)

    text = Service(library, Search(), _Deluge()).request("Example Show season 1", kind="show")
    assert "Already on Plex" in text
    assert "season 1" in text


def test_kind_keeps_a_movie_and_a_show_apart():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        movie("Example Story", 2019, 1080),
        Copy("episode", "Pilot", 2019, "Example Story", 1, 1, 1080, "E:/Shows/pilot.mkv"),
    ]
    movies = library.find(parse_request("movie Example Story"))
    shows = library.find(parse_request("show Example Story"))
    assert [copy.kind for copy in movies] == ["movie"]
    assert [copy.kind for copy in shows] == ["episode"]


def test_service_replies_to_check_plex_and_does_not_add():
    library = Library(None, [])
    library.loaded = True
    library.copies = [movie("Example Story", 2019, 1080, r"J:\Movies2\Example Story.mkv")]
    deluge = _Deluge()
    service = Service(library, _Search([]), deluge)
    text = service.request("example story", kind="movie")
    assert "Check Plex" in text
    assert "1080p" in text
    assert deluge.added == []


def test_a_bare_title_asks_movie_or_show_and_does_not_search():
    library = Library(None, [])
    library.loaded = True
    library.copies = [movie("Example Story", 2019, 1080)]

    class Search:
        def search(self, query, match_as=None):
            raise AssertionError(query)

    result = Service(library, Search(), _Deluge()).request("example story")
    assert isinstance(result, Clarify)
    assert "IMDB" in result.text
    assert result.query == "example story"


def test_service_queues_when_the_library_copy_is_below_1080():
    library = Library(None, [])
    library.loaded = True
    library.copies = [movie("Example Story", 2019, 720)]
    deluge = _Deluge()
    service = Service(
        library,
        _Search([hit("Example.Story.2019.1080p.BluRay", 12, "ab" * 20)]),
        deluge,
    )
    text = service.request("Example Story", kind="movie")
    assert "higher-quality" in text
    assert deluge.added
    assert "ab" * 20 in deluge.added[0]


def test_service_does_not_add_a_hash_already_in_deluge():
    library = Library(None, [])
    library.loaded = True
    info = "cd" * 20
    deluge = _Deluge([Torrent(info, "Example.Story.2019.1080p", "Downloading", 10, 0, 0)])
    service = Service(library, _Search([hit("Example.Story.2019.1080p", 12, info)]), deluge)
    text = service.request("example story", kind="movie")
    assert "Already in Deluge" in text


def test_deluge_match_requires_the_same_season():
    library = Library(None, [])
    library.loaded = True
    s01 = "aa" * 20
    s02 = "bb" * 20
    deluge = _Deluge([Torrent(s01, "Ted.Lasso.S01.1080p", "Downloading", 10, 0, 0)])

    class Search:
        def search(self, query, match_as=None):
            q = (match_as or query).upper()
            if "S01" in q:
                return [hit("Ted.Lasso.S01.1080p", 40, s01)]
            if "S02" in q:
                return [hit("Ted.Lasso.S02.1080p", 40, s02)]
            return []

    service = Service(library, Search(), deluge)
    assert "Already in Deluge" in service.request("Ted Lasso S01", kind="show")
    text = service.request("Ted Lasso S02", kind="show")
    assert "Queued" in text
    assert "Already in Deluge" not in text
    assert s02 in deluge.added[0]


def test_all_of_show_notes_when_later_seasons_are_missing():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        episode("Ted Lasso", 1, 1, 1080),
        episode("Ted Lasso", 1, 2, 1080),
        episode("Ted Lasso", 2, 1, 1080),
        episode("Ted Lasso", 2, 2, 1080),
        episode("Ted Lasso", 3, 1, 1080),
        episode("Ted Lasso", 3, 2, 1080),
    ]

    class Search:
        def search(self, query, match_as=None):
            return []

    text = Service(library, Search(), _Deluge()).request("All of ted lasso tv show")
    assert "S03:" in text
    assert "S04:" not in text
    assert "No releases found after S03" in text


def test_season_match_keeps_episode_releases_with_extra_title_words():
    hits = [
        hit("Ted.Lasso.S04E01.1080p.WEB.H264-CAKES", 50, "a1" * 20),
        hit("Ted.Lasso.S04E02.Riches.1080p.WEB", 40, "a2" * 20),
        hit("Ted.Lasso.S03E01.1080p.WEB", 90, "a3" * 20),
        hit("Ted.Lasso.S04.1080p.WEBRip", 20, "a4" * 20),
    ]
    matched = matching_hits("ted lasso S04", hits)
    names = {item.name for item in matched}
    assert "Ted.Lasso.S04E01.1080p.WEB.H264-CAKES" in names
    assert "Ted.Lasso.S04E02.Riches.1080p.WEB" in names
    assert "Ted.Lasso.S04.1080p.WEBRip" in names
    assert "Ted.Lasso.S03E01.1080p.WEB" not in names


def test_season_request_prefers_a_pack_over_individual_episodes():
    library = Library(None, [])
    library.loaded = True
    pack = "p4" * 20
    ep = "e4" * 20
    deluge = _Deluge()

    class Search:
        def search(self, query, match_as=None):
            return matching_hits(
                match_as or query,
                [
                    hit("Ted.Lasso.S04E01.1080p.CAKES", 90, ep),
                    hit("Ted.Lasso.S04.1080p.WEBRip", 40, pack),
                ],
            )

    text = Service(library, Search(), deluge).request("Ted Lasso season 4", kind="show")
    assert "Queued" in text
    assert pack in deluge.added[0]
    assert len(deluge.added) == 1


def test_season_request_skips_low_pack_and_queues_episodes():
    library = Library(None, [])
    library.loaded = True
    e1 = "c1" * 20
    e2 = "c2" * 20
    deluge = _Deluge()

    class Search:
        def search(self, query, match_as=None):
            return matching_hits(
                match_as or query,
                [
                    hit("Ted.Lasso.S04.720p.WEBRip", 500, "c0" * 20),
                    hit("Ted.Lasso.S04E01.1080p.CAKES", 40, e1),
                    hit("Ted.Lasso.S04E02.1080p.CAKES", 30, e2),
                ],
            )

    text = Service(library, Search(), deluge).request("Ted Lasso S04", kind="show")
    assert "Queued 2 episode(s)" in text
    assert len(deluge.added) == 2
    assert e1 in "".join(deluge.added)
    assert e2 in "".join(deluge.added)


def test_episode_fallback_remembers_replace_path_for_weak_episode(tmp_path):
    from plex_queue.ledger import Ledger

    shows = tmp_path / "Shows"
    shows.mkdir()
    weak = shows / "Ted Lasso - S01E02.mkv"
    weak.write_bytes(b"sd")
    library = Library(None, [shows])
    library.loaded = True
    library.copies = [
        episode("Ted Lasso", 1, 1, 1080, str(shows / "e1.mkv")),
        episode("Ted Lasso", 1, 2, 480, str(weak)),
    ]
    digest = "d2" * 20
    ledger = Ledger(tmp_path / "imports.json")
    deluge = _Deluge()

    class Search:
        def search(self, query, match_as=None):
            return matching_hits(
                match_as or query,
                [
                    hit("Ted.Lasso.S01.720p", 500, "d0" * 20),
                    hit("Ted.Lasso.S01E02.1080p.CAKES", 40, digest),
                ],
            )

    text = Service(
        library, Search(), deluge, ledger=ledger, roots=[shows],
    ).request("Ted Lasso S01", kind="show")
    assert "Queued" in text
    assert digest in deluge.added[0]
    state = ledger.state(digest)
    assert state["replace_path"] == str(weak)
    assert state["replace_height"] == 480
    assert ledger.label(digest).episode == 2


def test_episode_fallback_remembers_replace_for_already_in_deluge(tmp_path):
    from plex_queue.ledger import Ledger
    from plex_queue.models import Torrent

    shows = tmp_path / "Shows"
    shows.mkdir()
    weak = shows / "Ted Lasso - S01E02.mkv"
    weak.write_bytes(b"sd")
    digest = "e2" * 20
    library = Library(None, [shows])
    library.loaded = True
    library.copies = [
        episode("Ted Lasso", 1, 1, 1080, str(shows / "e1.mkv")),
        episode("Ted Lasso", 1, 2, 480, str(weak)),
    ]
    ledger = Ledger(tmp_path / "imports.json")
    deluge = _Deluge([
        Torrent(digest, "Ted.Lasso.S01E02.1080p.CAKES", "Downloading", 10, 0, 0),
    ])

    class Search:
        def search(self, query, match_as=None):
            return matching_hits(
                match_as or query,
                [
                    hit("Ted.Lasso.S01.720p", 500, "e0" * 20),
                    hit("Ted.Lasso.S01E02.1080p.CAKES", 40, digest),
                ],
            )

    text = Service(
        library, Search(), deluge, ledger=ledger, roots=[shows],
    ).request("Ted Lasso S01", kind="show")
    assert "Already in Deluge" in text
    assert deluge.added == []
    state = ledger.state(digest)
    assert state["replace_path"] == str(weak)
    assert state["replace_height"] == 480


def test_season_request_queues_each_missing_episode_when_no_pack():
    library = Library(None, [])
    library.loaded = True
    # Gap at E02 so the season is not treated as complete.
    library.copies = [
        episode("Ted Lasso", 4, 1, 1080),
        episode("Ted Lasso", 4, 3, 1080),
    ]
    e2 = "b2" * 20
    e3 = "b3" * 20
    deluge = _Deluge()

    class Search:
        def search(self, query, match_as=None):
            return matching_hits(
                match_as or query,
                [
                    hit("Ted.Lasso.S04E01.1080p.CAKES", 50, "b1" * 20),
                    hit("Ted.Lasso.S04E02.1080p.CAKES", 40, e2),
                    hit("Ted.Lasso.S04E03.Riches.1080p", 30, e3),
                    hit("Ted.Lasso.S04E02.720p.CAKES", 99, "b9" * 20),
                ],
            )

    text = Service(library, Search(), deluge).request("Ted Lasso S04", kind="show")
    assert "Queued 1 episode(s)" in text
    assert "E02" in text
    assert len(deluge.added) == 1
    assert e2 in deluge.added[0]


def test_an_imdb_link_uses_the_resolved_title_and_kind():
    library = Library(None, [])
    library.loaded = True
    library.copies = [
        movie("Example Story", 2019, 1080, r"J:\Movies2\Example Story.mkv"),
        Copy("episode", "Pilot", 2014, "Example Story", 1, 1, 1080, "E:/Shows/pilot.mkv"),
    ]
    deluge = _Deluge()

    def resolver(imdb_id):
        return Identity(imdb_id, "Example Story", 2019, "movie")

    text = Service(library, _Search([]), deluge).request(
        "https://www.imdb.com/title/tt0000001/",
        resolver=resolver,
    )
    assert "Check Plex" in text
    assert "Movies2" in text
    assert deluge.added == []
    assert deluge.added == []


def test_only_the_owner_can_remove():
    library = Library(None, [])
    library.loaded = True
    deluge = _Deluge()

    class Access:
        def is_owner(self, user_id):
            return user_id == 7

    service = Service(library, _Search([]), deluge, access=Access())
    assert service.remove("abcdef", False, 99) == "Only the owner can do that."
    assert deluge.removed == []
    assert service.remove("abcdef", True, 7) == "removed"
    assert deluge.removed == [("abcdef", True)]


def test_removal_prefers_one_hash_prefix():
    torrents = [
        Torrent("abcdef1234567890", "One", "Paused", 100, 0, 0),
        Torrent("abc9991234567890", "Two", "Downloading", 10, 0, 0),
    ]
    chosen, error = pick_for_removal(torrents, "abcdef")
    assert error is None
    assert chosen is not None and chosen.name == "One"
    chosen, error = pick_for_removal(torrents, "abc")
    assert chosen is None
    assert error and "more than one" in error


class _Search:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, match_as=None):
        return matching_hits(match_as or query, self.hits)


class _Deluge:
    def __init__(self, torrents=None):
        self.torrents = torrents or []
        self.added = []
        self.removed = []

    def list_torrents(self):
        return list(self.torrents)

    def add_magnet(self, magnet):
        self.added.append(magnet)

    def remove(self, needle, delete_files):
        self.removed.append((needle, delete_files))
        return "removed"
