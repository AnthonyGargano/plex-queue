from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import urllib.error
from collections import defaultdict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from plex_queue.access import Access
from plex_queue.config import Config
from plex_queue.deluge_remote import DelugeError
from plex_queue.errors import public_error
from plex_queue.service import Clarify, Service
from plex_queue.titles import kind_word, parse_request
from plex_queue.vm import guest_reachable

log = logging.getLogger(__name__)

HELP = """\
Send a title, or an IMDB link such as https://www.imdb.com/title/tt0133093/

A link tells me the exact movie or show. A bare title asks you to pick movie or TV show before I check Plex. A season or episode, like S01 or S01E03, is treated as a show. A bare show name lists what you have and asks for a season before anything is queued.

/active  downloads in progress
/queue   waiting, paused, and checking
/done    finished
/recent  transferred into library folders (past 24 hours)
/recent week  or /recent month
/status  counts
/add <title>  same as sending the title

A copy at 1080p or higher gets a reply to check Plex. Widescreen files count as 1080p when the picture is 1920 wide, even if the frame is shorter.
"All of the Clerks movies" or a trilogy lists every movie found, keeps ones already at 1080p, and queues missing or lower-quality ones.
"All of the Office tv show" or "all seasons of The Office" checks each season, keeps complete 1080p seasons, and queues the rest.
A lower copy queues a higher-quality one when a release exists.
A title you don't have is queued.

Finished downloads are copied to the movie or show drive with enough free space, removed from the VM, and Plex is scanned.
After Plex is serving the new higher-quality file, the owner can /upgrades and /drop the old copy to the Recycle Bin (not hard-deleted).
"""

CHOOSE = InlineKeyboardMarkup(
    [[
        InlineKeyboardButton("Movie", callback_data="kind:movie"),
        InlineKeyboardButton("TV show", callback_data="kind:show"),
    ]]
)

OWNER_HELP = """\
/rm <id>  remove from Deluge and keep the files
/rmdata <id>  remove from Deluge and delete the files
/scan  reread Plex and the drives
/upgrades  lower-quality copies ready to retire after a confirmed upgrade
/drop <id>  move that old copy to the Recycle Bin (Windows)
/keep <id>  leave the old copy and clear it from the list
/tidy  audit library (duplicates, orphans, misplaced, layout)
/tidy apply <id>  run one proposed fix
/tidy apply dupes  trash strong (Plex-linked) duplicates
/tidy apply renames 50   /tidy apply moves 1-20
/tidy apply renames all  /tidy skip renames all
/tidy orphans  ask Plex to scan orphan folders
/tidy skip <id>  dismiss a proposal
/import  run one import cycle now (copy finished downloads, scan Plex)
/allow <id>  let someone request titles
/deny <id>
/users  who is allowed
/sort <id> movie
/sort <id> show
"""


class Limiter:
    def __init__(self) -> None:
        self._hits: dict[int, list[float]] = defaultdict(list)

    def allow(self, user_id: int, limit: int, window: int = 3600, cost: int = 1) -> bool:
        now = time.monotonic()
        recent = [stamp for stamp in self._hits[user_id] if now - stamp < window]
        need = max(1, int(cost))
        if len(recent) + need > limit:
            self._hits[user_id] = recent
            return False
        recent.extend([now] * need)
        self._hits[user_id] = recent
        return True


LABEL_REPLY = re.compile(
    r"^(movie|film|show|series|tv(?: show)?)\s+([0-9a-f]{6,40})$",
    re.IGNORECASE,
)


def build_application(config: Config, service: Service, access: Access, importer=None) -> Application:
    limiter = Limiter()
    claim_user_limiter = Limiter()
    claim_global_limiter = Limiter()
    pending: dict[int, str] = {}
    app = Application.builder().token(config.token).build()

    def user_id(update: Update) -> int | None:
        user = update.effective_user
        return user.id if user else None

    async def reply(update: Update, text: str, markup=None) -> None:
        message = update.effective_message
        if not message:
            return
        # Telegram hard limit is 4096; chunk so batch tidy results are not silently cut.
        limit = 3500
        body = text or ""
        if len(body) <= limit:
            await message.reply_text(body, reply_markup=markup)
            return
        chunks = []
        start = 0
        while start < len(body):
            end = min(start + limit, len(body))
            if end < len(body):
                split = body.rfind("\n", start, end)
                if split > start + 500:
                    end = split + 1
            chunks.append(body[start:end])
            start = end
        for index, chunk in enumerate(chunks):
            suffix = f"\n…({index + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            await message.reply_text(
                (chunk + suffix)[:4000],
                reply_markup=markup if index == 0 else None,
            )

    async def allowed(update: Update, owner_only: bool = False) -> bool:
        uid = user_id(update)
        if uid is None:
            return False
        if owner_only and not access.is_owner(uid):
            await reply(update, "Only the owner can do that.")
            return False
        if not access.is_allowed(uid):
            await reply(update, f"You're not on the list yet. Your id is {uid}.")
            return False
        return True

    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = user_id(update)
        text = HELP
        if uid and access.is_owner(uid):
            text += "\n" + OWNER_HELP
        await reply(update, text)

    async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = user_id(update)
        await reply(update, f"Your id is {uid}.")

    async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = user_id(update)
        if uid is None:
            return
        given = " ".join(context.args).strip()
        if not given:
            await reply(update, "Usage: /claim <setup_secret>")
            return
        if not config.setup_secret_ready:
            await reply(
                update,
                "Set a private setup_secret (16+ characters) in config.yaml, then restart the bot.",
            )
            return
        # Per-account and global buckets slow down guessing of setup_secret.
        if not claim_user_limiter.allow(uid, 3):
            await reply(update, "Too many claim attempts this hour. Try again later.")
            return
        if not claim_global_limiter.allow(0, 8):
            await reply(update, "Too many claim attempts this hour. Try again later.")
            return
        # Digest both sides so length differences do not short-circuit compare_digest.
        expected = hashlib.sha256(config.setup_secret.encode("utf-8")).digest()
        provided = hashlib.sha256(given.encode("utf-8")).digest()
        if not hmac.compare_digest(expected, provided):
            await reply(update, "That claim secret is wrong.")
            return
        await reply(update, access.claim(uid))

    async def allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        if not context.args or not context.args[0].isdigit():
            await reply(update, "Usage: /allow 123456")
            return
        await reply(update, access.allow(int(context.args[0])))

    async def deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        if not context.args or not context.args[0].isdigit():
            await reply(update, "Usage: /deny 123456")
            return
        target = int(context.args[0])
        pending.pop(target, None)
        await reply(update, access.deny(target))

    async def users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        await reply(update, access.roster())

    async def listing(which: str, update: Update) -> None:
        if not await allowed(update):
            return
        await _starting(update)
        await reply(update, await _work(lambda: service.listing(which)))

    async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update):
            return
        await _starting(update)
        await reply(update, await _work(service.counts))

    async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update):
            return
        window = "day"
        if context.args:
            window = context.args[0]
        uid = user_id(update) or 0
        reveal = access.is_owner(uid)
        await reply(
            update,
            await _work(lambda: service.recent(window, reveal_paths=reveal)),
        )

    async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        await reply(update, "Reading Plex and the drives...")
        await reply(update, await _work(service.scan))

    async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE, delete_files: bool) -> None:
        if not await allowed(update, owner_only=True):
            return
        if not context.args:
            await reply(update, "Usage: /rm <id>")
            return
        needle = " ".join(context.args)
        uid = user_id(update) or 0
        await _starting(update)
        await reply(update, await _work(lambda: service.remove(needle, delete_files, uid)))

    async def upgrades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        uid = user_id(update) or 0
        await reply(update, await _work(lambda: service.upgrades(uid)))

    async def drop_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        if not context.args:
            await reply(update, "Usage: /drop <id>")
            return
        needle = " ".join(context.args)
        uid = user_id(update) or 0
        await reply(update, await _work(lambda: service.drop_upgrade(needle, uid)))

    async def keep_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        if not context.args:
            await reply(update, "Usage: /keep <id>")
            return
        needle = " ".join(context.args)
        uid = user_id(update) or 0
        await reply(update, await _work(lambda: service.keep_upgrade(needle, uid)))

    async def tidy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        uid = user_id(update) or 0
        args = [part.lower() for part in (context.args or [])]
        if not args:
            await reply(update, "Auditing the library...")
            await reply(update, await _work(lambda: service.tidy(uid, refresh=True)))
            return
        verb = args[0]
        rest = " ".join(context.args[1:]).strip() if context.args and len(context.args) > 1 else ""
        if verb in {"apply", "do"}:
            needle = rest or (args[1] if len(args) > 1 else "")
            if not needle and len(args) > 1:
                needle = args[1]
            await reply(update, "Working on tidy apply...")
            await reply(update, await _work(lambda: service.tidy_apply(needle, uid), tidy=True))
            return
        if verb in {"skip", "keep"}:
            if not rest:
                await reply(
                    update,
                    "Usage: /tidy skip <id>\n/tidy skip renames all",
                )
                return
            await reply(update, await _work(lambda: service.tidy_skip(rest, uid), tidy=True))
            return
        if verb in {"orphans", "orphan"}:
            await reply(update, "Working on orphan scans...")
            await reply(update, await _work(lambda: service.tidy_apply("orphans", uid), tidy=True))
            return
        if verb in {"refresh", "audit"}:
            await reply(update, "Auditing the library...")
            await reply(update, await _work(lambda: service.tidy(uid, refresh=True), tidy=True))
            return
        await reply(
            update,
            "Usage: /tidy\n"
            "/tidy apply <id>\n"
            "/tidy apply dupes\n"
            "/tidy apply renames 50\n"
            "/tidy apply moves 1-20\n"
            "/tidy orphans\n"
            "/tidy skip <id>\n"
            "/tidy skip renames all",
        )

    async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = " ".join(context.args)
        await _request(update, query)

    async def plain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.message.text if update.message and update.message.text else ""
        uid = user_id(update) or 0
        chosen = kind_word(query)
        if chosen and uid in pending:
            await _request(update, pending[uid], chosen)
            return
        labeled = LABEL_REPLY.fullmatch(query.strip())
        if labeled and importer is not None and access.is_owner(uid):
            kind = "movie" if labeled.group(1).lower() in {"movie", "film"} else "show"
            await _sort(update, kind, labeled.group(2))
            return
        await _request(update, query)

    async def sort_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        parsed = _sort_args(context.args)
        if parsed is None:
            await reply(update, "Usage: /sort <id> movie")
            return
        kind, needle = parsed
        await _sort(update, kind, needle)

    async def import_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await allowed(update, owner_only=True):
            return
        if importer is None:
            await reply(update, "Moving finished downloads is not set up.")
            return
        await _starting(update)
        await reply(update, "Running an import cycle...")
        notes = await _work(lambda: importer.cycle(notify=False))
        if isinstance(notes, str):
            await reply(update, notes)
            return
        if not notes:
            import asyncio

            reachable = await asyncio.to_thread(
                guest_reachable, config.ssh_host, 22,
            )
            if not reachable:
                await reply(
                    update,
                    f"{config.vm_name} is not reachable. Import skipped.",
                )
                return
            await reply(update, "Import cycle finished. Nothing new to report.")
            return
        await reply(update, "\n\n".join(notes))

    async def _sort(update: Update, kind: str, needle: str) -> None:
        if importer is None:
            await reply(update, "Moving finished downloads is not set up.")
            return
        await _starting(update)
        await reply(update, "Checking the drives...")
        await reply(update, await _work(lambda: importer.label_and_import(needle, kind)))

    async def choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        callback = update.callback_query
        if callback is None:
            return
        await callback.answer()
        data = callback.data or ""
        kind = data.split(":", 1)[1] if data.startswith("kind:") else ""
        if kind not in {"movie", "show"}:
            return
        uid = user_id(update) or 0
        stored = pending.get(uid)
        if not stored:
            await reply(update, "Send the title again.")
            return
        await _request(update, stored, kind)

    async def _request(update: Update, query: str, kind: str | None = None) -> None:
        if not await allowed(update):
            return
        uid = user_id(update) or 0
        if _needs_choice(query, kind):
            pending[uid] = query.strip()
            await reply(update, _clarify_text(query), CHOOSE)
            return
        is_owner = access.is_owner(uid)
        limit = 30 if is_owner else config.guest_hourly_limit
        # Collection / "all seasons" sweeps fan out many nested lookups; charge more.
        # Guests: one collection sweep per hour at the default limit of 5.
        preview = parse_request(query)
        if preview.collection:
            cost = 10 if is_owner else limit
        else:
            cost = 1
        if not limiter.allow(uid, limit, cost=cost):
            await reply(update, "Too many requests this hour. Try again later.")
            return
        await reply(update, "Checking the library...")
        result = await _work(
            lambda: service.request(query, kind=kind, as_guest=not is_owner)
        )
        if isinstance(result, Clarify):
            pending[uid] = result.query
            await reply(update, result.text, CHOOSE)
            return
        pending.pop(uid, None)
        await reply(update, result)

    async def _starting(update: Update) -> None:
        import asyncio

        reachable = await asyncio.to_thread(guest_reachable, config.ssh_host, 22)
        if not reachable:
            await reply(update, f"{config.vm_name} is off. Starting it...")

    async def _work(func, *, tidy: bool = False):
        import asyncio

        try:
            return await asyncio.to_thread(func)
        except DelugeError as exc:
            log.warning("deluge: %s", type(exc).__name__)
            return public_error(exc)
        except OSError as exc:
            if tidy:
                log.warning("tidy failed: %s", exc)
                return public_error(exc)
            log.warning("search failed: %s", exc)
            return "The search index didn't answer. Try again in a minute."
        except (urllib.error.URLError, TimeoutError) as exc:
            log.warning("search failed: %s", exc)
            return "The search index didn't answer. Try again in a minute."
        except Exception:
            log.exception("request failed")
            return "Something went wrong. The details are in the bot log."

    async def active(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await listing("active", update)

    async def queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await listing("queued", update)

    async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await listing("done", update)

    async def rm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await remove(update, context, False)

    async def rmdata(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await remove(update, context, True)

    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("claim", claim))
    app.add_handler(CommandHandler("allow", allow))
    app.add_handler(CommandHandler("deny", deny))
    app.add_handler(CommandHandler("users", users))
    app.add_handler(CommandHandler("active", active))
    app.add_handler(CommandHandler("queue", queue))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("sort", sort_cmd))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("rm", rm))
    app.add_handler(CommandHandler("rmdata", rmdata))
    app.add_handler(CommandHandler("upgrades", upgrades))
    app.add_handler(CommandHandler("drop", drop_upgrade))
    app.add_handler(CommandHandler("keep", keep_upgrade))
    app.add_handler(CommandHandler("tidy", tidy_cmd))
    app.add_handler(CallbackQueryHandler(choose, pattern=r"^kind:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain))
    return app


def _sort_args(args) -> tuple[str, str] | None:
    if not args or len(args) != 2:
        return None
    kinds = {"movie": "movie", "film": "movie", "show": "show", "series": "show", "tv": "show"}
    left, right = args[0].lower(), args[1].lower()
    if left in kinds and re.fullmatch(r"[0-9a-f]{6,40}", right):
        return kinds[left], right
    if right in kinds and re.fullmatch(r"[0-9a-f]{6,40}", left):
        return kinds[right], left
    return None


def _needs_choice(query: str, kind: str | None) -> bool:
    if kind in {"movie", "show"}:
        return False
    parsed = parse_request(query)
    return parsed.imdb_id is None and parsed.kind not in {"movie", "show"}


def _clarify_text(query: str) -> str:
    shown = " ".join(query.split())
    if len(shown) > 80:
        shown = shown[:77] + "..."
    return (
        f'Is "{shown}" a movie or a TV show?\n'
        "An IMDB link is the surest way. Paste one, such as "
        "https://www.imdb.com/title/tt0133093/"
    )
