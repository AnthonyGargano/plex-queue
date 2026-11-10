from pathlib import Path

from plex_queue.errors import public_error
from plex_queue.fetch import SshFetch, TransferError, cat_command, confined_cat_script
from plex_queue.fetch import TransferError
from plex_queue.importer import Importer, _retire_conflicting, send_telegram
from plex_queue.ledger import Ledger
from plex_queue.place import CopyJob
from plex_queue.library import Library
from plex_queue.models import Hit, Torrent
from plex_queue.place import (
    Download,
    Label,
    RemoteFile,
    cleaned_title,
    confined,
    general_dirs,
    is_complete,
    parse_finished,
    plan,
    remote_join,
    safe_name,
    safe_relative,
    survey,
)
from plex_queue.plexscan import refresh_section, section_id
from plex_queue.service import Service


def test_general_dirs_skip_special_libraries():
    roots = [
        Path(r"D:\Movies4"),
        Path(r"G:\Kids Shows2"),
        Path(r"D:\Shows4"),
        Path(r"H:\Pure Pwnage"),
        Path(r"L:\Anime\Movies"),
    ]
    assert general_dirs(roots, "movie") == (Path(r"D:\Movies4"),)
    assert general_dirs(roots, "show") == (Path(r"D:\Shows4"),)


def test_cleaned_title_drops_the_release_tags():
    assert cleaned_title("movie The Matrix 1999") == "The Matrix"
    assert cleaned_title("Example.Show.S01E03.1080p.WEB.x264") == "Example Show"
    assert cleaned_title("Example.Show.S01.1080p.BluRay") == "Example Show"
    assert cleaned_title("Exit to Eden (1994) DVDRip.mkv") == "Exit to Eden"
    assert cleaned_title("Ted.Lasso.S01.1080p.WEBRip.x265-RARBG[eztv.re]") == "Ted Lasso"


def test_a_season_pack_lands_under_the_show_not_show_s01(tmp_path):
    download = Download("d" * 40, "Example.Show.S01.1080p", "/home/ttt/Downloads", (
        RemoteFile("Example.Show.S01E01.mkv", 4),
        RemoteFile("Example.Show.S01E02.mkv", 5),
    ))
    # Label title as /sort used to leave S01 in the name.
    jobs, problem = plan(
        download,
        Label("show", "Example Show S01", season=1),
        tmp_path,
    )
    assert problem is None
    assert [job.local for job in jobs] == [
        tmp_path / "Example Show" / "Season 01" / "Example.Show.S01E01.mkv",
        tmp_path / "Example Show" / "Season 01" / "Example.Show.S01E02.mkv",
    ]


def test_retire_conflicting_renames_with_underscore_number(tmp_path):
    path = tmp_path / "3_English.srt"
    path.write_text("old", encoding="utf-8")
    retired = _retire_conflicting(path)
    assert retired.name == "3_English_1.srt"
    assert retired.read_text(encoding="utf-8") == "old"
    assert not path.exists()
    path.write_text("newer", encoding="utf-8")
    again = _retire_conflicting(path)
    assert again.name == "3_English_2.srt"


def test_place_retires_subtitle_size_clash_but_not_video(tmp_path):
    movies = tmp_path / "Movies4"
    movies.mkdir()
    library = Library(None, [movies])
    library.loaded = True
    deluge = _Deluge([], set())
    transport = _Transport({})
    importer = _importer(tmp_path, deluge, transport, object(), [movies], library, 50 * 1024 ** 3)

    sub = movies / "Show" / "3_English.srt"
    sub.parent.mkdir(parents=True)
    sub.write_bytes(b"old-sub")
    transport.files["/remote/3_English.srt"] = b"new-subtitle-bytes"
    importer._place(CopyJob("/remote/3_English.srt", sub, len(b"new-subtitle-bytes")), movies)
    assert (movies / "Show" / "3_English_1.srt").read_bytes() == b"old-sub"
    assert sub.read_bytes() == b"new-subtitle-bytes"
    assert not (movies / "Show" / "3_English.srt.incoming").exists()

    video = movies / "Show" / "episode.mkv"
    video.write_bytes(b"old-video")
    transport.files["/remote/episode.mkv"] = b"new-video-bytes!!"
    try:
        importer._place(CopyJob("/remote/episode.mkv", video, len(b"new-video-bytes!!")), movies)
        assert False, "expected TransferError for video size clash"
    except TransferError as exc:
        assert "different size" in str(exc)
    assert video.read_bytes() == b"old-video"


def test_place_keeps_old_subtitle_if_fetch_fails(tmp_path):
    movies = tmp_path / "Movies4"
    movies.mkdir()
    library = Library(None, [movies])
    library.loaded = True
    importer = _importer(
        tmp_path, _Deluge([], set()), _Transport({}), object(),
        [movies], library, 50 * 1024 ** 3,
    )
    sub = movies / "Show" / "3_English.srt"
    sub.parent.mkdir(parents=True)
    sub.write_bytes(b"old-sub")
    try:
        importer._place(CopyJob("/missing/3_English.srt", sub, 12), movies)
        assert False, "expected TransferError"
    except Exception:
        pass
    assert sub.read_bytes() == b"old-sub"
    assert not (movies / "Show" / "3_English_1.srt").exists()


def test_completion_is_seeding_or_a_paused_finished_torrent():
    assert is_complete("Seeding", 100)
    assert is_complete("Paused", 100)
    assert is_complete("Queued", 100)
    assert is_complete("Queued", 99.9)
    assert not is_complete("Downloading", 100)
    assert not is_complete("Checking", 100)
    assert not is_complete("Paused", 40)
    assert not is_complete("Queued", 50)
    finished, hashes = parse_finished({
        "aa" * 20: {
            "name": "Partial", "state": "Downloading", "progress": 40,
            "save_path": "/home/ttt/Downloads", "files": [{"path": "a.mkv", "size": 3}],
        },
        "bb" * 20: {
            "name": "Done", "state": "Seeding", "progress": 100,
            "save_path": "/home/ttt/Downloads", "files": [{"path": "b.mkv", "size": 4}],
        },
    })
    assert hashes == {"aa" * 20, "bb" * 20}
    assert [item.name for item in finished] == ["Done"]


def test_remote_paths_cannot_leave_the_download_folder():
    from plex_queue.place import safe_save_path

    assert safe_relative("Show/S01E01.mkv") == "Show/S01E01.mkv"
    assert safe_relative("../.ssh/id.mkv") is None
    assert safe_relative("/etc/passwd.mkv") is None
    assert safe_relative(r"C:\Windows\file.mkv") is None
    assert safe_relative("folder/../../secret.mkv") is None
    assert remote_join("/home/ttt/Downloads", "../secret.mkv") is None
    assert remote_join("/home/ttt/Downloads", "Movie/file.mkv") == (
        "/home/ttt/Downloads/Movie/file.mkv"
    )
    assert safe_save_path("/home/ttt/Downloads/../.ssh") is None
    assert remote_join("/home/ttt/Downloads/../.ssh", "id.mkv") is None
    assert safe_save_path("/tmp/other", ("/home/ttt/Downloads",)) is None
    assert safe_save_path("/home/ttt/Downloads", ()) is None
    assert safe_save_path("/home/ttt/Downloads", None) is None


def test_dotdot_titles_stay_inside_the_library_root(tmp_path):
    assert safe_name("..") == "untitled"
    assert safe_name(".") == "untitled"
    root = tmp_path / "movies"
    root.mkdir()
    download = Download("a" * 40, "evil", "/home/ttt/Downloads", (RemoteFile("x.mkv", 5),))
    jobs, problem = plan(download, Label("movie", ".."), root)
    assert problem is None
    assert jobs
    assert confined(jobs[0].local, root)
    assert ".." not in jobs[0].local.parts


def test_a_traversal_torrent_path_is_refused(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    download = Download(
        "b" * 40,
        "evil",
        "/home/ttt/Downloads",
        (RemoteFile("../../../home/ttt/.ssh/id_ed25519.mkv", 5),),
    )
    jobs, problem = plan(download, Label("movie", "Example", 2019), root)
    assert jobs == []
    assert problem and "leaves the download folder" in problem


def test_movie_and_show_land_in_plex_folders(tmp_path):
    movie = Download("a" * 40, "Example.Story.2019.mkv", "/home/ttt/Downloads", (
        RemoteFile("Example.Story.2019.mkv", 5),
    ))
    jobs, problem = plan(movie, Label("movie", "Example Story", 2019), tmp_path)
    assert problem is None
    assert jobs[0].local == tmp_path / "Example Story (2019)" / "Example.Story.2019.mkv"
    show = Download("b" * 40, "pack", "/home/ttt/Downloads", (
        RemoteFile("Example.Show.S01E03.mkv", 4),
    ))
    jobs, problem = plan(show, Label("show", "Example Show"), tmp_path)
    assert problem is None
    assert jobs[0].local == tmp_path / "Example Show" / "Season 01" / "Example.Show.S01E03.mkv"
    jobs, problem = plan(
        Download("c" * 40, "loose", "/home/ttt/Downloads", (RemoteFile("episode.mkv", 4),)),
        Label("show", "Example Show"),
        tmp_path,
    )
    assert jobs == []
    assert "season" in problem


def test_plan_refuses_a_save_path_outside_the_download_root(tmp_path):
    download = Download(
        "a" * 40, "Example.mkv", "/tmp/other", (RemoteFile("Example.mkv", 5),),
    )
    jobs, problem = plan(download, Label("movie", "Example", 2019), tmp_path)
    assert jobs == []
    assert problem and "outside the download root" in problem


def test_space_check_picks_the_drive_that_can_hold_the_file(tmp_path):
    movies = tmp_path / "movies"
    other = tmp_path / "other"
    movies.mkdir()
    other.mkdir()
    free = {movies: 20 * 1024 ** 3, other: 3 * 1024 ** 3}
    report = survey([movies, other], 10 * 1024 ** 3, 2 * 1024 ** 3, lambda path: free[path])
    assert report.root == movies
    tight = survey([other], 10 * 1024 ** 3, 2 * 1024 ** 3, lambda path: free[path])
    assert tight.root is None
    assert "3.0 GB free" in tight.lines[0]


def test_copy_still_scans_when_vm_remove_fails(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    digest = "aa" * 20
    download = Download(digest, "Example.Story.2019.mkv", "/home/ttt/Downloads", (
        RemoteFile("Example.Story.2019.mkv", 5),
    ))
    plex = _Plex()
    transport = _Transport({"/home/ttt/Downloads/Example.Story.2019.mkv": b"hello"})

    class Deluge(_Deluge):
        def remove_hash(self, info_hash):
            raise RuntimeError("still seeding")

    deluge = Deluge([download], {digest})
    importer = _importer(tmp_path, deluge, transport, plex, [root], None, free=100 * 1024 ** 3)
    importer.ledger.remember(digest, Label("movie", "Example Story", 2019))
    notes = importer.cycle(notify=False)
    assert any("still on the VM" in note for note in notes)
    assert any("Asked Plex to scan" in note for note in notes)
    assert (root / "Example Story (2019)" / "Example.Story.2019.mkv").read_bytes() == b"hello"
    assert importer.ledger.state(digest)["status"] == "copied"
    assert importer.ledger.state(digest)["scanned"] is True


def test_missing_plex_token_does_not_mark_scanned(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    digest = "dd" * 20
    download = Download(digest, "Example.Story.2019.mkv", "/home/ttt/Downloads", (
        RemoteFile("Example.Story.2019.mkv", 5),
    ))
    transport = _Transport({"/home/ttt/Downloads/Example.Story.2019.mkv": b"hello"})
    deluge = _Deluge([download], {digest})
    importer = _importer(
        tmp_path, deluge, transport, _PlexNoToken(), [root], None, free=100 * 1024 ** 3,
    )
    importer.ledger.remember(digest, Label("movie", "Example Story", 2019))
    notes = importer.cycle(notify=False)
    assert any("token is missing" in note for note in notes)
    assert importer.ledger.state(digest)["status"] == "imported"
    assert not importer.ledger.state(digest).get("scanned")
    again = importer.cycle(notify=False)
    assert any("token is missing" in note for note in again)


def test_finished_movie_is_copied_removed_and_scanned(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    digest = "ab" * 20
    download = Download(digest, "Example.Story.2019.mkv", "/home/ttt/Downloads", (
        RemoteFile("Example.Story.2019.mkv", 5),
    ))
    library = Library(None, [])
    library.loaded = True
    plex = _Plex()
    transport = _Transport({"/home/ttt/Downloads/Example.Story.2019.mkv": b"hello"})
    deluge = _Deluge([download], {digest})
    importer = _importer(tmp_path, deluge, transport, plex, [root], library, free=100 * 1024 ** 3)
    importer.ledger.remember(digest, Label("movie", "Example Story", 2019))
    notes = importer.cycle(notify=False)
    assert any("Moved Example Story" in note for note in notes)
    assert any("Asked Plex to scan" in note for note in notes)
    assert (root / "Example Story (2019)" / "Example.Story.2019.mkv").read_bytes() == b"hello"
    assert deluge.removed == [digest]
    assert library.loaded is True
    assert any("Example.Story.2019" in copy.path for copy in library.copies)
    assert importer.ledger.state(digest)["status"] == "imported"


def test_copied_torrent_gone_from_deluge_does_not_claim_vm_cleanup(tmp_path):
    digest = "bb" * 20
    dest = tmp_path / "movies" / "Example"
    dest.mkdir(parents=True)
    importer = _importer(tmp_path, _Deluge([], set()), _Transport({}), _Plex(), [tmp_path], None, free=1)
    importer.ledger.mark(digest, status="copied", dest=str(dest), scanned=False)
    first = importer.cycle(notify=False)
    second = importer.cycle(notify=False)
    assert any("were not removed by me" in note for note in first)
    assert any("Asked Plex to scan" in note for note in first)
    assert second == []
    assert importer.ledger.state(digest)["status"] == "imported"


def test_still_removing_is_reported_once(tmp_path):
    digest = "cc" * 20
    dest = tmp_path / "movies" / "Example"
    dest.mkdir(parents=True)

    class Deluge(_Deluge):
        def remove_hash(self, info_hash):
            raise RuntimeError("Permission denied (publickey). /home/ttt/.ssh/id")

    importer = _importer(
        tmp_path, Deluge([], {digest}), _Transport({}), _Plex(), [tmp_path], None, free=1,
    )
    importer.ledger.mark(digest, status="copied", dest=str(dest), scanned=True)
    first = importer.cycle(notify=False)
    second = importer.cycle(notify=False)
    assert len(first) == 1
    assert "Still removing" in first[0]
    assert "SSH authentication failed." in first[0]
    assert "/home/ttt" not in first[0]
    assert second == []


def test_public_error_redacts_ssh_paths():
    assert public_error(RuntimeError("Permission denied (publickey)")) == "SSH authentication failed."
    assert "[path]" in public_error(RuntimeError("failed under /home/ttt/.ssh/id_ed25519"))
    assert "[path]" in public_error(RuntimeError(r"failed under C:\Users\garga\.ssh\id"))


def test_empty_download_roots_fail_closed_in_fetch():
    import pytest

    with pytest.raises(TransferError):
        confined_cat_script("/home/ttt/Downloads/a.mkv", [])
    with pytest.raises(TransferError):
        SshFetch(Path("key"), "ttt", "host", Path("known"), download_roots=())


def test_no_space_is_reported_once_and_the_file_stays(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    digest = "cd" * 20
    download = Download(digest, "Big.mkv", "/home/ttt/Downloads", (RemoteFile("Big.mkv", 50),))
    notes_sent = []
    deluge = _Deluge([download], {digest})
    importer = _importer(tmp_path, deluge, _Transport({}), _Plex(), [root], None, free=10)
    importer.notify = notes_sent.append
    importer.ledger.remember(digest, Label("movie", "Big", 2019))
    first = importer.cycle(notify=True)
    second = importer.cycle(notify=True)
    assert any("No movie drive has room" in note for note in first)
    assert second == []
    assert notes_sent and notes_sent[0].count("No movie drive") == 1
    assert deluge.removed == []


def test_an_unlabeled_download_asks_once(tmp_path):
    digest = "ef" * 20
    download = Download(digest, "Mystery.mkv", "/home/ttt/Downloads", (RemoteFile("Mystery.mkv", 5),))
    importer = _importer(
        tmp_path, _Deluge([download], {digest}), _Transport({}), _Plex(),
        [tmp_path], None, free=100 * 1024 ** 3,
    )
    first = importer.cycle(notify=False)
    second = importer.cycle(notify=False)
    assert "movie " + digest[:6] in first[0]
    assert "show " + digest[:6] in first[0]
    assert second == []


def test_vm_off_does_not_ask_deluge(tmp_path):
    class Deluge:
        def snapshot(self):
            raise AssertionError("snapshot")

    importer = _importer(tmp_path, Deluge(), _Transport({}), _Plex(), [tmp_path], None, free=1)
    importer.reachable = lambda: False
    assert importer.cycle(notify=False) == []


def test_a_short_copy_is_not_removed(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    digest = "aa" * 20
    download = Download(digest, "Example.mkv", "/home/ttt/Downloads", (RemoteFile("Example.mkv", 5),))
    deluge = _Deluge([download], {digest})
    importer = _importer(
        tmp_path, deluge, _Transport({"/home/ttt/Downloads/Example.mkv": b"nope"}),
        _Plex(), [root], None, free=100 * 1024 ** 3,
    )
    importer.ledger.remember(digest, Label("movie", "Example", 2019))
    notes = importer.cycle(notify=False)
    assert any("incomplete" in note for note in notes)
    assert deluge.removed == []
    assert not (root / "Example (2019)" / "Example.mkv").exists()


def test_label_reply_moves_a_finished_download(tmp_path):
    root = tmp_path / "movies"
    root.mkdir()
    digest = "abcdef1234567890"
    download = Download(digest, "Example.Story.2019.1080p.mkv", "/home/ttt/Downloads", (
        RemoteFile("Example.Story.2019.1080p.mkv", 5),
    ))
    deluge = _Deluge([download], {digest})
    deluge.torrents = [Torrent(digest, download.name, "Seeding", 100, 0, 0)]
    importer = _importer(
        tmp_path, deluge,
        _Transport({"/home/ttt/Downloads/Example.Story.2019.1080p.mkv": b"hello"}),
        _Plex(), [root], None, free=100 * 1024 ** 3,
    )
    text = importer.label_and_import(digest[:6], "movie")
    assert "Marked abcdef as a movie: Example Story" in text
    assert "Moved Example Story" in text
    assert deluge.removed == [digest]


def test_request_remembers_the_kind_for_later(tmp_path):
    library = Library(None, [])
    library.loaded = True
    ledger = Ledger(tmp_path / "imports.json")
    digest = "ab" * 20

    class Deluge:
        def list_torrents(self):
            return []

        def add_magnet(self, magnet):
            self.magnet = magnet

    class Search:
        def search(self, query, match_as=None):
            return [Hit("Example.Story.2019.1080p", digest, 12, 1)]

    Service(library, Search(), Deluge(), ledger=ledger).request("Example Story", kind="movie")
    label = ledger.label(digest)
    assert label is not None
    assert label.kind == "movie"
    assert label.title == "Example Story"


def test_plex_refresh_uses_the_matching_section_and_hides_the_token():
    sections = [(1, Path(r"D:\Movies4")), (2, Path(r"D:\Shows4"))]
    assert section_id(sections, Path(r"D:\Movies4\Example (2019)")) == 1
    assert section_id(sections, Path(r"E:\Other")) is None
    seen = {}

    class Response:
        def read(self):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["token"] = request.get_header("X-plex-token")
        return Response()

    refresh_section("http://127.0.0.1:32400", 1, Path(r"D:\Movies4\Example (2019)"), "secret-token", urlopen)
    assert seen["token"] == "secret-token"
    assert "secret-token" not in seen["url"]
    assert "/library/sections/1/refresh" in seen["url"]


def test_ssh_copy_quotes_spaces_and_checks_the_size(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("host ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestsOnly0000000000000000\n", encoding="utf-8")
    remote = "/home/ttt/Downloads/My File.mkv"
    command = cat_command("ssh", Path("key"), "ttt", "host", remote, known)
    script = command[-1]
    assert "python3 -c" in script
    assert "O_NOFOLLOW" in script
    assert "realpath" in script
    assert "'/home/ttt/Downloads/My File.mkv'" in script or '"/home/ttt/Downloads/My File.mkv"' in script
    assert "StrictHostKeyChecking=yes" in command
    assert any(str(known) in part for part in command)
    script = confined_cat_script(remote, ("/home/ttt/Downloads",))
    assert "/home/ttt/Downloads" in script
    assert "raise SystemExit(1)" in script

    def run(cmd, stdout, stderr, check):
        stdout.write(b"hello")

        class Completed:
            returncode = 0
            stderr = b""

        return Completed()

    local = tmp_path / "out.mkv"
    SshFetch(Path("key"), "ttt", "host", known, ssh="ssh", run=run).fetch(remote, local, 5)
    assert local.read_bytes() == b"hello"


def test_telegram_notify_is_skipped_without_an_owner():
    def urlopen(request, timeout):
        raise AssertionError("sent")

    send_telegram("token", 0, "hello", urlopen)


def test_library_note_files_avoids_full_refresh(tmp_path):
    from plex_queue.library import Library

    root = tmp_path / "movies"
    root.mkdir()
    library = Library(None, [root])
    library.loaded = True
    library.copies = []
    video = root / "Example Story (2019)" / "Example.Story.2019.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"hello")
    assert library.note_files([video.parent]) == 1
    assert library.loaded is True
    assert len(library.copies) == 1
    assert library.copies[0].title == "Example Story"


def test_ledger_batches_marks_until_flush(tmp_path):
    path = tmp_path / "imports.json"
    ledger = Ledger(path)
    digest = "ee" * 20
    ledger.mark(digest, status="copied", dest="D:/Movies")
    assert not path.exists()
    ledger.flush()
    assert path.exists()
    reloaded = Ledger(path)
    assert reloaded.state(digest)["status"] == "copied"


def test_search_hits_are_cached(monkeypatch):
    from plex_queue import search as search_mod

    search_mod._HITS_CACHE.clear()
    calls = []

    def pinned(url, timeout=30):
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

    monkeypatch.setattr(
        search_mod, "validate_search_endpoint", lambda endpoint, resolve=True: endpoint
    )
    monkeypatch.setattr(search_mod, "_https_get_json_pinned", pinned)
    first = search_mod.fetch_hits("Example Story", "https://example.test/q.php")
    second = search_mod.fetch_hits("Example Story", "https://example.test/q.php")
    assert len(calls) == 1
    assert first and first == second


def test_empty_search_results_are_not_cached(monkeypatch):
    from plex_queue import search as search_mod

    search_mod._HITS_CACHE.clear()
    calls = []

    def pinned(url, timeout=30):
        calls.append(1)
        return []

    monkeypatch.setattr(
        search_mod, "validate_search_endpoint", lambda endpoint, resolve=True: endpoint
    )
    monkeypatch.setattr(search_mod, "_https_get_json_pinned", pinned)
    assert search_mod.fetch_hits("Zzz Missing", "https://example.test/q.php") == []
    assert search_mod.fetch_hits("Zzz Missing", "https://example.test/q.php") == []
    assert len(calls) == 2


class _Plex:
    def refresh_dirs(self, directories):
        self.dirs = list(directories)
        notes = [f"Asked Plex to scan {directory}." for directory in directories]
        return notes, list(directories)


class _PlexNoToken:
    def refresh_dirs(self, directories):
        self.dirs = list(directories)
        return (
            ["Files are in place. Plex was not scanned because the server token is missing."],
            [],
        )


class _Transport:
    def __init__(self, files):
        self.files = files
        self.fetched = []

    def fetch(self, remote, local, size):
        self.fetched.append(remote)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(self.files[remote])


class _Deluge:
    def __init__(self, finished, hashes):
        self.finished = finished
        self.hashes = hashes
        self.removed = []
        self.torrents = []

    def snapshot(self):
        return list(self.finished), set(self.hashes)

    def remove_hash(self, info_hash):
        self.removed.append(info_hash)

    def list_torrents(self):
        return list(self.torrents)


def _importer(tmp_path, deluge, transport, plex, movies, library, free):
    return Importer(
        deluge=deluge,
        ledger=Ledger(tmp_path / "imports.json"),
        movie_dirs=movies,
        show_dirs=movies,
        margin=2 * 1024 ** 3,
        transport=transport,
        plex=plex,
        notify=lambda text: None,
        reachable=lambda: True,
        library=library,
        free_of=lambda path: free,
    )


def test_ledger_stamps_imported_at(tmp_path):
    ledger = Ledger(tmp_path / "imports.json")
    digest = "ab" * 20
    ledger.mark(digest, status="imported", dest=r"D:\Movies4\Example")
    state = ledger.state(digest)
    assert state is not None
    assert state["imported_at"]
    first = state["imported_at"]
    ledger.mark(digest, scanned=True)
    assert ledger.state(digest)["imported_at"] == first


def test_ledger_backfill_uses_dest_mtime(tmp_path):
    from datetime import datetime, timezone

    dest = tmp_path / "Movies4" / "Old Title"
    dest.mkdir(parents=True)
    old = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    import os
    os.utime(dest, (old, old))
    ledger = Ledger(tmp_path / "imports.json")
    digest = "cd" * 20
    ledger.mark(digest, status="copied", dest=str(dest))
    # Transition without an existing stamp should keep dest mtime, not "now".
    ledger.mark(digest, status="imported", dest=str(dest))
    stamped = ledger.state(digest)["imported_at"]
    assert stamped.startswith("2026-10-01")


def test_recent_lists_transfers_in_the_window(tmp_path):
    from datetime import datetime, timedelta, timezone

    ledger = Ledger(tmp_path / "imports.json")
    fresh = "aa" * 20
    old = "bb" * 20
    dest = tmp_path / "Movies4" / "Example Story (2019)"
    dest.mkdir(parents=True)
    ledger.remember(fresh, Label("movie", "Example Story", 2019))
    ledger.mark(
        fresh,
        status="imported",
        dest=str(dest),
        imported_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    ledger.remember(old, Label("movie", "Ancient Film", 1990))
    ledger.mark(
        old,
        status="imported",
        dest=str(tmp_path / "old"),
        imported_at=(datetime.now(timezone.utc) - timedelta(days=10))
        .replace(microsecond=0)
        .isoformat(),
    )
    library = Library(None, [])
    library.loaded = True
    service = Service(library, object(), _Deluge([], set()), ledger=ledger)
    day = service.recent("day", reveal_paths=True)
    assert "Example Story (2019)" in day
    assert str(dest) in day
    assert "Ancient Film" not in day
    week = service.recent("week", reveal_paths=True)
    assert "Example Story (2019)" in week
    assert "Ancient Film" not in week
    month = service.recent("month", reveal_paths=True)
    assert "Ancient Film (1990)" in month
    assert "Usage:" in service.recent("year")
    guest = service.recent("day", reveal_paths=False)
    assert "Example Story (2019)" in guest
    assert str(dest) not in guest
