# Cloudflare D1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace unavailable Supabase storage with fresh Cloudflare D1 while retaining Python and Actions.
**Architecture:** Authenticated Worker offers consistent snapshots and atomic, version-checked row changes. Python executes each existing unit of work in a private in-memory SQLite snapshot, then sends only changed rows in a single D1 batch. This preserves existing read-your-writes and rollback semantics without independently committing each SQL call. No arbitrary SQL crosses the service boundary.
**Tech Stack:** Python sqlite3/httpx, Cloudflare Worker JavaScript, D1, Wrangler, pytest, node:test.
**Spec:** docs/superpowers/specs/2026-11-16-cloudflare-d1-design.md

## Global Constraints

Keep GitHub schedules and unrelated uncommitted changes. User explicitly accepts fresh storage; do not require recovery from Supabase. Preserve Google events using existing tag adoption. Do not delete Supabase. Keep PostgreSQL fallback available.

## Review Focus

- Concurrent writers must fail explicitly rather than overwrite newer data.
- Network failure must not trigger blind replay of calendar operations or ambiguous commits.
- JSON/date values and generated contact/time IDs must preserve application behavior.
- Failed batches must roll back completely; missing credentials fail closed.
- A fresh database must reuse tagged calendar events rather than create copies.

## Task 1: Transactional storage client

Files: src/forever22/d1.py, src/forever22/db.py, src/forever22/engagements.py, tests/test_d1.py, cloudflare/migrations/0001_initial.sql.
Interfaces: D1Connection.execute/commit/close/context manager/cursor, configured by FOREVER22_D1_URL and FOREVER22_D1_TOKEN. Worker protocol GET /snapshot returns version and table rows; POST /commit accepts version, request ID and per-table upserts/deletes.
- [x] Write failing tests for rollback, upsert/delete diffs, SQL placeholders and engagement JSON/date/dedup behavior; run pytest and confirm failures.
- [x] Implement private SQLite snapshots, bounded responses and request bodies, no automatic write retries, version conflict reporting; route both storage modules to D1 when selected.
- [x] Run tests and commit only migration files.

## Task 2: Private D1 service

Files: cloudflare/worker.js, cloudflare/package.json, cloudflare/wrangler.jsonc, cloudflare/test/worker.test.js.
Interfaces: fixed schema/table/column catalog; snapshot and commit endpoints only. Revision guard and all mutations execute in one batch. Never accept SQL from caller.
- [x] Add failing endpoint tests for auth, validation, conflicts, and transactional failure.
- [x] Implement bounded input, constant-time auth, fixed table catalog, consistent snapshot and version-checked batches; configure D1 migrations and disabled public previews.
- [x] Test against local D1 using Wrangler, including concurrent commits and actual batch rollback; dry-run deployment.

## Task 3: Provision, configure and validate

Files: .github/workflows/sync.yml, .github/workflows/autolog.yml, README.md, docs/superpowers/specs/2026-11-16-cloudflare-d1-design.md.
- [x] Provision D1 and Worker using existing Cloudflare login. Use secret files/stdin without printing credentials.
- [x] Configure local and Actions credentials; preserve old Postgres values. Fresh D1 intentionally starts without historic boards/engagement logs.
- [x] Verify schema, remote isolated read/write/delete, Google event adoption tests, and full Python suite.
- [x] Obtain independent code review and fix significant findings.
- [ ] Push migration branch and execute Actions there; verify successful sync before reporting cutover. Integrate only within user authorization and report any remaining action.

## Rulings and progress

- User approved implementation and fresh storage. Proceed inline without another plan approval round.
- Use current checkout on a new codex branch to retain existing local board changes; do not commit those unrelated changes.
- Snapshot approach trades whole-dataset reads for simple, atomic compatibility. Enforce 16 MiB snapshot and 8 MiB commit limits; fail explicitly when exceeded. Suitable for this bounded personal calendar cache, not a general database driver.

Verification ledger: baseline 8 tests passed; client red test confirmed missing module before implementation. Worker red tests confirmed missing worker before implementation. Final local suite: 21 passed, including four real local D1 integration tests. Node API suite: 4 passed. Independent reviewer found no critical/important bugs. Production service smoke test: unauthenticated access rejected; authenticated insert/read/delete passed. Wheel includes SQLite schema. Existing local board/view/web changes remain uncommitted. Cloudflare database 515bc12f-8752-4d1f-955d-117f989e46b9 and calendar-sync-storage Worker provisioned. Local and Actions credentials configured without exposing values. Both Actions workflows now share a concurrency group to serialize calendar writers.
