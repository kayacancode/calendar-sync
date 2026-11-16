# Calendar database migration to Cloudflare D1

Status: proposed; scope agreed, implementation pending design approval.

Move shared application storage from Supabase PostgreSQL to Cloudflare D1. Keep the Python application, Google Calendar integrations, local dashboard, and GitHub Actions schedules.

## Approach

Use D1 behind an authenticated HTTPS Worker API. This follows Cloudflare's recommended external-access pattern rather than using its administrative REST API for routine application traffic. Retaining external PostgreSQL through Hyperdrive would retain the failed database dependency; moving the whole application to Workers expands scope unnecessarily.

Adapt both calendar and engagement storage. The database also holds mirror and aggregate event IDs, held blocks, board cards and history, contacts, and time logs. Migrate every application table, not just the event cache. Convert PostgreSQL JSONB, dates, timestamps, serial IDs, placeholders, and schema introspection to explicit SQLite-compatible representations. Decode JSON fields at the application boundary.

## Correctness and access

The Worker accepts authenticated, parameterized application operations over HTTPS, with bounded request sizes, a restricted operation surface, and sanitized errors. Store the service credential as a Worker secret and GitHub Actions secret; never expose it to dashboard browser code. Both local Python and Actions use the same service.

Preserve atomic multi-statement operations using D1 batches. Do not emulate a PostgreSQL connection by making each statement an independently committed HTTP request. Refactor reads and writes into explicit operations where required. Calendar API changes cannot be rolled back by the database; preserve tracking and existing reconciliation behavior, and do not blindly retry ambiguous writes.

Protect read–modify–write operations with conditional updates or database-enforced uniqueness rather than assuming batches protect earlier reads. Inventory all current schema additions, including local board changes. Preserve explicit IDs during import and verify that generated IDs continue above imported values.

## Migration and cutover

1. Build and test the new backend independently while retaining PostgreSQL support for rollback.
2. Confirm Cloudflare account access and provision a separate D1 database and authenticated Worker.
3. Stop all database writers during the final export and import, including sync, daily autolog, local schedules, and dashboard writes. Account for autolog's watchdog that re-enables sync.
4. Take a private complete export from the existing database and import into D1. Verify table counts, primary keys, relationships, and calendar tracking IDs before switching configuration.
5. Test read-only application access first, then run one controlled sync and verify its result before resuming schedules. Update both Actions workflows and local configuration together.
6. Retain the old database and export. Rollback after D1 writes requires reconciling those writes; switching the URL alone is insufficient.

If Supabase remains unavailable, complete the new backend but do not enable calendar writes against an empty database. The local events.db is dated May 22 and is not automatically a trustworthy current backup. Recover the existing database or explicitly design reconstruction of synthetic calendar tracking before cutover.

## Validation

Cover storage behavior against local D1: event upserts, mirror/aggregate mappings, blocks, board operations, engagement JSON/date conversions, time-log deduplication, foreign keys, and atomic rollback on a failing batch. Test missing/invalid credentials and sanitized errors. Verify deployed authenticated read/write operations on isolated test data, imported data integrity, and a successful Actions sync before claiming migration complete.

## Constraints and implementation choices

Preserve unrelated uncommitted dashboard and board changes. Do not delete Supabase resources or replace Google OAuth credentials. No UI redesign, scheduler migration, or new paid plan is included. Determine exact batching, service operation boundaries, and test fixtures during implementation; preserve behavior rather than providing a generic public SQL gateway.

References: https://developers.cloudflare.com/d1/tutorials/build-an-api-to-access-d1/ ; https://developers.cloudflare.com/d1/worker-api/d1-database/
