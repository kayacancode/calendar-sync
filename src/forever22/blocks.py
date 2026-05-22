from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import dateparser
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import db
from .auth import load_credentials
from .config import Config, load

BLOCK_KEY = "f22Block"


@dataclass
class Block:
    block_id: str
    mode: str
    reason: str
    start_iso: str
    end_iso: str
    target_label: str | None
    affected_accounts: list[str]


@dataclass
class BlockOp:
    block: Block
    errors: list[str]


class BlockTimeError(ValueError):
    pass


_SHORTHAND_RE = re.compile(
    r"(\d{1,2})(:\d{2})?\s*-\s*(\d{1,2})(:\d{2})?\s*(am|pm)",
    flags=re.IGNORECASE,
)


def _hour24(h: int, suffix: str) -> int:
    if suffix == "am":
        return 0 if h == 12 else h
    return 12 if h == 12 else h + 12


def _expand_shorthand(m: re.Match) -> str:
    left_hour = int(m.group(1))
    left_min = m.group(2) or ""
    right_hour = int(m.group(3))
    right_min = m.group(4) or ""
    right_suffix = m.group(5).lower()
    right_24 = _hour24(right_hour, right_suffix)
    # pick the left suffix that produces a forward (start < end) range
    left_same = _hour24(left_hour, right_suffix)
    if left_same < right_24:
        left_suffix = right_suffix
    else:
        left_suffix = "am" if right_suffix == "pm" else "pm"
    return f"{left_hour}{left_min}{left_suffix} to {right_hour}{right_min}{right_suffix}"


def _normalize_range(when: str) -> str:
    when = when.replace("–", "-").replace("—", "-")
    return _SHORTHAND_RE.sub(_expand_shorthand, when)


def parse_when(when: str) -> tuple[datetime, datetime]:
    normalized = _normalize_range(when)
    if " to " in normalized:
        start_text, end_text = [s.strip() for s in normalized.split(" to ", 1)]
    elif "-" in normalized:
        idx = normalized.rfind("-")
        start_text = normalized[:idx].strip()
        end_text = normalized[idx + 1:].strip()
    else:
        raise BlockTimeError(
            f"need a range like 'tomorrow 2-4pm' or 'May 20 14:00 to 16:00', got: {when!r}"
        )

    start = dateparser.parse(start_text, settings={"PREFER_DATES_FROM": "future"})
    if start is None:
        raise BlockTimeError(f"could not parse start time {start_text!r} in {when!r}")
    end = dateparser.parse(end_text, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": start})
    if end is None:
        raise BlockTimeError(f"could not parse end time {end_text!r} in {when!r}")
    if end <= start:
        raise BlockTimeError(f"end must be after start ({start} → {end})")
    return _coerce(start), _coerce(end)


def _coerce(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def _new_block_id() -> str:
    return secrets.token_hex(6)


def _block_body(reason: str, start: datetime, end: datetime, block_id: str, target_label: str | None) -> dict:
    return {
        "summary": reason,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "transparency": "opaque",
        "visibility": "private",
        "description": (
            f"Held by forever22 block."
            + (f" Reserved for {target_label}." if target_label else " Universal busy.")
        ),
        "extendedProperties": {"private": {BLOCK_KEY: block_id}},
        "reminders": {"useDefault": False},
    }


def _target_accounts(cfg: Config, mode: str, target_label: str | None) -> list[str]:
    all_labels = [a.label for a in cfg.accounts]
    if mode == "busy":
        return all_labels
    if mode == "reserved-for":
        if target_label is None or target_label not in all_labels:
            raise ValueError(f"reserved-for mode needs a valid --for; got {target_label!r}")
        return [l for l in all_labels if l != target_label]
    raise ValueError(f"unknown mode: {mode}")


def create(*, reason: str, when: str, mode: str = "busy",
           target_label: str | None = None, cfg: Config | None = None) -> BlockOp:
    cfg = cfg or load()
    start, end = parse_when(when)
    block_id = _new_block_id()
    affected = _target_accounts(cfg, mode, target_label)
    block = Block(
        block_id=block_id,
        mode=mode,
        reason=reason,
        start_iso=start.isoformat(),
        end_iso=end.isoformat(),
        target_label=target_label,
        affected_accounts=affected,
    )
    errors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    body = _block_body(reason, start, end, block_id, target_label)

    with db.connect() as conn:
        for label in affected:
            try:
                creds = load_credentials(label, cfg=cfg)
                service = build("calendar", "v3", credentials=creds, cache_discovery=False)
                created = service.events().insert(calendarId="primary", body=body).execute()
                conn.execute(
                    """INSERT INTO blocks (block_id, account_label, event_id, mode, target_label,
                                           start_iso, end_iso, reason, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (block_id, label, created["id"], mode, target_label,
                     block.start_iso, block.end_iso, reason, now),
                )
            except HttpError as e:
                errors.append(f"{label}: {e._get_reason()}")
            except Exception as e:
                errors.append(f"{label}: {e}")
    return BlockOp(block=block, errors=errors)


def list_blocks(*, future_only: bool = True) -> list[Block]:
    out: dict[str, Block] = {}
    now_iso = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        sql = "SELECT * FROM blocks"
        if future_only:
            sql += " WHERE end_iso > %s"
            rows = conn.execute(sql + " ORDER BY start_iso", (now_iso,)).fetchall()
        else:
            rows = conn.execute(sql + " ORDER BY start_iso").fetchall()
    for r in rows:
        bid = r["block_id"]
        if bid not in out:
            out[bid] = Block(
                block_id=bid,
                mode=r["mode"],
                reason=r["reason"],
                start_iso=r["start_iso"],
                end_iso=r["end_iso"],
                target_label=r["target_label"],
                affected_accounts=[],
            )
        out[bid].affected_accounts.append(r["account_label"])
    return list(out.values())


def delete(block_id: str, *, cfg: Config | None = None) -> tuple[int, list[str]]:
    cfg = cfg or load()
    errors: list[str] = []
    deleted = 0
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM blocks WHERE block_id = %s", (block_id,)).fetchall()
        if not rows:
            raise KeyError(f"no block with id {block_id}")
        for r in rows:
            label = r["account_label"]
            event_id = r["event_id"]
            try:
                creds = load_credentials(label, cfg=cfg)
                service = build("calendar", "v3", credentials=creds, cache_discovery=False)
                service.events().delete(calendarId="primary", eventId=event_id).execute()
            except HttpError as e:
                if e.resp.status not in (404, 410):
                    errors.append(f"{label}: {e._get_reason()}")
                    continue
            except Exception as e:
                errors.append(f"{label}: {e}")
                continue
            conn.execute(
                "DELETE FROM blocks WHERE block_id = %s AND account_label = %s",
                (block_id, label),
            )
            deleted += 1
    return deleted, errors
