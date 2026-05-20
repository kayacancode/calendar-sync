# calendar-sync

Unify multiple Google Calendars into one view. Pulls events from every account you own, mirrors busy times across them so nobody can double-book you, and builds a single deduplicated "all calendars" calendar you can look at in the Google Calendar app.

Also includes a lightweight engagement tracker and a local web dashboard. Optional integration with [bestmate](https://bestmate.ai) (an AI knowledge-twin CLI) as a shared context layer.

## Features

- **Unified view** — merged today/week views across all your Google accounts
- **Busy mirroring** — copies busy blocks across calendars so external schedulers see you as busy everywhere
- **Aggregate calendar** — one calendar with the real events from every account, deduplicated by `iCalUID + start`
- **Held-time blocks** — `f22 block` to hold time busy-everywhere or reserved-for-one-account
- **Engagement tracker** — markdown-backed client/community engagement files with time logging
- **Web dashboard** — local FastAPI app at `localhost:8022`

## Setup (one-time)

### 1. Install

```bash
cd calendar-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Google Cloud project + OAuth credentials

1. Go to https://console.cloud.google.com and create a project.
2. Enable the **Google Calendar API**.
3. Configure the OAuth consent screen (External). Add each account email as a test user.
4. Credentials → Create Credentials → **OAuth client ID** → **Desktop app**.
5. Download the JSON and save it as `credentials.json` in the repo root (gitignored).

### 3. Configure your accounts

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` — set one entry per Google account (label, email, color) and adjust the `mirror.rules` / `aggregate` sections. `config.yaml` is gitignored.

### 4. Authorize each account

Run once per account label in your config. A browser window opens each time — sign in with the matching Google account.

```bash
f22 auth --label personal
f22 auth --label work
# ... one per account
```

Each writes `accounts/<label>.json` (gitignored).

> **Heads up:** corporate Google Workspace accounts may have admins that block third-party OAuth apps. If you get "App blocked," ask IT to allowlist your OAuth client ID, or fall back to public free/busy URLs (read-only).

### 5. (Optional) bestmate

If you use the `bestmate` CLI, confirm it's authenticated with `bestmate status`. Otherwise set `bestmate_digest: false` and `aggregate`/sync still work — bestmate calls are best-effort.

## Daily use

```bash
f22 sync                  # pull events → mirror busy times → update unified calendar
f22 sync --no-mirror      # sync only
f22 sync --no-aggregate   # skip the unified-calendar pass
f22 today                 # merged timeline for today
f22 week                  # 7-day grid
f22 status                # last sync per account + bestmate health
f22 mirror                # run the mirror pass on its own
f22 aggregate             # run the unified-calendar pass on its own
f22 web                   # dashboard at http://localhost:8022
```

### Held-time blocks

```bash
# Busy on every calendar — no one can book you
f22 block create "Focus" --when "tomorrow 2-4pm"

# Busy everywhere EXCEPT one account — only that account's invites see free time
f22 block create "Office hours" --when "Wed 9-11am" --for work

f22 block list            # upcoming holds + their IDs
f22 block delete <id>     # remove a hold from every calendar it touched
```

### Engagement tracker

```bash
f22 eng list
f22 eng add --title "Project X" --type client --client acme --hours-per-week 10
f22 eng show project-x
f22 eng log project-x --hours 2.5 --note "Planning meeting"
```

Engagements are markdown files under `engagements/` (gitignored — they're your data). `f22 eng`, the dashboard, and bestmate ingestion all read from those files.

### Dashboard

```bash
f22 web   # → http://localhost:8022
```

Pages: today, week, engagements list, engagement detail.

## Scheduled sync (optional, macOS)

Edit `scripts/launchd/com.forever22.sync.plist` first — replace `/ABSOLUTE/PATH/TO/calendar-sync` with your real checkout path. Then:

```bash
cp scripts/launchd/com.forever22.sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.forever22.sync.plist
```

Runs `f22 sync` every 10 minutes (sync → mirror → aggregate). To stop: `launchctl unload ~/Library/LaunchAgents/com.forever22.sync.plist`.

> **First-run heads up:** the first mirror pass creates a "Busy — \<source\>" event on each target calendar for every busy event in the window (`now` to `+60 days`). Recurring events count once per occurrence. They're marked `private` and tagged `mirroredFrom` so the tool owns them; later syncs only apply deltas. Trim the volume by editing `mirror.rules` in `config.yaml`.

## Notes

- The unified calendar holds *copies* — edit events on their original calendar, not the aggregate.
- Sync does a bounded windowed pull (`past_days` / `future_days`) each run, so the local cache stays small.
