from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import db
from .auth import load_credentials
from .config import Config, load

AGGREGATED_KEY = "aggregatedFrom"
CALENDAR_ID_STATE_KEY = "aggregate_calendar_id"

# source account label → Google event colorId
_COLOR_BY_LABEL = {
    "personal": "3",    # Grape
    "forever22": "5",   # Banana
    "betaworks": "10",  # Basil
    "andus": "9",       # Blueberry
}


@dataclass
class AggregateReport:
    started_at: str
    finished_at: str
    created: int = 0
    updated: int = 0
    deleted: int = 0
    skipped_duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    calendar_id: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        line = (f"  +{self.created} created, ~{self.updated} updated, "
                f"-{self.deleted} deleted, {self.skipped_duplicates} duplicates skipped")
        if self.errors:
            line += f"\n  errors: {len(self.errors)}"
        return line


def _is_synthetic(raw: dict) -> bool:
    priv = raw.get("extendedProperties", {}).get("private", {})
    return "mirroredFrom" in priv or "f22Block" in priv or AGGREGATED_KEY in priv


def _start_end_fields(raw: dict) -> tuple[dict, dict]:
    s, e = raw.get("start", {}), raw.get("end", {})
    if "date" in s:
        return {"date": s["date"]}, {"date": e["date"]}
    out_s = {"dateTime": s["dateTime"]}
    if s.get("timeZone"):
        out_s["timeZone"] = s["timeZone"]
    out_e = {"dateTime": e["dateTime"]}
    if e.get("timeZone"):
        out_e["timeZone"] = e["timeZone"]
    return out_s, out_e


def _ensure_calendar(service, cfg: Config, conn) -> str:
    existing = db.get_state(conn, CALENDAR_ID_STATE_KEY)
    if existing:
        try:
            service.calendars().get(calendarId=existing).execute()
            return existing
        except HttpError as e:
            if e.resp.status not in (404, 410):
                raise
    created = service.calendars().insert(body={
        "summary": cfg.aggregate.calendar_name,
        "description": "Unified view of all forever22 calendars. Managed by forever22 — do not hand-edit.",
        "timeZone": "America/New_York",
    }).execute()
    db.set_state(conn, CALENDAR_ID_STATE_KEY, created["id"])
    return created["id"]


def run(*, cfg: Config | None = None) -> AggregateReport:
    cfg = cfg or load()
    started = datetime.now(timezone.utc).isoformat()
    report = AggregateReport(started_at=started, finished_at=started)

    if not cfg.aggregate.enabled:
        return report

    host = cfg.aggregate.host_account
    try:
        creds = load_credentials(host, cfg=cfg)
        host_service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        report.errors.append(f"host {host} auth: {e}")
        report.finished_at = datetime.now(timezone.utc).isoformat()
        return report

    now = datetime.now(timezone.utc).isoformat()

    with db.connect() as conn:
        try:
            cal_id = _ensure_calendar(host_service, cfg, conn)
        except Exception as e:
            report.errors.append(f"calendar setup: {e}")
            report.finished_at = datetime.now(timezone.utc).isoformat()
            return report
        report.calendar_id = cal_id

        rows = conn.execute(
            """SELECT * FROM events
            WHERE COALESCE(status, 'confirmed') != 'cancelled'
            ORDER BY start_iso"""
        ).fetchall()

        unique: dict[str, dict] = {}
        for r in rows:
            try:
                raw = json.loads(r["raw_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if _is_synthetic(raw):
                continue
            ical = raw.get("iCalUID") or f"{r['account_label']}:{r['event_id']}"
            # Dedup key = iCalUID + start. Same meeting on two calendars collapses;
            # distinct occurrences of a recurring series stay separate.
            dedup_key = f"{ical}|{r['start_iso']}"
            if dedup_key in unique:
                report.skipped_duplicates += 1
                continue
            unique[dedup_key] = {"row": r, "raw": raw}

        seen_uids = set(unique.keys())

        for uid, item in unique.items():
            r = item["row"]
            raw = item["raw"]
            existing = conn.execute(
                "SELECT * FROM aggregated WHERE ical_uid = ?", (uid,)
            ).fetchone()
            start, end = _start_end_fields(raw)
            body = {
                "summary": r["summary"] or "(no title)",
                "start": start,
                "end": end,
                "description": f"From {r['account_label']} · unified by forever22.",
                "extendedProperties": {
                    "private": {AGGREGATED_KEY: f"{r['account_label']}:{r['event_id']}"}
                },
                "reminders": {"useDefault": False},
            }
            color = _COLOR_BY_LABEL.get(r["account_label"])
            if color:
                body["colorId"] = color

            if existing is None:
                try:
                    created = host_service.events().insert(calendarId=cal_id, body=body).execute()
                    conn.execute(
                        """INSERT INTO aggregated
                        (ical_uid, source_account, source_event_id, agg_event_id,
                         start_iso, end_iso, summary, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (uid, r["account_label"], r["event_id"], created["id"],
                         r["start_iso"], r["end_iso"], r["summary"], now),
                    )
                    report.created += 1
                except HttpError as e:
                    report.errors.append(f"create {uid}: {e._get_reason()}")
            else:
                if (existing["start_iso"] != r["start_iso"]
                        or existing["end_iso"] != r["end_iso"]
                        or (existing["summary"] or "") != (r["summary"] or "")):
                    try:
                        host_service.events().patch(
                            calendarId=cal_id, eventId=existing["agg_event_id"], body=body
                        ).execute()
                        conn.execute(
                            """UPDATE aggregated SET start_iso=?, end_iso=?, summary=?, updated_at=?
                            WHERE ical_uid=?""",
                            (r["start_iso"], r["end_iso"], r["summary"], now, uid),
                        )
                        report.updated += 1
                    except HttpError as e:
                        if e.resp.status in (404, 410):
                            conn.execute("DELETE FROM aggregated WHERE ical_uid=?", (uid,))
                        else:
                            report.errors.append(f"update {uid}: {e._get_reason()}")

        for agg in conn.execute("SELECT * FROM aggregated").fetchall():
            if agg["ical_uid"] in seen_uids:
                continue
            try:
                host_service.events().delete(
                    calendarId=cal_id, eventId=agg["agg_event_id"]
                ).execute()
            except HttpError as e:
                if e.resp.status not in (404, 410):
                    report.errors.append(f"delete {agg['ical_uid']}: {e._get_reason()}")
                    continue
            except Exception as e:
                report.errors.append(f"delete {agg['ical_uid']}: {e}")
                continue
            conn.execute("DELETE FROM aggregated WHERE ical_uid=?", (agg["ical_uid"],))
            report.deleted += 1

    report.finished_at = datetime.now(timezone.utc).isoformat()
    return report
