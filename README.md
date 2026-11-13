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
- After Plex is serving a higher-quality upgrade, the owner can `/upgrades` and `/drop` the old copy to the Recycle Bin (not hard-deleted).
- Structured JSON events are logged on the `plex_queue.events` logger (`tidy.audit`, `tidy.apply`, `plex.refresh_all`, `import.cycle`, `upgrade.drop`, …). With `otel.enabled: true` and `pip install -e ".[otel]"`, those events (plus traces/metrics) export over OTLP to the local stack in [`observability/`](observability/) (Collector → Kafka + Prometheus/Loki/Tempo → Grafana).

Other people can request titles after you `/allow` their Telegram id. Only the owner can remove torrents, tidy/refresh the library, or change who is allowed.

## Library tidy

The owner can `/tidy` (or CLI `python -m plex_queue tidy`) to audit existing library files across all roots. Fixes are **propose-and-confirm** — nothing moves or trashes until you apply.

| Kind | What it does |
|------|----------------|
| **dupes** | Keep the best copy (height, then size); trash strong Plex-linked losers to the Recycle Bin |
| **scan / orphans** | Ask Plex to scan folders that have files on disk but not in the Plex index |
| **move** | Relocate misplaced titles onto the matching bucket (movies, kids, anime, documentary, standup, …) |
| **rename** | Clean **release-junk movie folders** only (e.g. `Title (2024) [1080p] [YTS.MX]` → `Title (2024)`), keeping the original filename |

### Rename rules (conservative)

- Movies only — episodes keep their season/episode wording.
- Prefer stripping junk from an existing `Title (Year) [tags]` folder so digits in titles survive (`This Is 40`, `Rush Hour 3`).
- Dotted release folders are humanized; collection packs (`Complete`, `2008-2012`, …) are left alone.
- Already-clean `Title (Year).mkv` files sitting on a bucket root are **not** wrapped into a new folder.
- Sample with `/tidy apply renames 50` before `renames all`.

### Move rules (conservative)

- Trust specialized shelves (kids, anime, christmas, documentary, standup, workouts).
- Do not yank show extras / holiday episodes into movies or christmas just because of a title keyword.
- Standup classification is narrow (person + special patterns); comedy movies are not auto-filed as standup.

### Apply / skip

```text
/tidy apply <id>
/tidy apply dupes
/tidy apply orphans          # or: tidy apply orphans
/tidy apply renames 50
/tidy apply moves 1-20
/tidy skip <id>
/tidy skip renames all
```

Selectors: `50` | `1-100` | `1:50` | `1,5,987` | `s01` | `s01:05` | `s01e03` | `all`

Skipped and completed action ids stay sticky across re-audits so dismissed work does not come back with the same id.

## Plex refresh

`/scan` only rereads the local Plex DB and drive walks.

`/refresh` (owner) — or CLI `python -m plex_queue refresh`:

1. Stops any in-progress Plex library scans (`DELETE /library/sections/all/refresh`).
2. Asks Plex to fully refresh every library section.
3. Posts progress (~every 20s) from Plex `/activities` (Telegram and CLI).
4. Waits at least ~10 minutes and ~3 minutes of quiet after scan activity before declaring the pass done (avoids quitting between sections).
5. Rereads the local index.

Do not put your user profile (e.g. `C:\Users\…`) in a Plex library path — full refresh will crawl it.

### After a large tidy rename batch

1. `python -m plex_queue refresh` (or `/refresh`) and wait until Plex Activity is idle.
2. `python -m plex_queue tidy` again.
3. Skip renames that target disconnected drives; re-audit when those drives are online.

## CLI

```powershell
.\.venv\Scripts\python.exe -m plex_queue              # run the bot
.\.venv\Scripts\python.exe -m plex_queue check        # connectivity / library smoke test
.\.venv\Scripts\python.exe -m plex_queue tidy         # audit (requires telegram.owner_id)
.\.venv\Scripts\python.exe -m plex_queue tidy apply renames 50
.\.venv\Scripts\python.exe -m plex_queue tidy apply orphans
.\.venv\Scripts\python.exe -m plex_queue tidy skip renames all
.\.venv\Scripts\python.exe -m plex_queue refresh      # full Plex refresh + progress
```

## Telegram (owner extras)

`/rm`, `/rmdata`, `/scan`, `/refresh`, `/upgrades`, `/drop`, `/keep`, `/tidy …`, `/import`, `/allow`, `/deny`, `/users`, `/sort`

Guests who are `/allow`ed can request titles and view queue status; they cannot tidy, refresh, or remove torrents.

## Setup

1. In Telegram, open `@BotFather`, create a bot, and copy the token.
2. Run `scripts\setup.ps1`.
3. Edit `config.yaml`: token, a claim secret, and the Deluge password. The video folders are already filled in from this Plex server. Prefer `config.example.yaml` as the template for new machines.
4. Run the SSH command setup prints. That installs a key so the bot can reach Deluge without a password prompt.
5. `.\.venv\Scripts\python.exe -m plex_queue check`
6. `.\.venv\Scripts\python.exe -m plex_queue`
7. Message the bot `/claim your-secret`, then `/whoami` from anyone else and `/allow` that id.

`scripts\install-task.ps1` starts the bot when you sign in to Windows.

### Observability (optional)

```powershell
cd observability
docker compose up -d
.\.venv\Scripts\pip.exe install -e ".[otel]"
# set otel.enabled: true in config.yaml, then restart the bot
```

Grafana: [http://127.0.0.1:3001](http://127.0.0.1:3001) (default `admin`/`admin`). Details in [`observability/README.md`](observability/README.md). Docker is separate from the Scheduled Task.

The Deluge password is the `localclient` value in `~/.config/deluge/auth` on the VM. The bot never needs the `.torrent` file. It sends a magnet to the daemon, and the download stays on the VM.

## Security

- Corporate-style policy and SDLC expectations: [`SECURITY.md`](SECURITY.md)
- Homelab operator disclosure / expectations: [`SECURITY_homelab.md`](SECURITY_homelab.md)

Never commit real `config.yaml`, tokens, passwords, or private keys. `data/` holds signed state (`access.json`, `tidy.json`, `imports.json`, `ssh_known_hosts`) and is gitignored.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Python 3.11+. Package layout is under `src/plex_queue/`. This repo treats Bugbot + Security Review as a hard gate before commits that change behavior.
