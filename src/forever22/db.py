from __future__ import annotations

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .config import database_url

# All forever22 state — calendar cache plus the stateful mirror/aggregate/block
# tracking — lives in one Postgres database so local runs and the cloud cron
# share a single source of truth (no divergence).
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    account_label TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    calendar_id   TEXT NOT NULL,
    start_iso     TEXT NOT NULL,
    end_iso       TEXT NOT NULL,
    summary       TEXT,
    status        TEXT,
    is_busy       INTEGER NOT NULL DEFAULT 1,
    raw_json      TEXT,
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (account_label, event_id)
);
CREATE INDEX IF NOT EXISTS events_time ON events(start_iso, end_iso);
CREATE INDEX IF NOT EXISTS events_account ON events(account_label);

CREATE TABLE IF NOT EXISTS sync_state (
    account_label TEXT PRIMARY KEY,
    sync_token    TEXT,
    last_full_at  TEXT,
    last_run_at   TEXT
);

CREATE TABLE IF NOT EXISTS mirrors (
    source_account_label TEXT NOT NULL,
    source_event_id      TEXT NOT NULL,
    target_account_label TEXT NOT NULL,
    target_event_id      TEXT NOT NULL,
    source_start_iso     TEXT NOT NULL,
    source_end_iso       TEXT NOT NULL,
    source_summary       TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (source_account_label, source_event_id, target_account_label)
);
CREATE INDEX IF NOT EXISTS mirrors_target ON mirrors(target_account_label, target_event_id);

CREATE TABLE IF NOT EXISTS blocks (
    block_id        TEXT NOT NULL,
    account_label   TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    mode            TEXT NOT NULL,
    target_label    TEXT,
    start_iso       TEXT NOT NULL,
    end_iso         TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (block_id, account_label)
);
CREATE INDEX IF NOT EXISTS blocks_time ON blocks(start_iso, end_iso);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS aggregated (
    ical_uid        TEXT PRIMARY KEY,
    source_account  TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    agg_event_id    TEXT NOT NULL,
    start_iso       TEXT NOT NULL,
    end_iso         TEXT NOT NULL,
    summary         TEXT,
    updated_at      TEXT NOT NULL
);
"""

_schema_ready = False


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn.execute(SCHEMA)
    conn.commit()
    _schema_ready = True


@contextmanager
def connect():
    conn = psycopg.connect(
        database_url(), prepare_threshold=None, connect_timeout=20, row_factory=dict_row
    )
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_state(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_state (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )


def upsert_event(conn, *, account_label: str, event_id: str, calendar_id: str,
                 start_iso: str, end_iso: str, summary: str | None,
                 status: str | None, is_busy: bool, raw_json: str, synced_at: str) -> None:
    conn.execute(
        """
        INSERT INTO events (account_label, event_id, calendar_id, start_iso, end_iso,
                            summary, status, is_busy, raw_json, synced_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_label, event_id) DO UPDATE SET
            calendar_id=excluded.calendar_id,
            start_iso=excluded.start_iso,
            end_iso=excluded.end_iso,
            summary=excluded.summary,
            status=excluded.status,
            is_busy=excluded.is_busy,
            raw_json=excluded.raw_json,
            synced_at=excluded.synced_at
        """,
        (account_label, event_id, calendar_id, start_iso, end_iso,
         summary, status, 1 if is_busy else 0, raw_json, synced_at),
    )


def delete_event(conn, *, account_label: str, event_id: str) -> None:
    conn.execute("DELETE FROM events WHERE account_label = %s AND event_id = %s",
                 (account_label, event_id))


def get_sync_token(conn, account_label: str) -> str | None:
    row = conn.execute("SELECT sync_token FROM sync_state WHERE account_label = %s",
                       (account_label,)).fetchone()
    return row["sync_token"] if row else None


def set_sync_state(conn, *, account_label: str, sync_token: str | None,
                   last_full_at: str | None, last_run_at: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (account_label, sync_token, last_full_at, last_run_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (account_label) DO UPDATE SET
            sync_token = COALESCE(excluded.sync_token, sync_state.sync_token),
            last_full_at = COALESCE(excluded.last_full_at, sync_state.last_full_at),
            last_run_at = excluded.last_run_at
        """,
        (account_label, sync_token, last_full_at, last_run_at),
    )


def events_in_range(conn, *, start_iso: str, end_iso: str) -> list[dict]:
    return conn.execute(
        """
        SELECT * FROM events
        WHERE end_iso > %s AND start_iso < %s
          AND COALESCE(status, 'confirmed') != 'cancelled'
        ORDER BY start_iso
        """,
        (start_iso, end_iso),
    ).fetchall()


def sync_status(conn) -> list[dict]:
    return conn.execute(
        """
        SELECT s.account_label, s.last_run_at, s.last_full_at,
               (SELECT COUNT(*) FROM events e WHERE e.account_label = s.account_label) AS event_count
        FROM sync_state s
        ORDER BY s.account_label
        """
    ).fetchall()
