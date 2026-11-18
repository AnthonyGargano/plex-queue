import pytest

from plex_queue.library import Library
from plex_queue.models import Copy
from plex_queue.search import matching_hits
from plex_queue.service import Service
from plex_queue.vm import (
    DelugeError,
    GuestAddress,
    ensure_running,
    guest_answering,
    parse_guest_ips,
)
from plex_queue.models import Hit


class Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeVm:
    def __init__(self, states):
        self.states = list(states)
        self.started = 0
        self.resumed = 0

    def state(self):
        return self.states.pop(0)

    def start(self):
        self.started += 1

    def resume(self):
        self.resumed += 1


def ports(*answers):
    remaining = list(answers)

    def reachable(host, port):
        return remaining.pop(0)

    return reachable


def no_addresses(_name):
    return []


def test_reachable_guest_is_left_alone():
    vm = FakeVm(["Off"])
    status = ensure_running(
        "UbuntuClean", "127.0.0.1", control=vm, reachable=ports(True),
        sleep=lambda _s: None, clock=lambda: 0, timeout=5, discover=no_addresses,
    )
    assert status == "running"
    assert vm.started == 0


def test_off_vm_is_started_and_then_waited_on():
    clock = Clock()
    vm = FakeVm(["Off"])
    status = ensure_running(
        "UbuntuClean", "172.30.161.89", control=vm,
        reachable=ports(False, False, True),
        sleep=clock.sleep, clock=clock.time, timeout=30, discover=no_addresses,
    )
    assert status == "started"
    assert vm.started == 1


def test_paused_vm_is_resumed():
    clock = Clock()
    vm = FakeVm(["Paused"])
    status = ensure_running(
        "UbuntuClean", "172.30.161.89", control=vm,
        reachable=ports(False, False, True),
        sleep=clock.sleep, clock=clock.time, timeout=30, discover=no_addresses,
    )
    assert status == "started"
    assert vm.resumed == 1
    assert vm.started == 0


def test_running_vm_waits_for_ssh_without_starting():
    clock = Clock()
    vm = FakeVm(["Running"])
    status = ensure_running(
        "UbuntuClean", "172.30.161.89", control=vm,
        reachable=ports(False, False, True),
        sleep=clock.sleep, clock=clock.time, timeout=30, discover=no_addresses,
    )
    assert status == "running"
    assert vm.started == 0


def test_stopping_vm_is_not_started_until_it_is_off():
    clock = Clock()
    vm = FakeVm(["Stopping", "Off"])
    status = ensure_running(
        "UbuntuClean", "172.30.161.89", control=vm,
        reachable=ports(False, False, False, True),
        sleep=clock.sleep, clock=clock.time, timeout=30, discover=no_addresses,
    )
    assert status == "started"
    assert vm.started == 1


def test_timeout_reports_the_vm():
    clock = Clock()
    vm = FakeVm(["Off"])
    with pytest.raises(DelugeError, match="UbuntuClean"):
        ensure_running(
            "UbuntuClean", "172.30.161.89", control=vm,
            reachable=lambda host, port: False,
            sleep=clock.sleep, clock=clock.time, timeout=5, discover=no_addresses,
        )
    assert vm.started == 1


def test_changed_address_is_used_without_starting():
    vm = FakeVm(["Off"])
    address = GuestAddress("172.30.161.89")

    def reachable(host, port):
        return host == "172.23.33.12"

    status = ensure_running(
        "UbuntuClean", "172.30.161.89", control=vm, reachable=reachable,
        discover=lambda _name: ["not-an-ip", "169.254.1.1", "172.23.33.12"],
        sleep=lambda _s: None, clock=lambda: 0, timeout=5, address=address,
    )
    assert status == "running"
    assert vm.started == 0
    assert address.current == "172.23.33.12"


def test_off_vm_answers_on_the_new_address_after_start():
    clock = Clock()
    vm = FakeVm(["Off"])
    address = GuestAddress("172.30.161.89")
    seen = {"n": 0}

    def discover(_name):
        seen["n"] += 1
        if seen["n"] >= 3:
            return ["172.23.33.12"]
        return []

    def reachable(host, port):
        return host == "172.23.33.12" and seen["n"] >= 3

    status = ensure_running(
        "UbuntuClean", "172.30.161.89", control=vm, reachable=reachable,
        discover=discover, sleep=clock.sleep, clock=clock.time, timeout=30,
        address=address,
    )
    assert status == "started"
    assert vm.started == 1
    assert address.current == "172.23.33.12"


def test_guest_answering_accepts_a_discovered_address():
    def reachable(host, port):
        return host == "172.23.33.12"

    assert guest_answering(
        "UbuntuClean", "172.30.161.89", reachable=reachable,
        discover=lambda _name: ["172.23.33.12"],
    )
    assert not guest_answering(
        "UbuntuClean", "172.30.161.89", reachable=lambda *_: False,
        discover=no_addresses,
    )


def test_parse_guest_ips_keeps_real_addresses_only():
    assert parse_guest_ips(
        ["172.23.33.12", "172.23.33.12", "nope", "169.254.1.1", "127.0.0.1", "8.8.8.8"]
    ) == ["172.23.33.12", "8.8.8.8"]


def test_bad_vm_name_is_rejected():
    with pytest.raises(DelugeError):
        ensure_running("UbuntuClean; Stop-VM", "127.0.0.1", reachable=lambda *_: False)


def test_reply_says_when_the_vm_was_started():
    library = Library(None, [])
    library.loaded = True
    library.copies = [Copy("movie", "Example Story", 2019, None, None, None, 720, "x.mkv")]

    class Deluge:
        def __init__(self):
            self.boot_note = ""
            self.added = []

        def list_torrents(self):
            self.boot_note = "Started UbuntuClean."
            return []

        def add_magnet(self, magnet):
            self.added.append(magnet)

    search_hit = Hit("Example.Story.2019.1080p", "ab" * 20, 12, 1)
    service = Service(library, _Search([search_hit]), Deluge())
    text = service.request("Example Story", kind="movie")
    assert text.startswith("Started UbuntuClean.")
    assert "higher-quality" in text


class _Search:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, match_as=None):
        return matching_hits(match_as or query, self.hits)
