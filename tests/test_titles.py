from plex_queue.models import Hit
from plex_queue.place import cleaned_title
from plex_queue.search import matching_hits
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
    assert movie.collection is False


def test_tv_show_without_a_season_fills_the_series():
    show = parse_request("rocco's modern life tv show")
    assert show.kind == "show"
    assert show.collection is True
    truman = parse_request("The Truman Show tv show")
    assert truman.kind == "movie"
    assert truman.collection is False
    assert truman.tokens == ("truman", "show")
    truman_pack = parse_request("The Truman Show all tv shows")
    assert truman_pack.kind == "movie"
    assert truman_pack.collection is False
    watch_truman = parse_request("watch the truman show all tv shows")
    assert watch_truman.kind == "movie"
    assert watch_truman.collection is False
    assert watch_truman.tokens == ("truman", "show")
    assert parse_request("The Truman Show tv show all tv shows").kind == "movie"
    assert tokenize("rocko\u2019s modern life") == ["rocko", "modern", "life"]
    assert tokenize("can\u2019t") == ["cant"]
    assert show.collection is True
    assert show.season is None
    assert show.tokens == ("rocco", "modern", "life")
    assert show.franchise == "rocco's modern life"
    assert parse_request("tv show Breaking Bad").collection is True
    assert parse_request("Breaking Bad S01").collection is False
    assert parse_request("Breaking Bad S01").season == 1
    office = parse_request("show The Office")
    assert office.kind == "show"
    assert office.collection is True
    assert office.tokens == ("office",)


def test_a_title_that_ends_in_show_is_not_classified():
    # Known film "The Truman Show" is forced to movie; other *Show titles stay unclear.
    request = parse_request("The Truman Show")
    assert request.kind == "movie"
    assert request.collection is False
    assert request.tokens == ("truman", "show")
    late = parse_request("The Late Show")
    assert late.kind is None
    assert late.tokens == ("late", "show")


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
    watch_me = parse_request("watch all of me tv show")
    assert watch_me.collection is False
    assert watch_me.kind == "movie"
    assert watch_me.tokens == ("all", "of", "me")
    assert parse_request("all of me tv show").kind == "movie"


def test_trailing_all_seasons_and_tv_show_are_stripped():
    pack = parse_request("Perfect strangers tv show all seasons")
    assert pack.collection is True
    assert pack.kind == "show"
    assert pack.season is None
    assert pack.tokens == ("perfect", "strangers")
    assert pack.franchise == "Perfect strangers"
    assert cleaned_title("Perfect strangers tv show all seasons") == "Perfect strangers"

    # Bare trailing "all" (and typos like espisodes) also fill the show.
    bare_all = parse_request("mr ed tv show all")
    assert bare_all.collection is True
    assert bare_all.kind == "show"
    assert bare_all.tokens == ("mr", "ed")
    assert cleaned_title("mr ed tv show all") == "mr ed"
    typo = parse_request("mr ed tv show all espisodes")
    assert typo.collection is True
    assert typo.tokens == ("mr", "ed")
    # "Title all tv shows" word order.
    flipped = parse_request("mr ed all tv shows")
    assert flipped.collection is True
    assert flipped.kind == "show"
    assert flipped.tokens == ("mr", "ed")
    assert cleaned_title("mr ed all tv shows") == "mr ed"
    assert parse_request("clerks all movies").collection is True
    assert parse_request("clerks all movies").kind == "movie"
    # Do not treat "all of …" mid-phrase as a trailing pack cue.
    assert parse_request("watch all of the office").collection is False
    rescued = parse_request("watch all of the office all tv shows")
    assert rescued.collection is True
    assert rescued.kind == "show"
    assert rescued.tokens == ("office",)
    me = parse_request("all of me all tv shows")
    assert me.collection is False
    assert me.kind == "movie"
    watch_me = parse_request("watch all of me all tv shows")
    assert watch_me.collection is False
    assert watch_me.kind == "movie"
    bare_movie = parse_request("clerks all")
    assert bare_movie.collection is True
    assert bare_movie.kind is None
    assert cleaned_title("watch all of the office all tv shows") == "office"
    assert cleaned_title("all of me all tv shows") == "all of me"

    episodes = parse_request("The nanny tv show all episodes")
    assert episodes.collection is True
    assert episodes.kind == "show"
    assert episodes.season is None
    assert episodes.tokens == ("nanny",)
    assert episodes.franchise == "The nanny"
    assert cleaned_title("The nanny tv show all episodes") == "The nanny"
    assert parse_request("all episodes of The Nanny").collection is True
    assert parse_request("all episodes of The Nanny").tokens == ("nanny",)

    for phrase in (
        "The nanny tv show every season",
        "The nanny tv show each episode",
    ):
        req = parse_request(phrase)
        assert req.collection is True, phrase
        assert req.kind == "show", phrase
        assert req.tokens == ("nanny",), phrase
        assert cleaned_title(phrase) == "The nanny", phrase

    for phrase in (
        "every seasons of The Nanny",
        "each episode of The Nanny",
    ):
        req = parse_request(phrase)
        assert req.collection is True, phrase
        assert req.kind == "show", phrase
        assert req.tokens == ("nanny",), phrase
        assert cleaned_title(phrase) == "Nanny", phrase

    office_tv = parse_request("all seasons of the office tv show")
    assert office_tv.collection is True
    assert office_tv.kind == "show"
    assert office_tv.tokens == ("office",)
    assert office_tv.franchise == "office"
    assert cleaned_title("all seasons of the office tv show") == "office"

    season = parse_request("Perfect strangers tv show all seasons s01")
    assert season.collection is False
    assert season.kind == "show"
    assert season.season == 1
    assert season.tokens == ("perfect", "strangers")
    assert season.franchise is None

    # Kind before S01 without "all seasons" must match cleaned_title.
    bare = parse_request("Breaking Bad tv show S01")
    assert bare.tokens == ("breaking", "bad")
    assert bare.season == 1
    assert cleaned_title("Breaking Bad tv show S01") == "Breaking Bad"

    # Combined leading set + trailing all seasons keeps a clean franchise.
    office = parse_request("all of the office tv show all seasons")
    assert office.collection is True
    assert office.kind == "show"
    assert office.tokens == ("office",)
    assert office.franchise == "office"

    hit = Hit(
        name="Perfect.Strangers.S01.1080p.WEBRip.x265",
        info_hash="ab" * 20,
        seeders=10,
        size=1,
    )
    assert matching_hits("Perfect strangers tv show all seasons s01", [hit])
    assert matching_hits("Breaking Bad tv show S01", [
        Hit(name="Breaking.Bad.S01.1080p", info_hash="cd" * 20, seeders=5, size=1),
    ])
    assert franchise_match(("perfect", "strangers"), tokenize(hit.name))


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
