from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

import psycopg
from psycopg.rows import dict_row

from .config import database_url

VALID_STATUSES = ("active", "paused", "completed")

SCHEMA = """
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
    links                    JSONB NOT NULL DEFAULT '[]',
    tags                     JSONB NOT NULL DEFAULT '[]',
    notes                    TEXT NOT NULL DEFAULT '',
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS contacts (
    id              BIGSERIAL PRIMARY KEY,
    engagement_slug TEXT NOT NULL REFERENCES engagements(slug) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL DEFAULT '',
    role            TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS contacts_engagement ON contacts(engagement_slug);
CREATE TABLE IF NOT EXISTS time_log (
    id              BIGSERIAL PRIMARY KEY,
    engagement_slug TEXT NOT NULL REFERENCES engagements(slug) ON DELETE CASCADE,
    entry_date      DATE NOT NULL,
    hours           REAL NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS time_log_engagement ON time_log(engagement_slug);
CREATE UNIQUE INDEX IF NOT EXISTS time_log_source_event
    ON time_log(engagement_slug, source_event_id) WHERE source_event_id <> '';
"""


@dataclass
class TimeLogEntry:
    date: str
    hours: float
    note: str = ""
    source_event_id: str = ""


@dataclass
class Contact:
    name: str
    email: str = ""
    role: str = ""


@dataclass
class Engagement:
    title: str
    slug: str
    type: str
    status: str = "active"
    client: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    hours_committed_per_week: float | None = None
    calendar_account: str | None = None
    contacts: list[Contact] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    time_log: list[TimeLogEntry] = field(default_factory=list)
    notes: str = ""

    @property
    def total_hours(self) -> float:
        return sum(e.hours for e in self.time_log)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s or "untitled"


def _connect():
    return psycopg.connect(
        database_url(), prepare_threshold=None, connect_timeout=20, row_factory=dict_row
    )


def ensure_schema() -> None:
    with _connect() as conn:
        conn.execute(SCHEMA)
        conn.commit()


def _row_to_engagement(conn, row: dict) -> Engagement:
    slug = row["slug"]
    contacts = [
        Contact(name=c["name"], email=c["email"], role=c["role"])
        for c in conn.execute(
            "SELECT name, email, role FROM contacts WHERE engagement_slug = %s ORDER BY id",
            (slug,),
        ).fetchall()
    ]
    time_log = [
        TimeLogEntry(date=str(t["entry_date"]), hours=t["hours"], note=t["note"],
                     source_event_id=t["source_event_id"])
        for t in conn.execute(
            "SELECT entry_date, hours, note, source_event_id FROM time_log "
            "WHERE engagement_slug = %s ORDER BY entry_date, id",
            (slug,),
        ).fetchall()
    ]
    return Engagement(
        title=row["title"],
        slug=slug,
        type=row["type"],
        status=row["status"],
        client=row["client"],
        start_date=str(row["start_date"]) if row["start_date"] else None,
        end_date=str(row["end_date"]) if row["end_date"] else None,
        hours_committed_per_week=row["hours_committed_per_week"],
        calendar_account=row["calendar_account"],
        contacts=contacts,
        links=list(row["links"] or []),
        tags=list(row["tags"] or []),
        time_log=time_log,
        notes=row["notes"] or "",
    )


def load(slug: str) -> Engagement:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM engagements WHERE slug = %s", (slug,)).fetchone()
        if not row:
            raise FileNotFoundError(f"engagement '{slug}' not found")
        return _row_to_engagement(conn, row)


def list_engagements(*, client: str | None = None, status: str | None = None,
                     type: str | None = None) -> list[Engagement]:
    clauses, params = [], []
    if client:
        clauses.append("client = %s")
        params.append(client)
    if status:
        clauses.append("status = %s")
        params.append(status)
    if type:
        clauses.append("type = %s")
        params.append(type)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        rows = conn.execute(f"SELECT * FROM engagements{where}", params).fetchall()
        engs = [_row_to_engagement(conn, r) for r in rows]
    engs.sort(key=lambda e: (e.status != "active", e.title.lower()))
    return engs


def save(eng: Engagement) -> None:
    """Upsert the engagement and its contacts. Does not touch time_log —
    use log_time() or add_time_entries() for that."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO engagements
            (slug, title, type, status, client, start_date, end_date,
             hours_committed_per_week, calendar_account, links, tags, notes, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (slug) DO UPDATE SET
              title=excluded.title, type=excluded.type, status=excluded.status,
              client=excluded.client, start_date=excluded.start_date,
              end_date=excluded.end_date,
              hours_committed_per_week=excluded.hours_committed_per_week,
              calendar_account=excluded.calendar_account, links=excluded.links,
              tags=excluded.tags, notes=excluded.notes, updated_at=now()""",
            (eng.slug, eng.title, eng.type, eng.status, eng.client,
             eng.start_date, eng.end_date, eng.hours_committed_per_week,
             eng.calendar_account, json.dumps(eng.links), json.dumps(eng.tags), eng.notes),
        )
        conn.execute("DELETE FROM contacts WHERE engagement_slug = %s", (eng.slug,))
        for c in eng.contacts:
            conn.execute(
                "INSERT INTO contacts (engagement_slug, name, email, role) VALUES (%s,%s,%s,%s)",
                (eng.slug, c.name, c.email, c.role),
            )
        conn.commit()


def log_time(slug: str, *, hours: float, note: str = "", when: str | None = None) -> Engagement:
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM engagements WHERE slug = %s", (slug,)).fetchone():
            raise FileNotFoundError(f"engagement '{slug}' not found")
        conn.execute(
            "INSERT INTO time_log (engagement_slug, entry_date, hours, note) VALUES (%s,%s,%s,%s)",
            (slug, when or date.today().isoformat(), hours, note),
        )
        conn.commit()
    return load(slug)


def add_time_entries(slug: str, entries: list[TimeLogEntry]) -> int:
    """Bulk-insert time entries. Entries with a source_event_id that already
    exists for this engagement are skipped (dedup). Returns rows inserted."""
    inserted = 0
    with _connect() as conn:
        for t in entries:
            cur = conn.execute(
                """INSERT INTO time_log (engagement_slug, entry_date, hours, note, source_event_id)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (engagement_slug, source_event_id)
                  WHERE source_event_id <> '' DO NOTHING""",
                (slug, t.date, t.hours, t.note, t.source_event_id),
            )
            inserted += cur.rowcount
        conn.commit()
    return inserted


def create(*, title: str, type: str, client: str | None = None, status: str = "active",
           hours_per_week: float | None = None, start_date: str | None = None) -> Engagement:
    if type not in ("client", "community"):
        raise ValueError("type must be 'client' or 'community'")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    if type == "client" and not client:
        raise ValueError("client engagements require a client")
    slug = slugify(title)
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM engagements WHERE slug = %s", (slug,)).fetchone():
            raise FileExistsError(f"engagement '{slug}' already exists")
    eng = Engagement(
        title=title, slug=slug, type=type, status=status, client=client,
        start_date=start_date or date.today().isoformat(),
        hours_committed_per_week=hours_per_week,
    )
    save(eng)
    return eng
