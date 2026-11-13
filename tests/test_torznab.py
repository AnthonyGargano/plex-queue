from __future__ import annotations

import urllib.error

import pytest

from plex_queue import config as config_mod
from plex_queue import search as search_mod
from plex_queue import torznab as torznab_mod
from plex_queue.config import TorznabConfig
from plex_queue.models import Hit


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">
  <channel>
    <item>
      <title>Example.Show.S01.1080p.WEBRip</title>
      <guid>magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&amp;dn=Example</guid>
      <link>magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</link>
      <enclosure url="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" length="1000" type="application/x-bittorrent"/>
      <torznab:attr name="seeders" value="12"/>
      <torznab:attr name="infohash" value="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/>
      <torznab:attr name="size" value="1000"/>
    </item>
    <item>
      <title>Other.Show.S02.720p</title>
      <link>magnet:?xt=urn:btih:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb</link>
      <torznab:attr name="seeders" value="3"/>
    </item>
    <item>
      <title>No hash release</title>
      <link>https://example.test/download.torrent</link>
    </item>
  </channel>
</rss>
"""


def test_parse_torznab_xml_maps_items():
    hits = torznab_mod.parse_torznab_xml(SAMPLE_XML)
    assert len(hits) == 2
    assert hits[0].name == "Example.Show.S01.1080p.WEBRip"
    assert hits[0].info_hash == "a" * 40
    assert hits[0].seeders == 12
    assert hits[0].size == 1000
    assert hits[1].info_hash == "b" * 40
    assert hits[1].seeders == 3


def test_normalize_info_hash_accepts_base32():
    # 20 zero bytes → base32 "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    digest = torznab_mod.normalize_info_hash("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert digest == "00" * 20


def test_validate_torznab_config_loopback_only():
    ok = config_mod.validate_torznab_config(
        {
            "url": "http://127.0.0.1:19696/api/v2.0/indexers/all/results/torznab/api",
            "api_key": "secret-key-value",
            "local_port": 19696,
            "remote_port": 9117,
        }
    )
    assert ok is not None
    assert ok.url.startswith("http://127.0.0.1:19696/")
    assert ok.api_key == "secret-key-value"

    assert config_mod.validate_torznab_config(None) is None

    with pytest.raises(SystemExit, match="http://"):
        config_mod.validate_torznab_config(
            {
                "url": "https://127.0.0.1:19696/api",
                "api_key": "secret-key-value",
                "local_port": 19696,
                "remote_port": 9117,
            }
        )
    with pytest.raises(SystemExit, match="127.0.0.1"):
        config_mod.validate_torznab_config(
            {
                "url": "http://10.0.0.5:19696/api",
                "api_key": "secret-key-value",
                "local_port": 19696,
                "remote_port": 9117,
            }
        )
    with pytest.raises(SystemExit, match="local_port"):
        config_mod.validate_torznab_config(
            {
                "url": "http://127.0.0.1:19696/api",
                "api_key": "secret-key-value",
                "local_port": 19697,
                "remote_port": 9117,
            }
        )
    with pytest.raises(SystemExit, match="api_key"):
        config_mod.validate_torznab_config(
            {
                "url": "http://127.0.0.1:19696/api",
                "api_key": "PUT_TORZNAB_API_KEY_HERE",
                "local_port": 19696,
                "remote_port": 9117,
            }
        )
    with pytest.raises(SystemExit, match="deluge.ssh.local_port"):
        config_mod.validate_torznab_config(
            {
                "url": "http://127.0.0.1:15884/api",
                "api_key": "secret-key-value",
                "local_port": 15884,
                "remote_port": 9117,
            },
            ssh_local_port=15884,
        )


def test_search_hits_prefers_torznab_then_falls_back(monkeypatch):
    torznab = TorznabConfig(
        url="http://127.0.0.1:19696/api",
        api_key="secret-key-value",
        local_port=19696,
        remote_port=9117,
    )
    called = {"tunnel": 0, "torznab": 0, "apibay": 0}

    def ensure():
        called["tunnel"] += 1

    def fake_torznab(query, cfg, timeout=30, *, match_as=None):
        called["torznab"] += 1
        assert cfg is torznab
        return [
            Hit(name="Example.Show.S01.1080p", info_hash="ab" * 20, seeders=5, size=1),
        ]

    def fake_apibay(query, endpoint, timeout=30, *, match_as=None):
        called["apibay"] += 1
        return [Hit(name="fallback", info_hash="cd" * 20, seeders=1, size=1)]

    monkeypatch.setattr(torznab_mod, "fetch_torznab_hits", fake_torznab)
    monkeypatch.setattr(search_mod, "fetch_hits", fake_apibay)

    hits = search_mod.search_hits(
        "Example Show S01",
        "https://apibay.org/q.php",
        torznab=torznab,
        ensure_tunnel=ensure,
    )
    assert called == {"tunnel": 1, "torznab": 1, "apibay": 0}
    assert hits[0].info_hash == "ab" * 20

    def empty_torznab(*args, **kwargs):
        called["torznab"] += 1
        return []

    monkeypatch.setattr(torznab_mod, "fetch_torznab_hits", empty_torznab)
    called.update(tunnel=0, torznab=0, apibay=0)
    hits = search_mod.search_hits(
        "Missing Show",
        "https://apibay.org/q.php",
        torznab=torznab,
        ensure_tunnel=ensure,
    )
    assert called["tunnel"] == 1
    assert called["torznab"] == 1
    assert called["apibay"] == 1
    assert hits[0].name == "fallback"

    def boom(*args, **kwargs):
        called["torznab"] += 1
        raise urllib.error.URLError("down")

    monkeypatch.setattr(torznab_mod, "fetch_torznab_hits", boom)
    called.update(tunnel=0, torznab=0, apibay=0)
    hits = search_mod.search_hits(
        "Missing Show",
        "https://apibay.org/q.php",
        torznab=torznab,
        ensure_tunnel=ensure,
    )
    assert called["apibay"] == 1
    assert hits[0].name == "fallback"
