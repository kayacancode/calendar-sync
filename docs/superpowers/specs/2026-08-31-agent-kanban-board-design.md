# Agent Kanban Board — Design

2026-08-31 · approved in-session by Kaya

## What

An autonomous Kanban board inside forever22 (`/board` on the f22 dashboard). Cards are
agents: each holds a standing prompt and a living markdown body, auto-populated and
merged from Granola meeting notes, executed by headless Claude runs, able to message
other cards. Outlook bridge explicitly deferred.

## Decisions

- Lives in forever22: SQLite + FastAPI dashboard + launchd schedule.
- "Anonymous" = autonomous (dictation typo).
- V1 scope: board + Granola ingest + send-to-Claude execution + card-to-card. No Outlook.
- Triggers: lane-drag into `in_progress` fires the agent; scheduled tick runs ingest and
  dirty in-progress cards.
- Agent powers: board writes + bestmate ingest. **Never Salience** — no Salience field
  exists in the result schema, so it is structurally impossible.
- Architecture: approach A — structured one-shot `claude -p` runs returning strict JSON;
  f22 parses and applies all writes. Board-MCP live sessions are a v2 swap.
- Granola: official Granola API key (in `config.yaml`) pulls notes directly; Claude only
  extracts/merges.

## Data model (new tables in existing SQLite DB)

- `cards`: id, title, lane (`inbox|todo|in_progress|review|done`), position, prompt,
  body (markdown, evolves), sources (JSON: granola meeting ids/links), blocked_by
  (JSON card ids), origin (`granola|manual|agent`), dirty flag, archived, timestamps.
- `card_runs`: card_id, trigger (`lane|schedule|manual|ingest`), prompt_sent,
  raw_output, parsed JSON, status (`ok|parse_error|failed`), duration.
- `card_messages`: from_card, to_card (NULL = to user), body, read_at.

New granola cards land in `inbox` (clear-cut tasks may land in `todo`).

## Ingest & merge

`f22 board ingest`: fetch meetings/notes since watermark via Granola API → one
`claude -p` call with notes inlined + digest of existing cards → JSON verdict per item:
`new` / `update <card_id>` (append dated note, extend sources) / `skip`. Validate, apply
atomically; on parse failure apply nothing, log raw output. Watermark makes re-runs
idempotent. Ingest is logged as a run for auditability.

## Runner

`f22 board run <id>`: prompt = card prompt + body + source excerpts + unread messages +
completed blockers' outputs + fixed protocol block. Shell out to `claude -p` (web search
allowed; 5-min timeout). Result JSON: `body`, `lane_move` (may move to `review`/`todo`,
never `done`), `messages [{to, body}]`, `bestmate [{content, tags}]`, optional
`blocked_by` proposals (applied if acyclic). Bad JSON → `parse_error`, warning badge,
nothing applied.

A card fires only when **dirty**: entered `in_progress`, new message, blocker completed,
or prompt/body edited since last run. Blocked cards never fire. Blocker hitting `done`
messages its dependents (marking them dirty) → next tick cascades. Concurrency cap 2,
one run per card at a time. Loop guards: ≤10 runs per tick, message-triggered runs
cascade at most one extra hop.

## Card-to-card

Messages are the only channel (no shared scratchpad). Agents see a board roster
(id/title/lane) to address peers. Prerequisites set by user, ingest, or agent proposal
(cycle-checked).

## UI

`/board` page, existing Jinja + single-stylesheet style, vanilla-JS HTML5 drag & drop.
Card face: title, origin badge, blocked/warning badges, unread dot, last-run time.
Detail view: editable prompt/body, sources → Granola links, blockers, message thread,
full run log. Top strip: messages to user + ingest summary. Manual new-card; archive
done cards. CLI: `f22 board ingest | run <id> | tick | list`.

## Testing

Unit-test the pure core (merge application, dirty logic, result validation/apply, cycle
check) against a temp DB; runner's `claude -p` boundary mocked. Manual smoke: ingest
against real Granola API, drag a card, read the run log.
