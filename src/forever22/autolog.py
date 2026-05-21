from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from dateutil import parser as dtparser
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import engagements
from .auth import load_credentials
from .config import Config, load


@dataclass
class AutologResult:
    engagement_slug: str
    account: str
    logged: int = 0
    hours: float = 0.0
    skipped_existing: int = 0
    error: str | None = None


@dataclass
class AutologReport:
    started_at: str
    results: list[AutologResult] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(r.error is None for r in self.results)

    def summary(self) -> str:
        if not self.results:
            return "  (no engagements have a calendar_account set — nothing to auto-log)"
        verb = "would log" if self.dry_run else "logged"
        lines = []
        for r in self.results:
            if r.error:
                lines.append(f"  {r.engagement_slug}: ERROR — {r.error}")
            else:
                lines.append(
                    f"  {r.engagement_slug} ← {r.account}: {verb} {r.logged} events "
                    f"/ {r.hours:.2f}h ({r.skipped_existing} already logged)"
                )
        return "\n".join(lines)


def _parse(iso: str) -> datetime:
    d = dtparser.isoparse(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _qualifies(ev: dict, now: datetime) -> bool:
    if ev.get("status") == "cancelled":
        return False
    start, end = ev.get("start", {}), ev.get("end", {})
    if "dateTime" not in start or "dateTime" not in end:
        return False  # all-day or malformed
    if _parse(end["dateTime"]) > now:
        return False  # not finished yet
    if ev.get("transparency", "opaque") == "transparent":
        return False  # marked free, not busy
    priv = ev.get("extendedProperties", {}).get("private", {})
    if priv.keys() & {"mirroredFrom", "f22Block", "aggregatedFrom"}:
        return False  # synthetic event
    attendees = ev.get("attendees", [])
    if not any(not a.get("self") for a in attendees):
        return False  # solo event — not a real meeting
    for a in attendees:
        if a.get("self") and a.get("responseStatus") == "declined":
            return False  # user declined — didn't attend
    return True


def _hours(ev: dict) -> float:
    s = _parse(ev["start"]["dateTime"])
    e = _parse(ev["end"]["dateTime"])
    return round((e - s).total_seconds() / 3600, 2)


def run(*, cfg: Config | None = None, lookback_days: int = 30,
        dry_run: bool = False) -> AutologReport:
    cfg = cfg or load()
    now = datetime.now(timezone.utc)
    report = AutologReport(started_at=now.isoformat(), dry_run=dry_run)
    time_min = (now - timedelta(days=lookback_days)).isoformat()
    time_max = now.isoformat()

    known_accounts = {a.label for a in cfg.accounts}
    by_account: dict[str, str] = {}
    for e in engagements.list_engagements():
        if e.calendar_account and e.calendar_account in known_accounts:
            by_account[e.calendar_account] = e.slug

    for account_label, slug in by_account.items():
        res = AutologResult(engagement_slug=slug, account=account_label)
        try:
            creds = load_credentials(account_label, cfg=cfg)
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        except Exception as ex:
            res.error = str(ex)
            report.results.append(res)
            continue

        fetched: list[dict] = []
        page_token = None
        try:
            while True:
                resp = service.events().list(
                    calendarId="primary", singleEvents=True, orderBy="startTime",
                    timeMin=time_min, timeMax=time_max, maxResults=250, pageToken=page_token,
                ).execute()
                fetched.extend(resp.get("items", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        except HttpError as ex:
            res.error = f"HTTP {ex.resp.status}: {ex._get_reason()}"
            report.results.append(res)
            continue
        except Exception as ex:
            res.error = str(ex)
            report.results.append(res)
            continue

        eng = engagements.load(slug)
        logged_ids = {t.source_event_id for t in eng.time_log if t.source_event_id}
        new_entries: list[engagements.TimeLogEntry] = []
        for ev in fetched:
            if not _qualifies(ev, now):
                continue
            eid = ev.get("id")
            if not eid:
                continue
            if eid in logged_ids:
                res.skipped_existing += 1
                continue
            hrs = _hours(ev)
            new_entries.append(engagements.TimeLogEntry(
                date=ev["start"]["dateTime"][:10],
                hours=hrs,
                note=ev.get("summary", "(no title)"),
                source_event_id=eid,
            ))
            res.logged += 1
            res.hours += hrs

        if new_entries and not dry_run:
            eng.time_log.extend(new_entries)
            eng.time_log.sort(key=lambda t: t.date)
            engagements.save(eng)
        report.results.append(res)

    return report
