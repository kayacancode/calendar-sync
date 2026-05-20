from __future__ import annotations

import json
from datetime import datetime, timedelta, time, timezone
from typing import Iterable

from dateutil import parser as dtparser
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import db
from .config import Account, Config, load


def _local_midnight(dt: datetime) -> datetime:
    return datetime.combine(dt.date(), time.min).astimezone()


def _account_color(cfg: Config, label: str) -> str:
    try:
        return cfg.account(label).color
    except KeyError:
        return "white"


def _account_lookup(cfg: Config) -> dict[str, Account]:
    return {a.label: a for a in cfg.accounts}


def _parse(iso: str) -> datetime:
    dt = dtparser.isoparse(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def today(*, cfg: Config | None = None, console: Console | None = None) -> None:
    cfg = cfg or load()
    console = console or Console()
    start = _local_midnight(datetime.now().astimezone())
    end = start + timedelta(days=1)
    _print_day(console, cfg, start, end, header="Today")


def week(*, cfg: Config | None = None, console: Console | None = None, days: int = 7) -> None:
    cfg = cfg or load()
    console = console or Console()
    start = _local_midnight(datetime.now().astimezone())
    for i in range(days):
        day_start = start + timedelta(days=i)
        day_end = day_start + timedelta(days=1)
        _print_day(console, cfg, day_start, day_end, header=day_start.strftime("%A %b %d"))
        console.print()


def _print_day(console: Console, cfg: Config, start: datetime, end: datetime, *, header: str) -> None:
    with db.connect() as conn:
        rows = db.events_in_range(conn, start_iso=start.isoformat(), end_iso=end.isoformat())
    table = Table(title=f"[bold]{header}[/bold]  ({start.strftime('%Y-%m-%d')})",
                  expand=True, show_lines=False, header_style="bold")
    table.add_column("When", width=18)
    table.add_column("Account", width=10)
    table.add_column("Event", overflow="fold")
    if not rows:
        table.add_row("—", "—", Text("(no events)", style="dim"))
        console.print(table)
        return
    for row in rows:
        s = _parse(row["start_iso"])
        e = _parse(row["end_iso"])
        when = f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}"
        if s.date() != start.date() and e.date() != start.date():
            when = "all day"
        color = _account_color(cfg, row["account_label"])
        account = Text(row["account_label"], style=color)
        summary = row["summary"] or "(no title)"
        if not row["is_busy"]:
            summary = f"[dim]{summary}[/dim]"
        table.add_row(when, account, summary)
    console.print(table)


def status(*, cfg: Config | None = None, console: Console | None = None) -> None:
    cfg = cfg or load()
    console = console or Console()
    accounts = _account_lookup(cfg)
    with db.connect() as conn:
        rows = db.sync_status(conn)
    by_label = {r["account_label"]: r for r in rows}

    table = Table(title="forever22 status", show_lines=False)
    table.add_column("Label")
    table.add_column("Email")
    table.add_column("Events cached")
    table.add_column("Last sync")
    for label, account in accounts.items():
        info = by_label.get(label)
        events = str(info["event_count"]) if info else "0"
        last = info["last_run_at"] if info else "never"
        table.add_row(
            Text(label, style=account.color),
            account.email,
            events,
            last,
        )
    console.print(table)
