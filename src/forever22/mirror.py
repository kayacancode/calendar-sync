from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import bestmate, db
from .auth import load_credentials
from .config import Config, load

MIRRORED_FROM_KEY = "mirroredFrom"


@dataclass
class MirrorCount:
    label: str
    created: int = 0
    adopted: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class MirrorReport:
    started_at: str
    finished_at: str
    counts: dict[str, MirrorCount]

    @property
    def ok(self) -> bool:
        return all(not c.errors for c in self.counts.values())

    def summary(self) -> str:
        if not self.counts:
            return "  (no mirror rules configured)"
        lines = []
        for label, c in self.counts.items():
            err = f" — errors: {len(c.errors)}" if c.errors else ""
            adopted = f", ={c.adopted} adopted" if c.adopted else ""
            lines.append(
                f"  {label}: +{c.created} created{adopted}, ~{c.updated} updated, -{c.deleted} deleted{err}"
            )
        return "\n".join(lines)


def _is_mirror(raw: dict) -> bool:
    return MIRRORED_FROM_KEY in raw.get("extendedProperties", {}).get("private", {})


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


def _mirror_body(cfg: Config, source_label: str, src_row, src_raw: dict) -> dict:
    start, end = _start_end_fields(src_raw)
    return {
        "summary": f"{cfg.mirror.mirror_title} — {source_label}",
        "start": start,
        "end": end,
        "transparency": "opaque",
        "visibility": "private",
        "description": f"Auto-mirrored from {source_label} by forever22.",
        "extendedProperties": {
            "private": {MIRRORED_FROM_KEY: f"{source_label}:{src_row['event_id']}"}
        },
        "reminders": {"useDefault": False},
    }


class _ServiceCache:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cache: dict = {}

    def get(self, label: str):
        if label not in self._cache:
            creds = load_credentials(label, cfg=self.cfg)
            self._cache[label] = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._cache[label]


def run(*, cfg: Config | None = None) -> MirrorReport:
    cfg = cfg or load()
    started = datetime.now(timezone.utc).isoformat()
    counts: dict[str, MirrorCount] = {label: MirrorCount(label=label) for label in cfg.mirror.rules}

    if not cfg.mirror.enabled or not cfg.mirror.rules:
        return MirrorReport(started_at=started, finished_at=started, counts=counts)

    services = _ServiceCache(cfg)
    now_dt = datetime.now(timezone.utc)
    window_start = now_dt.isoformat()
    window_end = (now_dt + timedelta(days=cfg.sync.future_days)).isoformat()
    now = now_dt.isoformat()

    with db.connect() as conn:
        # Adoption map: mirror events already present on each calendar, keyed by
        # (target_label, "<source_label>:<source_event_id>"). Lets a fresh DB
        # take ownership of existing mirrors instead of creating duplicates.
        adopt_map: dict[tuple[str, str], str] = {}
        for r in conn.execute("SELECT account_label, event_id, raw_json FROM events"):
            try:
                raw = json.loads(r["raw_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            mf = raw.get("extendedProperties", {}).get("private", {}).get(MIRRORED_FROM_KEY)
            if mf:
                adopt_map[(r["account_label"], mf)] = r["event_id"]

        for source_label, targets in cfg.mirror.rules.items():
            source_events = conn.execute(
                """
                SELECT * FROM events
                WHERE account_label = ? AND is_busy = 1
                  AND end_iso > ? AND start_iso < ?
                  AND COALESCE(status, 'confirmed') != 'cancelled'
                """,
                (source_label, window_start, window_end),
            ).fetchall()

            for src in source_events:
                try:
                    src_raw = json.loads(src["raw_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if _is_mirror(src_raw):
                    continue

                for target_label in targets:
                    if target_label == source_label:
                        continue
                    existing = conn.execute(
                        """SELECT * FROM mirrors
                        WHERE source_account_label = ? AND source_event_id = ? AND target_account_label = ?""",
                        (source_label, src["event_id"], target_label),
                    ).fetchone()
                    try:
                        target_service = services.get(target_label)
                    except Exception as e:
                        counts[source_label].errors.append(f"{target_label} auth: {e}")
                        continue
                    body = _mirror_body(cfg, source_label, src, src_raw)

                    if existing is None:
                        adopt_id = adopt_map.get((target_label, f"{source_label}:{src['event_id']}"))
                        if adopt_id is not None:
                            conn.execute(
                                """INSERT INTO mirrors
                                (source_account_label, source_event_id, target_account_label, target_event_id,
                                 source_start_iso, source_end_iso, source_summary, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (source_label, src["event_id"], target_label, adopt_id,
                                 src["start_iso"], src["end_iso"], src["summary"], now, now),
                            )
                            counts[source_label].adopted += 1
                            continue
                        try:
                            created = target_service.events().insert(
                                calendarId="primary", body=body
                            ).execute()
                            conn.execute(
                                """INSERT INTO mirrors
                                (source_account_label, source_event_id, target_account_label, target_event_id,
                                 source_start_iso, source_end_iso, source_summary, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (source_label, src["event_id"], target_label, created["id"],
                                 src["start_iso"], src["end_iso"], src["summary"], now, now),
                            )
                            counts[source_label].created += 1
                        except HttpError as e:
                            counts[source_label].errors.append(f"{target_label} create: {e._get_reason()}")
                    else:
                        if (existing["source_start_iso"] != src["start_iso"]
                                or existing["source_end_iso"] != src["end_iso"]
                                or (existing["source_summary"] or "") != (src["summary"] or "")):
                            try:
                                target_service.events().patch(
                                    calendarId="primary",
                                    eventId=existing["target_event_id"],
                                    body=body,
                                ).execute()
                                conn.execute(
                                    """UPDATE mirrors SET
                                    source_start_iso = ?, source_end_iso = ?, source_summary = ?, updated_at = ?
                                    WHERE source_account_label = ? AND source_event_id = ? AND target_account_label = ?""",
                                    (src["start_iso"], src["end_iso"], src["summary"], now,
                                     source_label, src["event_id"], target_label),
                                )
                                counts[source_label].updated += 1
                            except HttpError as e:
                                if e.resp.status in (404, 410):
                                    conn.execute(
                                        """DELETE FROM mirrors
                                        WHERE source_account_label = ? AND source_event_id = ? AND target_account_label = ?""",
                                        (source_label, src["event_id"], target_label),
                                    )
                                else:
                                    counts[source_label].errors.append(f"{target_label} update: {e._get_reason()}")

        # A mirror is an orphan if its source event is gone/cancelled, OR if
        # the (source -> target) pair is no longer allowed by mirror.rules
        # (e.g. a target was removed from config). Both get cleaned up.
        all_mirrors = conn.execute(
            """
            SELECT m.*,
              CASE WHEN e.event_id IS NOT NULL
                        AND COALESCE(e.status, 'confirmed') != 'cancelled'
                   THEN 1 ELSE 0 END AS source_alive
            FROM mirrors m
            LEFT JOIN events e
              ON e.account_label = m.source_account_label AND e.event_id = m.source_event_id
            """
        ).fetchall()
        orphans = [
            m for m in all_mirrors
            if not m["source_alive"]
            or m["target_account_label"] not in cfg.mirror.rules.get(m["source_account_label"], [])
        ]

        for orphan in orphans:
            src_label = orphan["source_account_label"]
            target_label = orphan["target_account_label"]
            counter = counts.get(src_label) or counts.setdefault(src_label, MirrorCount(label=src_label))
            try:
                target_service = services.get(target_label)
                target_service.events().delete(
                    calendarId="primary",
                    eventId=orphan["target_event_id"],
                ).execute()
            except HttpError as e:
                if e.resp.status not in (404, 410):
                    counter.errors.append(f"{target_label} delete: {e._get_reason()}")
                    continue
            except Exception as e:
                counter.errors.append(f"{target_label} delete: {e}")
                continue
            conn.execute(
                """DELETE FROM mirrors
                WHERE source_account_label = ? AND source_event_id = ? AND target_account_label = ?""",
                (src_label, orphan["source_event_id"], target_label),
            )
            counter.deleted += 1

    finished = datetime.now(timezone.utc).isoformat()
    report = MirrorReport(started_at=started, finished_at=finished, counts=counts)

    if any(c.created or c.updated or c.deleted for c in counts.values()):
        bestmate.ingest(
            f"forever22 mirror at {finished}\n\n{report.summary()}",
            title=f"forever22 mirror {finished[:10]}",
            tags=["forever22", "calendar-mirror"],
            visibility=cfg.bestmate.visibility,
        )
    return report
