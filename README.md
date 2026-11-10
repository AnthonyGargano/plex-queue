# plex-queue

A Telegram bot on this PC. It reads the Plex library and the video drives, then talks to Deluge on the Ubuntu VM over an SSH tunnel. If UbuntuClean is off, the bot starts it and waits until SSH answers. The VM SSH host key is pinned in `data/ssh_known_hosts`; a changed key is refused. `/claim` is limited to a few attempts per hour, and `/deny` clears that person's pending title choice.

Send a title in Telegram. A bare title asks whether it is a movie or a TV show before anything is queued. An IMDB link, such as `https://www.imdb.com/title/tt0133093/`, names the exact title and skips that question. A season or episode code is treated as a show.

- If a copy is already 1080p or higher, the bot tells you to check Plex and does not queue anything. A widescreen 1080p file is often stored as 1920x800; that still counts as 1080p.
- "All of the Clerks movies", or a trilogy, looks up each movie. Anything already at 1080p is left alone. A missing movie, or one below 1080p, is queued by itself. The summary says how many were covered, queued, or left unfilled. Sets whose titles do not share a prefix (Dark Knight, Cornetto) use a built-in member list.
- A bare TV show name does not count as owned from one episode. It lists seasons on disk and asks for `S01` or `S01E01`. A season is treated as owned only when episodes 1 through the highest owned episode are all present at 1080p with no gaps.
- If the copy on disk is below 1080p, and a 1080p or 4K release exists, that release is queued.
- If you don't have it, it is queued. A 1080p release is preferred when one is available.
- `/active`, `/queue`, and `/done` show Deluge. The owner can `/rm` a torrent.
- Every 10 minutes, while the VM is already on, finished downloads are copied to the movie or show drive with enough free space (2 GB left over). The copy on the VM is then removed, and Plex scans the new folder. Kids, anime, documentary, and the other special libraries are left alone. Change the `import:` folders in `config.yaml` to pick different drives. File paths that climb out of the download folder, or that would write outside the chosen library root, are refused.
- The owner can `/tidy` to audit existing library files across all roots: duplicates (keep highest quality), orphans (ask Plex to scan), misplaced specials, and messy folder names. Fixes are propose-and-confirm (`/tidy apply <id>`, `/tidy apply dupes`, `/tidy apply renames 50`, `/tidy orphans`, `/tidy skip renames all`). The same verbs work from the CLI: `python -m plex_queue tidy`.
- Structured JSON events are logged on the `plex_queue.events` logger (`tidy.audit`, `tidy.apply`, `import.cycle`, `upgrade.drop`, …) so a future Kafka → Grafana / autotick pipeline can subscribe without rewriting call sites.

Other people can request titles after you `/allow` their Telegram id. Only the owner can remove torrents or change who is allowed.

## Setup

1. In Telegram, open `@BotFather`, create a bot, and copy the token.
2. Run `scripts\setup.ps1`.
3. Edit `config.yaml`: token, a claim secret, and the Deluge password. The video folders are already filled in from this Plex server.
4. Run the SSH command setup prints. That installs a key so the bot can reach Deluge without a password prompt.
5. `.\.venv\Scripts\python.exe -m plex_queue check`
6. `.\.venv\Scripts\python.exe -m plex_queue`
7. Message the bot `/claim your-secret`, then `/whoami` from anyone else and `/allow` that id.

`scripts\install-task.ps1` starts the bot when you sign in to Windows.

The Deluge password is the `localclient` value in `~/.config/deluge/auth` on the VM. The bot never needs the `.torrent` file. It sends a magnet to the daemon, and the download stays on the VM.
