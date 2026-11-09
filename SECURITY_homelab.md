# Security Policy (Homelab / Personal Projects)

This is the **homelab** security policy for personal software such as plex-queue: a single-maintainer, self-hosted project. It keeps the same disclosure discipline as an enterprise `SECURITY.md`, without inventing corporate contacts, bug bounties, or compliance claims that do not exist here.

**Code and reviews use the stricter corporate `SECURITY.md` §§9–11 baseline** (secrets, authz, SSRF, path confinement, state integrity). This file is for honest operator disclosure contacts/SLAs only — it does not weaken engineering controls.

For GitHub’s Security tab, prefer publishing `SECURITY.md` (filled-in) or a short root `SECURITY.md` that points here for reporting contacts while keeping corporate engineering rules in force.

---

## 1. Principles

1. **Protect the operator and guests first.** Prefer private, coordinated disclosure until a fix or mitigation lands.
2. **Assume good faith** from researchers who follow this policy (see Safe harbor).
3. **Severity drives urgency**, even when there is only one maintainer.
4. **No security theater.** Prefer real controls (authz, path checks, host-key pinning, secret hygiene) over obscurity.

---

## 2. What this project is

Typical deployment assumptions for this class of project:

- Runs on a **home PC / lab network**, not a multi-tenant SaaS.
- Trust boundary is roughly: **Telegram bot token + owner allow-list + SSH access to a download VM + local media paths**.
- Guests may request titles only after the owner explicitly `/allow`s them.
- Secrets live in **local** `config.yaml` / SSH keys / OS stores — not in git.

If you fork this for a shared or internet-exposed deployment, tighten auth, networking, and monitoring beyond what this doc assumes.

---

## 3. Scope

### In scope

| Area | Examples for this stack |
|------|-------------------------|
| Bot authz | Claiming ownership without the setup secret, bypassing `/allow`, guest privilege escalation |
| Secrets | Leaked Telegram token, Deluge password, SSH private key, Plex token misuse |
| Path / filesystem | Writing or deleting outside configured library/download roots; symlink tricks |
| SSH / VM trust | Host-key pinning bypass, tunnel misuse, command injection via remote paths |
| Supply chain | Malicious dependency updates that affect this repo’s install/runtime |

### Out of scope

- Phishing / social engineering the operator
- DoS against Telegram, public trackers, or cloud providers
- Pure upstream CVEs with no project-specific exposure (open a normal dependency PR if a fix exists)
- “Please add security headers” style advice with no exploit path
- Attacks that already require full control of the operator’s Windows session or SSH key

When unsure, **report privately** anyway.

### Supported versions

| Version | Supported |
|---------|-----------|
| `main` / latest commit the maintainer is running | Yes |
| Older commits / unmaintained forks | No |

There is no LTS channel. Security fixes land on `main` when the maintainer can apply them.

---

## 4. How to report a vulnerability

**Do not** open a public GitHub Issue, Discussion, or PR for security bugs.

### Preferred channel

1. **GitHub Private Vulnerability Reporting**  
   Repo → **Security** → **Advisories** → **Report a vulnerability**  
   (Maintainer: enable this under repository Settings → Security.)

2. If private reporting is unavailable, contact the repository owner through a **private** channel they publish (for example a profile email). Do not paste live tokens or keys into that message if a redacted PoC is enough.

### What to include

- Short title and vulnerability type
- Affected commit / tag if known
- Steps to reproduce (minimal)
- Impact (e.g. guest can `/rm`, write outside library roots, steal bot token)
- PoC that is **non-destructive** when possible
- Whether you plan any public write-up, and when

### Proof-of-concept rules

- Do not wipe media libraries, ransomware files, or pivot beyond what proves the bug.
- If you hit live credentials or personal data, stop, redact, and say so in the report.

---

## 5. Response targets (best effort)

Single-maintainer reality: these are **targets**, not SLAs.

| Severity | Examples | Target after confirmation |
|----------|----------|---------------------------|
| **Critical** | Unauth remote code execution; anyone can become owner; trivial write/delete outside media roots | Mitigate or fix within **7 days** when possible |
| **High** | Guest privilege escalation; SSH trust break; secret exposure in logs/repo | Within **30 days** |
| **Medium** | Limited authz gap; constrained path issue | Within **90 days** |
| **Low** | Defense-in-depth / hardening | Best effort / next opportunistic change |

Acknowledgement goal: **within a few days** of a clear private report. Weekly updates are ideal while a confirmed issue is open; silence may happen during travel or outages—one polite follow-up is fine. Do not create a public issue to bump.

---

## 6. Coordinated disclosure

- Please keep details private until a fix is on `main` (or an effective mitigation is documented), or until **90 days** after acknowledgement of a valid in-scope report, whichever comes first—unless we agree otherwise.
- Give **~7 days notice** before public disclosure if the embargo expires without a fix.
- Credit is offered by default in an advisory or commit message; say if you want a handle or anonymity.

**No bug bounty.** Thanks and credit only.

---

## 7. Safe harbor

Good-faith research that stays in scope and follows the PoC rules above will not be treated as an attack by the maintainer. Safe harbor does **not** cover extortion, destructive actions, exfiltrating media/PII beyond demonstration, or public dump of an unfixed Critical issue without following coordinated disclosure.

---

## 8. Secrets and local data

Expected operator hygiene (this project’s design assumes it):

| Secret | Where it should live |
|--------|----------------------|
| Telegram bot token | `config.yaml` (gitignored) |
| Setup / claim secret | `config.yaml` (gitignored); treat like a password |
| Deluge password | `config.yaml` (gitignored) |
| SSH private key | Operator key path (e.g. under `%USERPROFILE%\.ssh\`), not the repo |
| Plex token | Read from the local Plex install when needed; do not commit it |
| SSH known_hosts pin | `data/` (gitignored); refuse unexpected key changes |

If you find a secret in git history, a gist, or logs: report it privately, and the operator should **rotate** the credential (history rewrite alone is not enough).

Contributors must never commit real `config.yaml`, tokens, passwords, or private keys. Use `config.example.yaml` with placeholders only. Prefer scrubbing real hostnames, LAN IPs, and drive layouts from examples when publishing widely.

---

## 9. Secure development expectations (this codebase)

When changing this project, preserve or improve:

1. **Owner vs guest authorization** on dangerous commands (`/rm`, `/allow`, `/tidy apply`, `/drop`, etc.).
2. **Constant-time (or equivalent) comparison** for the claim secret; rate-limit `/claim`.
3. **Path confinement** for import, tidy, and trash/drop flows (no writes outside configured roots; be careful with symlinks).
4. **SSH host-key pinning** with strict checking against the dedicated known_hosts file (`deluge.ssh.allow_tofu` defaults to false; prefer `ssh-keyscan` on a trusted LAN).
5. **Propose-and-confirm** for destructive library tidy actions unless the operator knowingly enables automation.
6. Avoid logging tokens, passwords, or full secret values.
7. Keep `setup_secret` long (16+), leave `allowed_user_ids` empty until after `/claim`, and use guest rate / active-download caps in config.
8. `data/access.json` is HMAC-signed with `setup_secret`; tampered files are quarantined. Ownership is first-claim-wins (no secret reclaim)—set `telegram.owner_id` or delete a verified-bad `access.json` offline to reset.
9. `search.endpoint` is HTTPS-only, DNS-checked against private ranges, and redirects are blocked at fetch time.

Suggested maintainer extras (not required for a tiny homelab, but valuable):

- Enable GitHub Dependabot (or equivalent) for dependency alerts
- Turn on secret scanning / push protection if the repo is on GitHub
- Keep dependencies minimal and pinned reasonably in `pyproject.toml`

---

## 10. Homelab deployment tips (operators)

These are suggestions, not product guarantees:

- Do not expose the Deluge RPC port or SSH to the public internet; keep the SSH tunnel / LAN trust model.
- Use a dedicated SSH key for the bot with limited purpose; protect the Windows user account that runs the bot.
- Rotate the Telegram token and claim secret if either may have leaked.
- After first SSH host-key pin, treat a sudden key-change failure as a warning, not something to bypass casually.
- Limit `/allow` to people you trust; guests can still cause download/queue load.

---

## 11. Policy maintenance

| Item | Detail |
|------|--------|
| **Audience** | Personal / homelab repos |
| **Owner** | Repository maintainer |
| **Review** | When auth, filesystem, or SSH behavior changes materially |
| **Companion** | Enterprise-style template: `SECURITY.md` (placeholders for org use) |

Thank you for helping keep a home lab boringly safe.
