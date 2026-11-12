from __future__ import annotations

import logging
import stat
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from plex_queue.errors import public_error
from plex_queue.fetch import TransferError
from plex_queue.models import pick_for_removal
from plex_queue.place import (
    SUBTITLE_EXTENSIONS,
    Label,
    cleaned_title,
    common_parent,
    confined,
    gb,
    parse_finished,
    plan,
    survey,
    under_roots,
)
from plex_queue.titles import parse_request
from plex_queue.boost import clear_boost_siblings
from plex_queue.upgrades import (
    path_key,
    plex_new_height,
    plex_new_is_best,
    plex_paths_known,
    plex_share_item,
    video_locals,
)

log = logging.getLogger(__name__)


class Importer:
    def __init__(
        self,
        deluge,
        ledger,
        movie_dirs,
        show_dirs,
        margin: int,
        transport,
        plex,
        notify,
        reachable,
        library=None,
        vm_name: str = "UbuntuClean",
        free_of=None,
        interval: int = 600,
        download_roots=None,
        plex_db=None,
        min_height: int = 1080,
        library_roots=None,
        booster=None,
    ):
        self.deluge = deluge
        self.ledger = ledger
        self.movie_dirs = list(movie_dirs)
        self.show_dirs = list(show_dirs)
        self.margin = margin
        self.transport = transport
        self.plex = plex
        self.notify = notify
        self.reachable = reachable
        self.library = library
        self.vm_name = vm_name
        self.free_of = free_of or _free_bytes
        self.interval = interval
        self.plex_db = Path(plex_db) if plex_db else None
        self.min_height = min_height
        self.library_roots = tuple(Path(root) for root in (library_roots or ()))
        # Optional callable() -> str for stuck-torrent auto-boost at cycle end.
        self.booster = booster
        if download_roots is None:
            self.download_roots = ("/home/ttt/Downloads",)
        else:
            roots = tuple(root for root in download_roots if root)
            if not roots:
                raise ValueError("download_roots cannot be empty")
            self.download_roots = roots
        self._lock = threading.Lock()

    def serve(self, stop: threading.Event) -> None:
        if stop.wait(20):
            return
        while not stop.is_set():
            try:
                self.cycle(notify=True)
            except Exception:
                log.exception("import cycle failed")
            if stop.wait(self.interval):
                return

    def cycle(self, notify: bool = True) -> list[str]:
        with self._lock:
            notes = self._cycle()
        if notify and notes:
            self._notify(notes)
        return notes

    def label_and_import(self, needle: str, kind: str) -> str:
        with self._lock:
            text = self._apply_label(needle, kind)
            notes = self._cycle()
        return "\n\n".join(part for part in [text, *notes] if part)

    def _cycle(self) -> list[str]:
        from plex_queue.events import emit

        try:
            return self._cycle_body()
        except Exception as exc:
            # Keep partial progress (ledger flush still runs); surface a useful note
            # so /import does not only say "Something went wrong."
            log.exception("import cycle failed")
            emit("import.cycle", status="fail", error=public_error(exc))
            return [f"Import cycle stopped early: {public_error(exc)}"]
        finally:
            self.ledger.flush()

    def _cycle_body(self) -> list[str]:
        if not self.reachable():
            log.info("import skipped; %s is not reachable", self.vm_name)
            from plex_queue.events import emit

            emit("import.cycle", status="skip", reason="vm_unreachable")
            return []
        finished, hashes = self.deluge.snapshot()
        notes = []
        scan_dirs: list[Path] = []
        for info_hash, state in self.ledger.states().items():
            status = state.get("status")
            dest = state.get("dest")
            if dest and status in {"copied", "imported"} and not state.get("scanned"):
                scan_dirs.append(Path(dest))
            if status == "copied" and dest:
                if info_hash in hashes:
                    sibling_notes = clear_boost_siblings(
                        self.ledger, self.deluge, info_hash,
                    )
                    try:
                        self.deluge.remove_hash(info_hash)
                    except Exception as exc:
                        note = self._once(
                            info_hash,
                            "copied",
                            f"Still removing the VM copy of {dest}: {public_error(exc)}",
                            dest=dest,
                        )
                        if note:
                            notes.append(note)
                        notes.extend(sibling_notes)
                        continue
                    self.ledger.mark(info_hash, status="imported", dest=dest)
                    notes.append(f"Removed the VM copy. Files are in {dest}.")
                    notes.extend(sibling_notes)
                else:
                    # Torrent vanished from Deluge without a successful remove this cycle.
                    sibling_notes = clear_boost_siblings(
                        self.ledger, self.deluge, info_hash,
                    )
                    self.ledger.mark(info_hash, status="imported", dest=dest)
                    note = self._once(
                        info_hash,
                        "imported",
                        f"Files are in {dest}. The torrent is gone from Deluge; "
                        "any leftover files on the VM were not removed by me.",
                        dest=dest,
                    )
                    if note:
                        notes.append(note)
                    notes.extend(sibling_notes)
        for download in finished:
            note, folders = self._one(download)
            if note:
                notes.append(note)
            scan_dirs.extend(folders)
        if scan_dirs:
            scan_notes, refreshed = self.plex.refresh_dirs(scan_dirs)
            notes.extend(scan_notes)
            refreshed_roots = set()
            for path in refreshed:
                try:
                    refreshed_roots.add(path.resolve())
                except OSError:
                    continue
            for info_hash, state in self.ledger.states().items():
                dest = state.get("dest")
                if not dest or state.get("scanned"):
                    continue
                try:
                    if Path(dest).resolve() in refreshed_roots:
                        self.ledger.mark(info_hash, scanned=True)
                except OSError:
                    continue
            if self.library is not None:
                # Fold new files into the index; avoid a full multi-drive walk.
                self.library.note_files(scan_dirs)
        notes.extend(self._promote_upgrades())
        # Retry sibling cleanup only while a boost_group remains (failed remove).
        for info_hash, state in self.ledger.states().items():
            if state.get("status") not in {"imported", "copied"}:
                continue
            if not state.get("boost_group"):
                continue
            notes.extend(
                clear_boost_siblings(self.ledger, self.deluge, info_hash)
            )
        if self.booster is not None:
            try:
                boost_note = self.booster()
            except Exception as exc:
                log.exception("auto-boost failed")
                boost_note = f"Auto-boost failed: {public_error(exc)}"
            if boost_note:
                notes.append(boost_note)
        if not notes:
            pending = sum(
                1 for state in self.ledger.states().values()
                if state.get("upgrade_status") == "pending"
            )
            waiting = sum(
                1 for state in self.ledger.states().values()
                if state.get("upgrade_status") in {
                    "awaiting_import", "awaiting_plex", "needs_video",
                }
            )
            if pending:
                notes.append(
                    f"Nothing new to transfer. {pending} upgrade(s) ready for "
                    "/upgrades → /drop (or /keep)."
                )
            elif waiting:
                notes.append(
                    f"Nothing new to transfer. {waiting} upgrade(s) still waiting "
                    "on Plex confirmation — /upgrades."
                )
        log.info("import cycle finished (%s note(s))", len(notes))
        from plex_queue.events import emit

        emit("import.cycle", status="ok", notes=len(notes))
        return notes

    def _one(self, download) -> tuple[str | None, list[Path]]:
        state = self.ledger.state(download.hash) or {}
        if state.get("status") == "imported":
            return None, []
        if state.get("status") == "copied" and state.get("dest"):
            # Still waiting on VM remove; only re-queue a Plex scan if the last one failed.
            folders = [] if state.get("scanned") else [Path(state["dest"])]
            return None, folders
        label = self.ledger.label(download.hash)
        if label is None:
            return self._once(
                download.hash,
                "waiting_label",
                "Finished, and I don't know whether it is a movie or a show:\n"
                f"{download.hash[:6]}  {download.name}\n"
                f"Reply: movie {download.hash[:6]}\n"
                f"or: show {download.hash[:6]}",
            ), []
        roots = self.movie_dirs if label.kind == "movie" else self.show_dirs
        if not roots:
            return self._once(download.hash, "needs_detail", f"No {label.kind} folders are configured."), []
        probe_root = Path(roots[0])
        jobs, problem = plan(download, label, probe_root, self.download_roots)
        if problem:
            return self._once(download.hash, "needs_detail", problem), []
        needed = sum(job.size for job in jobs)
        report = survey(roots, needed, self.margin, self.free_of)
        if report.root is None:
            listed = "\n".join(report.lines)
            return self._once(
                download.hash,
                "waiting_space",
                f"{label.title} is ready ({gb(needed)}). "
                f"No {label.kind} drive has room for it with {gb(self.margin)} left over.\n"
                f"{listed}\nLeft it on the VM.",
            ), []
        if report.root != probe_root:
            jobs, problem = plan(download, label, report.root, self.download_roots)
            if problem or not jobs:
                return self._once(download.hash, "needs_detail", problem or "Nothing to copy."), []
        elif not jobs:
            return self._once(download.hash, "needs_detail", "Nothing to copy."), []
        try:
            for job in jobs:
                self._place(job, report.root)
        except (TransferError, OSError) as exc:
            return self._once(
                download.hash, "failed",
                f"Could not copy {label.title}: {public_error(exc)}",
            ), []
        dest = str(common_parent([job.local.parent for job in jobs]))
        upgrade_fields = self._upgrade_fields(download.hash, jobs)
        upgrade_note = self._upgrade_owner_note(download.hash, label.title, upgrade_fields)
        self.ledger.mark(
            download.hash, status="copied", dest=dest, scanned=False, **upgrade_fields,
        )
        sibling_notes = clear_boost_siblings(self.ledger, self.deluge, download.hash)
        try:
            self.deluge.remove_hash(download.hash)
        except Exception as exc:
            text = (
                f"Copied {label.title} to {dest}, and it's still on the VM: {public_error(exc)}"
            )
            if upgrade_note:
                text = f"{text}\n{upgrade_note}"
            if sibling_notes:
                text = f"{text}\n" + "\n".join(sibling_notes)
            return self._once(
                download.hash,
                "copied",
                text,
                dest=dest,
                scanned=False,
                **upgrade_fields,
            ), [Path(dest)]
        self.ledger.mark(
            download.hash, status="imported", dest=dest, scanned=False, **upgrade_fields,
        )
        moved = f"Moved {label.title} to {dest}.\nRemoved the copy on the VM."
        if upgrade_note:
            moved = f"{moved}\n{upgrade_note}"
        if sibling_notes:
            moved = f"{moved}\n" + "\n".join(sibling_notes)
        return moved, [Path(dest)]

    def _place(self, job, root: Path) -> None:
        if not confined(job.local, root):
            raise TransferError(f"Refusing to write outside {root}.")
        existing = _regular_size(job.local)
        if existing is not None:
            if existing == job.size:
                return
            if job.local.suffix.lower() not in SUBTITLE_EXTENSIONS:
                raise TransferError(
                    f"{job.local.name} is already in the library at a different size."
                )
            # Fetch the new subtitle first; only then rename the old one so a
            # failed transfer does not clear the library copy.
            incoming = job.local.with_name(job.local.name + ".incoming")
            _refuse_planted_path(incoming)
            retired = None
            try:
                self.transport.fetch(job.remote, incoming, job.size)
                _require_regular_file(incoming, job.size, job.local.name)
                retired = _retire_conflicting(job.local)
                incoming.replace(job.local)
            except Exception:
                incoming.unlink(missing_ok=True)
                if (
                    retired is not None
                    and _regular_size(job.local) is None
                    and _regular_size(retired) is not None
                ):
                    retired.rename(job.local)
                raise
            _require_regular_file(job.local, job.size, job.local.name)
            if not confined(job.local, root):
                job.local.unlink(missing_ok=True)
                raise TransferError(f"Refusing to leave a file outside {root}.")
            return
        _refuse_planted_path(job.local)
        self.transport.fetch(job.remote, job.local, job.size)
        try:
            _require_regular_file(job.local, job.size, job.local.name)
        except TransferError:
            job.local.unlink(missing_ok=True)
            raise
        if not confined(job.local, root):
            job.local.unlink(missing_ok=True)
            raise TransferError(f"Refusing to leave a file outside {root}.")

    def _apply_label(self, needle: str, kind: str) -> str:
        if kind not in {"movie", "show"}:
            return "Say movie or show."
        chosen, error = pick_for_removal(self.deluge.list_torrents(), needle)
        if error or chosen is None:
            return error or "Nothing in Deluge matches that."
        parsed = parse_request(chosen.name)
        title = cleaned_title(chosen.name) or chosen.name
        self.ledger.remember(
            chosen.hash,
            Label(kind, title, parsed.year, parsed.season, parsed.episode),
        )
        return (
            f"Marked {chosen.short_id} as a {kind}: {title}. "
            "I'll move it when it has finished and a drive has room."
        )

    def _upgrade_fields(self, info_hash: str, jobs) -> dict:
        state = self.ledger.state(info_hash) or {}
        replace_path = state.get("replace_path")
        if not replace_path:
            return {}
        roots = self.library_roots or tuple(self.movie_dirs + self.show_dirs)
        if roots and not under_roots(Path(replace_path), roots):
            return {"upgrade_status": "kept", "upgrade_note": "replace_outside_roots"}
        videos = video_locals(jobs)
        if not videos:
            return {
                "upgrade_status": "needs_video",
                "upgrade_note": "no_main_video",
            }
        return {
            "new_paths": videos,
            "upgrade_status": "awaiting_plex",
        }

    def _promote_upgrades(self) -> list[str]:
        notes = []
        roots = self.library_roots or tuple(self.movie_dirs + self.show_dirs)
        awaiting = [
            (info_hash, state)
            for info_hash, state in self.ledger.states().items()
            if state.get("upgrade_status") in {
                "awaiting_import", "awaiting_plex", "needs_video",
            }
        ]
        if awaiting and (self.plex_db is None or not self.plex_db.exists()):
            note = (
                "Upgrade review is waiting, but the Plex database is missing. "
                "Set library.plex_db so I can confirm the new file before /upgrades."
            )
            once = self._once("__upgrade_plex_db__", "needs_plex_db", note)
            if once:
                notes.append(once)
            return notes
        # Best pending upgrade per old library file: digest -> measured new height.
        claimed: dict[str, tuple[str, int]] = {}
        for digest, state in self.ledger.states().items():
            if state.get("upgrade_status") != "pending":
                continue
            replace = state.get("replace_path")
            if not replace:
                continue
            height = int(state.get("new_height") or 0)
            claimed[path_key(replace)] = (digest, height)
        for info_hash, state in awaiting:
            status = state.get("upgrade_status")
            replace_path = state.get("replace_path")
            if not replace_path:
                continue
            old_key = path_key(replace_path)
            label = self.ledger.label(info_hash)
            title = label.title if label else info_hash[:6]
            if roots and not under_roots(Path(replace_path), roots):
                if not state.get("upgrade_note_sent"):
                    notes.append(
                        f"Upgrade for {title}: old file is outside library roots, "
                        "so I will not offer /drop for it."
                    )
                self.ledger.mark(
                    info_hash, upgrade_status="kept", upgrade_note_sent=True,
                )
                continue
            if not Path(replace_path).is_file():
                self.ledger.mark(info_hash, upgrade_status="gone")
                continue
            if state.get("status") not in {"imported", "copied"}:
                continue
            new_paths = list(state.get("new_paths") or [])
            # Legacy rows stored only the largest episode; expand once from dest
            # without pulling the old replace_path (still on disk until /drop).
            if state.get("dest") and len(new_paths) <= 1:
                old_key = path_key(replace_path) if replace_path else ""
                recovered = [
                    path for path in _videos_under(Path(state["dest"]))
                    if path_key(path) != old_key
                ]
                if recovered and len(recovered) > len(new_paths):
                    new_paths = recovered
                    self.ledger.mark(info_hash, new_paths=new_paths)
            if not new_paths:
                if not state.get("upgrade_note_sent"):
                    notes.append(
                        f"Upgrade for {title}: imported, but I could not find a "
                        f"main video file to confirm in Plex. "
                        f"Use /keep {info_hash[:6]} if needed."
                    )
                self.ledger.mark(
                    info_hash,
                    upgrade_status="needs_video",
                    upgrade_note_sent=True,
                )
                continue
            replace_height = state.get("replace_height")
            if isinstance(replace_height, int):
                old_height_hint = replace_height
            else:
                old_height_hint = None
            if not plex_new_is_best(
                self.plex_db,
                new_paths,
                self.min_height,
                replace_path=replace_path,
                replace_height=old_height_hint,
            ):
                # Promote import/recovery states into awaiting_plex; do not
                # relabel an already-waiting row, and never invent awaiting_plex
                # from an unrelated status.
                if status in {"awaiting_import", "needs_video"}:
                    self.ledger.mark(info_hash, upgrade_status="awaiting_plex")
                if not state.get("upgrade_mismatch_noted"):
                    known = plex_paths_known(self.plex_db, new_paths + [replace_path])
                    new_known = any(path_key(path) in known for path in new_paths)
                    old_known = path_key(replace_path) in known
                    share = plex_share_item(self.plex_db, replace_path, new_paths)
                    if new_known and not old_known:
                        notes.append(
                            f"Upgrade for {title} is stuck: the new file is in Plex, "
                            f"but the old library path is not. "
                            f"Check Plex versions, then /keep {info_hash[:6]}."
                        )
                        self.ledger.mark(
                            info_hash,
                            upgrade_status="awaiting_plex",
                            upgrade_mismatch_noted=True,
                        )
                    elif new_known and old_known and not share:
                        notes.append(
                            f"Upgrade for {title} is stuck: Plex has both files on "
                            f"different items, so I cannot confirm the upgrade. "
                            f"Merge them in Plex or /keep {info_hash[:6]}."
                        )
                        self.ledger.mark(
                            info_hash,
                            upgrade_status="awaiting_plex",
                            upgrade_mismatch_noted=True,
                        )
                    elif new_known and old_known and share:
                        measured = plex_new_height(
                            self.plex_db, new_paths, replace_path=replace_path,
                        )
                        if measured and measured >= self.min_height:
                            detail = (
                                f"Plex still prefers another version over the new "
                                f"{measured}p copy"
                            )
                        elif measured:
                            detail = (
                                f"the new copy is only {measured}p in Plex "
                                f"(need {self.min_height}p+)"
                            )
                        else:
                            detail = (
                                "Plex has not reported a usable resolution for the "
                                "new copy yet"
                            )
                        notes.append(
                            f"Upgrade for {title} is stuck: both files are on the same "
                            f"Plex item, but {detail}. "
                            f"Wait for analysis or /keep {info_hash[:6]}."
                        )
                        self.ledger.mark(
                            info_hash,
                            upgrade_status="awaiting_plex",
                            upgrade_mismatch_noted=True,
                        )
                continue
            new_height = plex_new_height(
                self.plex_db, new_paths, replace_path=replace_path,
            ) or self.min_height
            existing = claimed.get(old_key)
            if existing is not None:
                other_hash, other_height = existing
                if new_height <= other_height:
                    if not state.get("upgrade_note_sent"):
                        notes.append(
                            f"Upgrade for {title}: kept pending {other_hash[:6]} "
                            f"({other_height}p) over {info_hash[:6]} ({new_height}p) "
                            f"for the same old file."
                        )
                    self.ledger.mark(
                        info_hash, upgrade_status="gone", upgrade_note_sent=True,
                    )
                    continue
                self.ledger.mark(other_hash, upgrade_status="gone")
                notes.append(
                    f"Upgrade for {title}: replaced pending {other_hash[:6]} "
                    f"({other_height}p) with better {info_hash[:6]} ({new_height}p)."
                )
            claimed[old_key] = (info_hash, new_height)
            self.ledger.mark(
                info_hash,
                upgrade_status="pending",
                new_height=new_height,
            )
            if label and label.year:
                title = f"{title} ({label.year})"
            old_height = state.get("replace_height") or "?"
            notes.append(
                f"Upgrade ready for review: {info_hash[:6]}  {title} "
                f"({old_height}p → {new_height}p). /upgrades then /drop or /keep."
            )
        return notes

    def _upgrade_owner_note(self, info_hash: str, title: str, fields: dict) -> str | None:
        status = fields.get("upgrade_status")
        if status == "kept" and fields.get("upgrade_note") == "replace_outside_roots":
            self.ledger.mark(info_hash, upgrade_note_sent=True)
            return (
                f"Upgrade for {title}: old file is outside library roots, "
                "so I will not offer /drop for it."
            )
        if status == "needs_video":
            self.ledger.mark(info_hash, upgrade_note_sent=True)
            return (
                f"Upgrade for {title}: copied, but I could not find a main video "
                "file to confirm in Plex."
            )
        return None

    def _once(self, info_hash: str, status: str, note: str, **extra) -> str | None:
        current = self.ledger.state(info_hash) or {}
        if current.get("status") == status and current.get("note") == note:
            return None
        self.ledger.mark(info_hash, status=status, note=note, **extra)
        return note

    def _notify(self, notes: list[str]) -> None:
        try:
            self.notify("\n\n".join(notes))
        except Exception as exc:
            log.warning("owner notification failed: %s", type(exc).__name__)


def _refuse_planted_path(path: Path) -> None:
    """Clear a leftover regular file, but refuse symlinks before a fetch write."""
    try:
        info = path.lstat()
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise TransferError(f"Refusing to write through a symlink at {path.name}.")
    if not stat.S_ISREG(info.st_mode):
        raise TransferError(f"Refusing to overwrite a non-file at {path.name}.")
    path.unlink()


def _regular_size(path: Path) -> int | None:
    """Return size for a regular file; refuse symlinks; None if missing."""
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise TransferError(f"Refusing to write through a symlink at {path.name}.")
    if not stat.S_ISREG(info.st_mode):
        raise TransferError(f"Refusing to overwrite a non-file at {path.name}.")
    return int(info.st_size)


def _require_regular_file(path: Path, size: int, label: str) -> None:
    """Post-write check with lstat so a swapped symlink cannot pass."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise TransferError(f"{label} arrived incomplete.") from exc
    if stat.S_ISLNK(info.st_mode):
        raise TransferError(f"Refusing to leave a symlink at {label}.")
    if not stat.S_ISREG(info.st_mode) or int(info.st_size) != size:
        raise TransferError(f"{label} arrived incomplete.")


def _retire_conflicting(path: Path) -> Path:
    """Rename an existing file to stem_N.suffix so a newer copy can take its place."""
    stem = path.stem
    suffix = path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            path.rename(candidate)
            return candidate
        n += 1


def snapshot_from(status: dict):
    return parse_finished(status)


def send_telegram(token: str, chat_id: int, text: str, urlopen=None) -> None:
    if not token or not chat_id or not text:
        return
    opener = urlopen or urllib.request.urlopen
    payload = urllib.parse.urlencode({"chat_id": str(chat_id), "text": text[:4000]}).encode()
    # Bot token must live in the path for Telegram's HTTP API; never log this URL.
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    try:
        with opener(request, timeout=30) as response:
            response.read()
    except Exception as exc:
        # Avoid leaking the bot token via urllib's URL-bearing exceptions.
        raise RuntimeError(f"Telegram notify failed: {type(exc).__name__}") from None


def _free_bytes(path: Path) -> int:
    import shutil

    return shutil.disk_usage(path).free


def _is_sample_or_trailer(stem: str) -> bool:
    text = stem.lower()
    return "trailer" in text or text.endswith("sample") or ".sample" in text


def _videos_under(folder: Path) -> list[str]:
    from plex_queue.upgrades import VIDEO_EXTENSIONS

    if not folder.exists():
        return []
    found = []
    if folder.is_file():
        if folder.suffix.lower() in VIDEO_EXTENSIONS and not _is_sample_or_trailer(folder.stem):
            return [str(folder)]
        return []
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if _is_sample_or_trailer(path.stem):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        found.append((size, str(path)))
    if not found:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _size, path in found]
