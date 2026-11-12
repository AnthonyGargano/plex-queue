from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Copy:
    kind: str
    title: str
    year: int | None
    show: str | None
    season: int | None
    episode: int | None
    height: int | None
    path: str
    tokens: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        if self.kind == "episode" and self.show:
            code = ""
            if self.season and self.episode:
                code = f" S{self.season:02d}E{self.episode:02d}"
            return f"{self.show}{code}"
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title


@dataclass(frozen=True)
class Hit:
    name: str
    info_hash: str
    seeders: int
    size: int


@dataclass(frozen=True)
class Torrent:
    hash: str
    name: str
    state: str
    progress: float
    eta: int
    rate: int
    time_added: int = 0

    @property
    def short_id(self) -> str:
        return self.hash[:6].lower()


def format_rate(rate: int) -> str:
    if rate >= 1_000_000:
        return f"{rate / 1_000_000:.1f} MB/s"
    if rate >= 1_000:
        return f"{rate / 1_000:.0f} KB/s"
    return f"{rate} B/s"


def format_eta(seconds: int) -> str:
    if seconds <= 0:
        return ""
    hours, rem = divmod(int(seconds), 3600)
    minutes, _secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{_secs}s"


def torrent_line(torrent: Torrent) -> str:
    bits = [torrent.short_id, f"{torrent.progress:.0f}%"]
    if torrent.state.lower() == "downloading" and torrent.rate > 0:
        bits.append(format_rate(torrent.rate))
        eta = format_eta(torrent.eta)
        if eta:
            bits.append(eta)
    else:
        bits.append(torrent.state)
    bits.append(torrent.name)
    return "  ".join(bits)


def bucket(torrent: Torrent) -> str:
    if torrent.progress >= 99.9 or torrent.state.lower() == "seeding":
        return "done"
    if torrent.state.lower() == "downloading":
        return "active"
    return "queued"


def pick_for_removal(torrents: list[Torrent], needle: str) -> tuple[Torrent | None, str | None]:
    needle = needle.strip().lower()
    if not needle:
        return None, "Tell me the id from the list, or part of the name."
    if len(needle) >= 3:
        by_hash = [item for item in torrents if item.hash.lower().startswith(needle)]
        if len(by_hash) == 1:
            return by_hash[0], None
        if len(by_hash) > 1:
            return None, "That id matches more than one torrent. Use a longer id."
    by_name = [item for item in torrents if needle in item.name.lower()]
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        shown = "\n".join(torrent_line(item) for item in by_name[:8])
        return None, f"That matches more than one torrent:\n{shown}"
    return None, "Nothing in Deluge matches that."
