from __future__ import annotations

import base64
import http.client
import logging
import re
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

from plex_queue.cache import TtlCache
from plex_queue.config import TorznabConfig
from plex_queue.models import Hit
from plex_queue.search import matching_hits

log = logging.getLogger(__name__)

_HITS_CACHE: TtlCache[list[Hit]] = TtlCache(900)
_BTIH = re.compile(
    r"(?i)(?:urn:btih:|infohash=)([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})\b"
)
_HEX40 = re.compile(r"^[a-fA-F0-9]{40}$")
_TORZNAB_NS = "http://torznab.com/schemas/2015/feed"


def fetch_torznab_hits(
    query: str,
    torznab: TorznabConfig,
    timeout: int = 30,
    *,
    match_as: str | None = None,
) -> list[Hit]:
    """Fetch Torznab RSS for ``query``, then filter with ``match_as`` or ``query``."""
    filter_query = match_as or query
    cache_key = (
        torznab.url,
        " ".join(query.lower().split()),
        " ".join(filter_query.lower().split()),
    )
    cached = _HITS_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    xml_text = _http_get_loopback(
        torznab.url,
        {"t": "search", "q": query, "apikey": torznab.api_key},
        timeout=timeout,
    )
    hits = parse_torznab_xml(xml_text)
    matched = matching_hits(filter_query, hits)
    if matched:
        _HITS_CACHE.set(cache_key, matched)
    return list(matched)


def parse_torznab_xml(text: str) -> list[Hit]:
    """Map Torznab/RSS ``item`` nodes to abstract ``Hit`` values."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise urllib.error.URLError(f"torznab XML parse failed: {exc}") from exc
    hits: list[Hit] = []
    seen: set[str] = set()
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        hit = _item_to_hit(item)
        if hit is None or hit.info_hash in seen:
            continue
        seen.add(hit.info_hash)
        hits.append(hit)
    return hits


def normalize_info_hash(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if _HEX40.fullmatch(text):
        return text.lower()
    # Base32 infohashes (32 chars) → 20-byte hex.
    if len(text) == 32 and re.fullmatch(r"[a-zA-Z2-7]+", text):
        try:
            raw = base64.b32decode(text.upper())
        except Exception:
            return None
        if len(raw) == 20:
            return raw.hex()
    match = _BTIH.search(text)
    if match:
        return normalize_info_hash(match.group(1))
    return None


def _item_to_hit(item: ET.Element) -> Hit | None:
    title = ""
    size = 0
    seeders = 0
    info_hash = None
    blobs: list[str] = []
    for child in list(item):
        name = _local(child.tag)
        if name == "title":
            title = (child.text or "").strip()
        elif name == "size":
            size = _int(child.text)
        elif name in {"link", "guid"}:
            blobs.append((child.text or "").strip())
        elif name == "enclosure":
            blobs.append((child.attrib.get("url") or "").strip())
            if not size:
                size = _int(child.attrib.get("length"))
        elif name == "attr" and _is_torznab_attr(child):
            attr = (child.attrib.get("name") or "").strip().lower()
            value = (child.attrib.get("value") or "").strip()
            if attr in {"infohash", "info_hash"}:
                info_hash = normalize_info_hash(value) or info_hash
            elif attr == "seeders":
                seeders = _int(value)
            elif attr == "size" and not size:
                size = _int(value)
    if info_hash is None:
        for blob in blobs:
            info_hash = normalize_info_hash(blob)
            if info_hash:
                break
    if not info_hash:
        return None
    return Hit(
        name=title or info_hash,
        info_hash=info_hash,
        seeders=seeders,
        size=size,
    )


def _is_torznab_attr(node: ET.Element) -> bool:
    if node.tag.startswith("{"):
        return node.tag.startswith("{" + _TORZNAB_NS + "}")
    # Some feeds omit the namespace on attr children.
    return _local(node.tag) == "attr"


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _int(value) -> int:
    try:
        return int(float(str(value or "0").strip() or "0"))
    except (TypeError, ValueError):
        return 0


def _http_get_loopback(url: str, params: dict[str, str], timeout: int) -> str:
    """GET http://127.0.0.1 only; no redirects; never log query (contains api key)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "http":
        raise urllib.error.URLError("torznab fetch requires http loopback")
    host = (parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost"}:
        raise urllib.error.URLError("torznab fetch host must be loopback")
    port = parsed.port
    if port is None:
        raise urllib.error.URLError("torznab fetch requires an explicit port")
    path = parsed.path or "/"
    query = urllib.parse.urlencode(params)
    request_path = f"{path}?{query}"
    # http.client DEBUG logs the full request line (would include apikey).
    http_log = logging.getLogger("http.client")
    previous_level = http_log.level
    http_log.setLevel(logging.WARNING)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.set_debuglevel(0)
    try:
        conn.request(
            "GET",
            request_path,
            headers={
                "Host": f"127.0.0.1:{port}",
                "User-Agent": "plex-queue",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            },
        )
        response = conn.getresponse()
        try:
            status = int(response.status)
            if 300 <= status < 400:
                raise urllib.error.URLError(
                    f"torznab redirected ({status}); redirects are blocked"
                )
            body = response.read()
            if status >= 400:
                raise urllib.error.HTTPError(
                    f"http://127.0.0.1:{port}{path}",
                    status,
                    response.reason,
                    response.headers,
                    None,
                )
            return body.decode("utf-8", "replace")
        finally:
            response.close()
    except (TimeoutError, OSError) as exc:
        raise urllib.error.URLError(str(exc)) from exc
    finally:
        conn.close()
        http_log.setLevel(previous_level)
