# Changelog

## Unreleased

- Bare titles are looked up on IMDB and treated as a movie or show when the match is unambiguous.
- Import copies onto an existing special shelf (kids, documentary, standup, and the others), or onto that shelf when the release name says so. Otherwise the generic movie or show drives.
- Batch `/tidy apply` (and `/tidy orphans`) lists each done/failed action; `/tidy last` re-shows that report.

## 0.1.0

### Library tidy

- Propose-and-confirm audit for duplicates, orphans/scans, misplaced moves, and layout renames.
- Renames limited to movie release-junk folders; preserve title digits; skip collection packs and clean root-level `Title (Year)` files.
- Moves trust specialized shelves; avoid yanking show extras into movies/christmas; narrower standup heuristics.
- Owner Telegram + CLI apply/skip with batch selectors; sticky skipped/done ids across re-audits.

### Plex refresh

- Owner `/refresh` and CLI `python -m plex_queue refresh`.
- Cancels in-progress PMS scans, refreshes every library section, posts `/activities` progress, then rereads the local index.
- Patient wait defaults so multi-section scans are not declared finished between libraries.

### Security / ops

- Owner-only gates for tidy apply and full refresh; Plex token not logged.
- Path confinement and symlink refusal on tidy apply unchanged.
- Documentation: README ops guide, `SECURITY_homelab.md` refresh notes, `config.example.yaml` comments.
