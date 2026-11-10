from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

from plex_queue.cache import TtlCache
from plex_queue.config import validate_search_endpoint
from plex_queue.models import Hit
from plex_queue.titles import franchise_match, parse_request, phrase_match, tokenize

TRACKER = "udp://tracker.opentrackr.org:1337/announce"
EMPTY_HASH = "0" * 40
_HITS_CACHE: TtlCache[list[Hit]] = TtlCache(900)


def matching_hits(query: str, hits: list[Hit]) -> list[Hit]:
    request = parse_request(query)
    if not request.tokens:
        return []
    matched = []
    for hit in hits:
        hit_words = tokenize(hit.name)
        if request.season is not None:
            hit_parsed = parse_request(hit.name)
            if hit_parsed.season != request.season:
                continue
            if (
                request.episode is not None
                and hit_parsed.episode is not None
                and hit_parsed.episode != request.episode
            ):
                continue
            # Episode names and release groups trail the show title; allow that
            # for season/episode requests (packs still match as a prefix).
            if not franchise_match(request.tokens, hit_words):
                continue
            matched.append(hit)
            continue
        if phrase_match(request.tokens, hit_words):
            matched.append(hit)
    return matched


def magnet_uri(hit: Hit) -> str:
    name = urllib.parse.quote(hit.name, safe="")
    tracker = urllib.parse.quote(TRACKER, safe="")
    return f"magnet:?xt=urn:btih:{hit.info_hash}&dn={name}&tr={tracker}"


def fetch_hits(
    query: str,
    endpoint: str,
    timeout: int = 30,
    *,
    match_as: str | None = None,
) -> list[Hit]:
    """Fetch torrents for ``query``, then keep hits matching ``match_as`` or ``query``.

    Season fills pass the bare show title as ``query`` (larger index page) and
    ``match_as=\"Show S04\"`` so episode releases are not dropped by a narrow API
    search string.
    """
    filter_query = match_as or query
    cache_key = (
        endpoint,
        " ".join(query.lower().split()),
        " ".join(filter_query.lower().split()),
    )
    cached = _HITS_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    # Re-check + resolve once; connect to that IP (DNS rebinding resistant).
    safe_endpoint = validate_search_endpoint(endpoint, resolve=True)
    url = safe_endpoint + "?" + urllib.parse.urlencode({"q": query})
    # Call the underscored name so tests can monkeypatch it without DNS.
    rows = _https_get_json_pinned(url, timeout=timeout)
    hits = []
    for row in rows:
        info_hash = str(row.get("info_hash") or "")
        if not info_hash or info_hash == EMPTY_HASH or str(row.get("id")) == "0":
            continue
        hits.append(
            Hit(
                name=str(row.get("name") or info_hash),
                info_hash=info_hash.lower(),
                seeders=int(row.get("seeders") or 0),
                size=int(row.get("size") or 0),
            )
        )
    matched = matching_hits(filter_query, hits)
    # Do not cache empty results: a transient empty API reply must not block
    # queuing for the full TTL.
    if matched:
        _HITS_CACHE.set(cache_key, matched)
    return list(matched)


def https_get_json_pinned(url: str, timeout: int = 30, *, label: str = "https"):
    """GET HTTPS JSON by connecting to a once-resolved public IP (SNI = hostname)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise urllib.error.URLError(f"{label} fetch requires https")
    host = parsed.hostname
    if not host:
        raise urllib.error.URLError(f"{label} fetch missing host")
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    ip = _pinned_public_ip(host, port, label=label)
    context = ssl.create_default_context()
    conn = _PinnedHTTPSConnection(host, ip, port, timeout=timeout, context=context)
    try:
        conn.request(
            "GET",
            path,
            headers={"Host": host, "User-Agent": "plex-queue", "Accept": "application/json"},
        )
        response = conn.getresponse()
        try:
            if 300 <= int(response.status) < 400:
                raise urllib.error.URLError(
                    f"{label} redirected ({response.status}); redirects are blocked"
                )
            if int(response.status) >= 400:
                raise urllib.error.HTTPError(
                    url, response.status, response.reason, response.headers, None
                )
            return json.loads(response.read().decode("utf-8"))
        finally:
            response.close()
    except (TimeoutError, OSError, ssl.SSLError) as exc:
        raise urllib.error.URLError(str(exc)) from exc
    finally:
        conn.close()


# Private alias so unit tests can monkeypatch without hitting real DNS.
_https_get_json_pinned = https_get_json_pinned


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that dials a pre-validated IP while keeping SNI hostname."""

    def __init__(self, host: str, ip: str, port: int, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pin_ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pin_ip, self.port), self.timeout)
        server_hostname = self.host if self._context else None
        self.sock = self._context.wrap_socket(raw, server_hostname=server_hostname)


def _pinned_public_ip(host: str, port: int, *, label: str = "https") -> str:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        _reject_special(ip, host, label=label)
        return str(ip)
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise urllib.error.URLError(f"{label} host could not be resolved: {host}") from exc
    for entry in answers:
        address = entry[4][0]
        try:
            candidate = ipaddress.ip_address(address)
        except ValueError:
            continue
        _reject_special(candidate, f"{host} -> {address}", label=label)
        return address
    raise urllib.error.URLError(f"{label} host could not be resolved: {host}")


def _reject_special(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, detail: str, *, label: str = "https",
) -> None:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise urllib.error.URLError(
            f"{label} must not target a private or special IP: {detail}"
        )
