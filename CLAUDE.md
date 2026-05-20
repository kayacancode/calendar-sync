# calendar-sync — repo conventions for Claude

A personal tool to unify multiple Google Calendars: merged views, busy-mirroring across accounts, a deduplicated aggregate calendar, an engagement tracker, and a local web dashboard. The Python package is `forever22` and the CLI is `f22` (historical names; the repo is `calendar-sync`).

## Optional bestmate integration

If `bestmate` (an AI knowledge-twin CLI) is installed, it acts as a shared context layer:

- Read context: `bestmate.ask(query)` before acting on something involving people or clients.
- Write outcomes: `bestmate.ingest(content, tags=[...])` after meaningful actions (sync digest, mirror batch, engagement update).

All bestmate calls go through `src/forever22/bestmate.py` — don't shell out to `bestmate` from anywhere else. Calls are best-effort: if bestmate is absent or unauthenticated, the core sync/mirror/aggregate features still work.

## Layout

- `src/forever22/` — all Python modules
- `accounts/` — per-account Google OAuth tokens (gitignored)
- `data/` — SQLite event cache + logs (gitignored)
- `engagements/` — markdown engagement files (gitignored — user data)
- `config.yaml` — accounts, colors, mirror/aggregate settings (gitignored; copy from `config.example.yaml`)
- `credentials.json` — Google Cloud Desktop OAuth client (gitignored; user creates once)
- `scripts/launchd/` — macOS scheduled-job template

## Commands

- `f22 auth --label <name>` — OAuth a single Google account
- `f22 sync` — pull events → mirror busy times → update aggregate calendar (`--no-mirror`, `--no-aggregate` skip stages)
- `f22 mirror` / `f22 aggregate` — run a single stage
- `f22 today` / `f22 week` — merged terminal views
- `f22 block create|list|delete` — held-time blocks
- `f22 eng list|add|show|log` — engagement tracking
- `f22 web` — local dashboard on http://localhost:8022
- `f22 status` / `f22 ask <query>`

## Design notes

- Sync does a bounded windowed pull every run (`past_days`/`future_days`) — no `syncToken`, so the cache stays small and recurring events don't explode.
- Aggregate dedups by `iCalUID + start time` so the same meeting on two calendars collapses, but distinct occurrences of a recurring series are preserved.
- Synthetic events (mirror placeholders, held-time blocks, aggregate copies) are tagged via `extendedProperties.private` and skipped by passes that shouldn't re-process them.

## Style

Lean code, no narrative comments. Don't add abstractions until the second use case actually arrives.
