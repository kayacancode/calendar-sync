from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import bestmate, db
from .auth import load_credentials
from .config import Account, Config, load


@dataclass
class AccountSyncResult:
    label: str
    kept: int = 0
    removed: int = 0
    error: str | None = None


@dataclass
class SyncReport:
    started_at: str
    finished_at: str
    results: list[AccountSyncResult]

    @property
    def ok(self) -> bool:
        return all(r.error is None for r in self.results)

    def summary(self) -> str:
        lines = []
        for r in self.results:
            if r.error:
                lines.append(f"  {r.label}: ERROR — {r.error}")
            else:
                lines.append(f"  {r.label}: {r.kept} events, -{r.removed} removed")
        return "\n".join(lines)


def _event_times(event: dict) -> tuple[str, str] | None:
    start = event.get("start", {})
    end = event.get("end", {})
    if "dateTime" in start and "dateTime" in end:
        return start["dateTime"], end["dateTime"]
    if "date" in start and "date" in end:
        return start["date"] + "T00:00:00+00:00", end["date"] + "T00:00:00+00:00"
    return None


def _is_busy(event: dict) -> bool:
    return event.get("transparency", "opaque") != "transparent"


def _sync_account(service, account: Account, cfg: Config, conn) -> AccountSyncResult:
    result = AccountSyncResult(label=account.label)
    now = datetime.now(timezone.utc)
    synced_at = now.isoformat()
    time_min = (now - timedelta(days=cfg.sync.past_days)).isoformat()
    time_max = (now + timedelta(days=cfg.sync.future_days)).isoformat()

    fetched: list[dict] = []
    page_token = None
    while True:
        params = {
            "calendarId": "primary",
            "singleEvents": True,
            "orderBy": "startTime",
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": 250,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            response = service.events().list(**params).execute()
        except HttpError as e:
            result.error = f"HTTP {e.resp.status}: {e._get_reason()}"
            return result
        except Exception as e:
            result.error = str(e)
            return result
        fetched.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    existing_ids = {
        row["event_id"]
        for row in conn.execute(
            "SELECT event_id FROM events WHERE account_label = %s", (account.label,)
        )
    }
    conn.execute("DELETE FROM events WHERE account_label = %s", (account.label,))

    new_ids: set[str] = set()
    for event in fetched:
        event_id = event.get("id")
        if not event_id or event.get("status") == "cancelled":
            continue
        times = _event_times(event)
        if not times:
            continue
        start_iso, end_iso = times
        db.upsert_event(
            conn,
            account_label=account.label,
            event_id=event_id,
            calendar_id=event.get("organizer", {}).get("email", "primary"),
            start_iso=start_iso,
            end_iso=end_iso,
            summary=event.get("summary"),
            status=event.get("status"),
            is_busy=_is_busy(event),
            raw_json=json.dumps(event, separators=(",", ":")),
            synced_at=synced_at,
        )
        new_ids.add(event_id)

    result.kept = len(new_ids)
    result.removed = len(existing_ids - new_ids)
    db.set_sync_state(
        conn,
        account_label=account.label,
        sync_token=None,
        last_full_at=synced_at,
        last_run_at=synced_at,
    )
    return result


def run(*, cfg: Config | None = None, labels: list[str] | None = None) -> SyncReport:
    cfg = cfg or load()
    targets = cfg.accounts if not labels else [cfg.account(l) for l in labels]
    started = datetime.now(timezone.utc).isoformat()
    results: list[AccountSyncResult] = []
    with db.connect() as conn:
        for account in targets:
            try:
                creds = load_credentials(account.label, cfg=cfg)
                service = build("calendar", "v3", credentials=creds, cache_discovery=False)
                results.append(_sync_account(service, account, cfg, conn))
            except Exception as e:
                results.append(AccountSyncResult(label=account.label, error=str(e)))
    finished = datetime.now(timezone.utc).isoformat()
    report = SyncReport(started_at=started, finished_at=finished, results=results)

    if cfg.sync.bestmate_digest and report.ok:
        bestmate.ingest(
            _format_digest(report),
            title=f"forever22 sync {started[:10]}",
            tags=["forever22", "calendar-sync"],
            visibility=cfg.bestmate.visibility,
        )
    return report


def _format_digest(report: SyncReport) -> str:
    lines = [f"forever22 calendar sync at {report.finished_at}", ""]
    for r in report.results:
        if r.error:
            lines.append(f"- {r.label}: error — {r.error}")
        else:
            lines.append(f"- {r.label}: {r.kept} events cached, {r.removed} removed")
    return "\n".join(lines)
