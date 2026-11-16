
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

CREATE TABLE IF NOT EXISTS cards (
    card_id     TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    lane        TEXT NOT NULL DEFAULT 'inbox',
    position    INTEGER NOT NULL DEFAULT 0,
    prompt      TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    tag         TEXT NOT NULL DEFAULT '',
    sources     TEXT NOT NULL DEFAULT '[]',
    blocked_by  TEXT NOT NULL DEFAULT '[]',
    origin      TEXT NOT NULL DEFAULT 'manual',
    dirty       INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cards_lane ON cards(lane, archived);

CREATE TABLE IF NOT EXISTS card_runs (
    run_id      TEXT PRIMARY KEY,
    card_id     TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    prompt_sent TEXT NOT NULL,
    raw_output  TEXT,
    status      TEXT NOT NULL,
    note        TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS card_runs_card ON card_runs(card_id, started_at);

CREATE TABLE IF NOT EXISTS card_messages (
    msg_id     TEXT PRIMARY KEY,
    from_card  TEXT,
    to_card    TEXT,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS card_messages_to ON card_messages(to_card, read_at);

CREATE TABLE IF NOT EXISTS engagements (
    slug                     TEXT PRIMARY KEY,
    title                    TEXT NOT NULL,
    type                     TEXT NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'active',
    client                   TEXT,
    start_date               DATE,
    end_date                 DATE,
    hours_committed_per_week REAL,
    calendar_account         TEXT,
    links                    TEXT NOT NULL DEFAULT '[]',
    tags                     TEXT NOT NULL DEFAULT '[]',
    notes                    TEXT NOT NULL DEFAULT '',
    created_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    engagement_slug TEXT NOT NULL REFERENCES engagements(slug) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS contacts_engagement ON contacts(engagement_slug);
CREATE TABLE IF NOT EXISTS time_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    engagement_slug TEXT NOT NULL REFERENCES engagements(slug) ON DELETE CASCADE,
    entry_date      DATE NOT NULL,
    hours           REAL NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS time_log_engagement ON time_log(engagement_slug);
CREATE UNIQUE INDEX IF NOT EXISTS time_log_source_event
    ON time_log(engagement_slug, source_event_id) WHERE source_event_id <> '';

CREATE TABLE _revision (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL);
INSERT INTO _revision VALUES (1, 0);
CREATE TABLE _commit_guard (id INTEGER PRIMARY KEY CHECK (id = 1), valid INTEGER NOT NULL CHECK (valid = 1));
CREATE TABLE _commits (request_id TEXT PRIMARY KEY, version INTEGER NOT NULL);
