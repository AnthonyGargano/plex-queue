from plex_queue.titles import franchise_match, parse_request, phrase_match, screen_height, tokenize, video_height


def test_height_from_release_name():
    assert video_height("Movie.Name.2019.1080p.BluRay.x264") == 1080
    assert video_height("Movie.Name.2160p.UHD") == 2160
    assert video_height("Movie.Name.4K.HDR") == 2160
    assert video_height("Movie.Name.720p.WEBRip") == 720
    assert video_height("Movie Name") is None


def test_query_drops_articles_year_and_episode_code():
    request = parse_request("The Example Story 2019 S02E05")
    assert request.tokens == ("example", "story")
    assert request.year == 2019
    assert request.season == 2
    assert request.episode == 5


def test_imdb_link_is_kept_and_removed_from_the_title_words():
    request = parse_request("https://www.imdb.com/title/tt0133093/?ref_=nv_sr")
    assert request.imdb_id == "tt0133093"
    assert request.tokens == ()
    assert request.kind is None


def test_a_leading_kind_word_is_not_part_of_the_title():
    movie = parse_request("movie Inception")
    assert movie.kind == "movie"
    assert movie.tokens == ("inception",)
    show = parse_request("show The Office")
    assert show.kind == "show"
    assert show.tokens == ("office",)


def test_a_title_that_ends_in_show_is_not_classified():
    request = parse_request("The Truman Show")
    assert request.kind is None
    assert request.tokens == ("truman", "show")


def test_an_episode_code_means_a_show_unless_they_also_said_movie():
    assert parse_request("The Office S02E05").kind == "show"
    assert parse_request("movie The Office S02E05").kind is None


def test_a_bare_season_code_is_a_show_season():
    request = parse_request("Example Show S01")
    assert request.kind == "show"
    assert request.season == 1
    assert request.episode is None
    assert request.tokens == ("example", "show")


def test_episode_without_a_season_is_incomplete():
    request = parse_request("Example Show episode 3")
    assert request.season is None
    assert request.episode is None
    assert request.tokens == ("example", "show")


def test_kind_word_is_only_the_whole_reply():
    from plex_queue.titles import kind_word

    assert kind_word("movie") == "movie"
    assert kind_word("TV show") == "show"
    assert kind_word("movie Inception") is None


def test_widescreen_1080p_is_not_judged_by_the_short_side():
    assert screen_height(1920, 796) == 1080
    assert screen_height(1920, 800) == 1080
    assert screen_height(3840, 1600) == 2160
    assert screen_height(1280, 720) == 720
    assert screen_height(720, 480) == 480
    assert screen_height(None, 796) == 796


def test_probe_height_uses_width_for_widescreen(monkeypatch, tmp_path):
    from plex_queue import library as library_mod

    video = tmp_path / "wide.mkv"
    video.write_bytes(b"x")

    class Completed:
        stdout = "1920,800\n"
        returncode = 0

    monkeypatch.setattr(library_mod.shutil, "which", lambda name: "ffprobe")
    monkeypatch.setattr(
        library_mod.subprocess,
        "run",
        lambda *args, **kwargs: Completed(),
    )
    assert library_mod.probe_height(str(video)) == 1080


def test_a_trilogy_is_a_movie_set_named_by_its_first_word():
    request = parse_request("matrix trilogy")
    assert request.collection is True
    assert request.kind == "movie"
    assert request.tokens == ("matrix",)
    assert request.franchise == "matrix"
    assert request.collection_limit == 3
    assert franchise_match(request.tokens, tokenize("The Matrix Reloaded"))
    assert not phrase_match(("matrix",), tokenize("The Matrix Reloaded"))


def test_all_of_the_movies_names_the_franchise():
    clerks = parse_request("all of the clerks movies")
    assert clerks.collection is True
    assert clerks.kind == "movie"
    assert clerks.franchise == "clerks"
    assert clerks.collection_limit is None
    stories = parse_request("all of the toy stories")
    assert stories.franchise == "toy story"
    assert stories.tokens == ("toy", "story")
    assert parse_request("all of me").collection is False


def test_all_of_a_tv_show_is_a_show_season_set():
    office = parse_request("All of the office tv show")
    assert office.collection is True
    assert office.kind == "show"
    assert office.franchise == "office"
    assert office.tokens == ("office",)
    # No "the" after "of" must not leave a leading "of" in the title.
    lasso = parse_request("All of ted lasso tv show")
    assert lasso.kind == "show"
    assert lasso.franchise == "ted lasso"
    assert lasso.tokens == ("ted", "lasso")
    seasons = parse_request("all seasons of The Office")
    assert seasons.collection is True
    assert seasons.kind == "show"
    assert seasons.tokens == ("office",)
    series = parse_request("all the office series")
    assert series.kind == "show"
    assert series.franchise == "office"
    # Bare "all of X" is a set, but movie vs show stays unclear.
    bare = parse_request("all of ted lasso")
    assert bare.collection is True
    assert bare.kind is None
    assert bare.franchise == "ted lasso"
    # Single-word "all of me" (with or without a kind word) is the film title, not a set.
    assert parse_request("all of me movie").collection is False
    assert parse_request("all of me tv show").collection is False
    assert parse_request("all of me").collection is False


def test_sequel_numbers_keep_toy_story_apart_from_its_sequels():
    assert tokenize("Toy Story 2") == ["toy", "story", "2"]
    assert tokenize("Clerks II") == ["clerks", "2"]
    assert not phrase_match(("toy", "story"), tokenize("Toy Story 2"))
    assert phrase_match(("toy", "story", "2"), tokenize("Toy Story 2"))
    assert phrase_match(("clerks",), tokenize("Clerks"))
    assert not phrase_match(("clerks",), tokenize("Clerks II"))


def test_phrase_rejects_a_sequel_and_allows_a_franchise_prefix():
    matrix = tokenize("The Matrix")
    reloaded = tokenize("The Matrix Reloaded 2003 1080p BluRay")
    awakens = tokenize("Star Wars The Force Awakens 2015 1080p")
    assert phrase_match(("matrix",), matrix)
    assert not phrase_match(("matrix",), reloaded)
    assert phrase_match(("matrix", "reloaded"), reloaded)
    assert phrase_match(("force", "awakens"), awakens)
    assert not phrase_match(("office",), tokenize("Office Space 1999"))
    assert phrase_match(("office",), tokenize("The Office"))
