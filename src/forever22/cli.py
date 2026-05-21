from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from . import aggregate, auth, autolog, bestmate, blocks, engagements, mirror, sync, view
from .config import load

app = typer.Typer(help="forever22 — unified calendar across Kaya's accounts.", no_args_is_help=True)
eng_app = typer.Typer(help="Track client + community engagements.", no_args_is_help=True)
block_app = typer.Typer(help="Hold time across calendars (busy-everywhere or reserved-for-client).", no_args_is_help=True)
app.add_typer(eng_app, name="eng")
app.add_typer(block_app, name="block")
console = Console()


@app.command("auth")
def cmd_auth(label: str = typer.Option(..., "--label", "-l", help="Account label from config.yaml")) -> None:
    """Authorize one Google account by label."""
    cfg = load()
    cfg.account(label)
    path = auth.authorize(label, cfg=cfg)
    console.print(f"[green]✓[/green] Saved token to {path}")


@app.command("sync")
def cmd_sync(
    label: list[str] = typer.Option(None, "--label", "-l", help="Sync only specific labels (repeatable)"),
    no_mirror: bool = typer.Option(False, "--no-mirror", help="Skip the mirror pass after sync"),
    no_aggregate: bool = typer.Option(False, "--no-aggregate", help="Skip the aggregate pass after sync"),
) -> None:
    """Pull events, mirror busy times, and update the unified calendar."""
    cfg = load()
    labels = list(label) if label else None
    report = sync.run(cfg=cfg, labels=labels)
    console.print(f"[bold]Sync[/bold] finished at {report.finished_at}")
    console.print(report.summary())
    if not report.ok:
        raise typer.Exit(code=1)
    failed = False
    if not no_mirror and cfg.mirror.enabled and cfg.mirror.rules:
        mreport = mirror.run(cfg=cfg)
        console.print(f"\n[bold]Mirror[/bold] finished at {mreport.finished_at}")
        console.print(mreport.summary())
        failed = failed or not mreport.ok
    if not no_aggregate and cfg.aggregate.enabled:
        areport = aggregate.run(cfg=cfg)
        console.print(f"\n[bold]Aggregate[/bold] finished at {areport.finished_at}")
        console.print(areport.summary())
        failed = failed or not areport.ok
    if failed:
        raise typer.Exit(code=1)


@app.command("mirror")
def cmd_mirror() -> None:
    """Mirror busy events across calendars per config.yaml `mirror.rules`."""
    cfg = load()
    if not cfg.mirror.enabled:
        console.print("[yellow]mirror is disabled in config.yaml[/yellow]")
        raise typer.Exit(code=0)
    report = mirror.run(cfg=cfg)
    console.print(f"Mirror finished at {report.finished_at}")
    console.print(report.summary())
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("aggregate")
def cmd_aggregate() -> None:
    """Update the unified 'forever22 — All' calendar with deduplicated real events."""
    cfg = load()
    if not cfg.aggregate.enabled:
        console.print("[yellow]aggregate is disabled in config.yaml[/yellow]")
        raise typer.Exit(code=0)
    report = aggregate.run(cfg=cfg)
    console.print(f"Aggregate finished at {report.finished_at}")
    if report.calendar_id:
        console.print(f"  calendar: {report.calendar_id}")
    console.print(report.summary())
    if not report.ok:
        for err in report.errors:
            console.print(f"  [red]{err}[/red]")
        raise typer.Exit(code=1)


@app.command("today")
def cmd_today() -> None:
    """Show today's merged timeline across all accounts."""
    view.today(console=console)


@app.command("week")
def cmd_week(days: int = typer.Option(7, "--days", "-d", help="How many days to render")) -> None:
    """Show a multi-day grid across all accounts."""
    view.week(console=console, days=days)


@app.command("status")
def cmd_status() -> None:
    """Show sync state per account and bestmate health."""
    view.status(console=console)
    console.print()
    bm = bestmate.status()
    if bm.ok:
        console.print("[green]bestmate:[/green] ok")
    else:
        console.print(f"[red]bestmate:[/red] {bm.error or 'unavailable'}")


@app.command("ask")
def cmd_ask(query: str = typer.Argument(...), target: str = typer.Option(None, "--target", "-t")) -> None:
    """Ask bestmate a question (passthrough convenience)."""
    cfg = load()
    result = bestmate.ask(query, target=target or cfg.bestmate.target)
    if result.ok:
        console.print(result.output)
    else:
        console.print(f"[red]bestmate error:[/red] {result.error}")
        raise typer.Exit(code=1)


@app.command("web")
def cmd_web(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8022, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes"),
) -> None:
    """Start the local web dashboard at http://localhost:8022."""
    import uvicorn
    console.print(f"[green]forever22 web[/green] → http://{host}:{port}")
    uvicorn.run("forever22.web:app", host=host, port=port, reload=reload, log_level="info")


@eng_app.command("list")
def cmd_eng_list(
    client: str = typer.Option(None, "--client", "-c"),
    status: str = typer.Option(None, "--status", "-s"),
    type: str = typer.Option(None, "--type", "-t", help="client or community"),
) -> None:
    """List engagements."""
    items = engagements.list_engagements(client=client, status=status, type=type)
    if not items:
        console.print("[dim](no engagements)[/dim]")
        return
    table = Table(title="engagements", show_lines=False)
    table.add_column("Slug")
    table.add_column("Title")
    table.add_column("Type")
    table.add_column("Client")
    table.add_column("Status")
    table.add_column("Hours logged")
    for e in items:
        status_style = {"active": "green", "paused": "yellow", "completed": "dim"}.get(e.status, "white")
        table.add_row(
            e.slug,
            e.title,
            e.type,
            e.client or "",
            Text(e.status, style=status_style),
            f"{e.total_hours:.1f}",
        )
    console.print(table)


@eng_app.command("add")
def cmd_eng_add(
    title: str = typer.Option(..., "--title", "-T", prompt=True),
    type: str = typer.Option("client", "--type", "-t", prompt="Type (client/community)"),
    client: str = typer.Option(None, "--client", "-c"),
    status: str = typer.Option("active", "--status", "-s"),
    hours_per_week: float = typer.Option(None, "--hours-per-week", "-h"),
) -> None:
    """Create a new engagement."""
    if type == "client" and not client:
        client = typer.prompt("Client label (e.g. betaworks, andus)")
    eng = engagements.create(
        title=title, type=type, client=client, status=status, hours_per_week=hours_per_week,
    )
    console.print(f"[green]✓[/green] created {eng.slug} at {eng.path}")


@eng_app.command("show")
def cmd_eng_show(slug: str = typer.Argument(...)) -> None:
    """Show an engagement's details."""
    e = engagements.load(slug)
    console.print(f"[bold]{e.title}[/bold]  ({e.slug})")
    console.print(f"type={e.type}  client={e.client or '—'}  status={e.status}")
    if e.start_date or e.end_date:
        console.print(f"dates: {e.start_date or '—'} → {e.end_date or '—'}")
    if e.hours_committed_per_week:
        console.print(f"committed: {e.hours_committed_per_week}h/week")
    console.print(f"total logged: {e.total_hours:.1f}h across {len(e.time_log)} entries")
    if e.contacts:
        console.print("contacts:")
        for c in e.contacts:
            console.print(f"  - {c.name} <{c.email}> {c.role}")
    if e.links:
        console.print("links:")
        for l in e.links:
            console.print(f"  - {l}")
    if e.notes:
        console.print("\n[bold]notes[/bold]")
        console.print(e.notes)
    console.print(f"\nfile: {e.path}")


@eng_app.command("log")
def cmd_eng_log(
    slug: str = typer.Argument(...),
    hours: float = typer.Option(..., "--hours", "-h"),
    note: str = typer.Option("", "--note", "-n"),
    when: str = typer.Option(None, "--date", "-d", help="ISO date, defaults to today"),
) -> None:
    """Log time against an engagement."""
    eng = engagements.log_time(slug, hours=hours, note=note, when=when)
    console.print(f"[green]✓[/green] logged {hours}h to {eng.slug} (total: {eng.total_hours:.1f}h)")


@app.command("autolog")
def cmd_autolog(
    lookback_days: int = typer.Option(30, "--lookback-days", "-d", help="How far back to scan"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without writing to engagement files"),
) -> None:
    """Auto-log completed client meetings as time entries on their engagements.

    Scans each engagement's `calendar_account`, logs finished meetings that have
    other attendees, and dedupes by event ID so nothing is logged twice.
    """
    cfg = load()
    report = autolog.run(cfg=cfg, lookback_days=lookback_days, dry_run=dry_run)
    if report.dry_run:
        console.print("[yellow]dry run — no engagement files written[/yellow]")
    console.print(report.summary())
    if not report.ok:
        raise typer.Exit(code=1)


@block_app.command("create")
def cmd_block_create(
    reason: str = typer.Argument(..., help="Title shown on the held-time events, e.g. 'Focus' or 'Andus office hours'"),
    when: str = typer.Option(..., "--when", "-w", help="Time range, e.g. 'tomorrow 2-4pm', 'Wed 9-11am', '2026-05-20 14:00 to 16:00'"),
    for_label: str = typer.Option(None, "--for", "-f", help="Reserve for only this account label (e.g. andus). Omit for universal busy."),
) -> None:
    """Hold time across calendars. Without --for, blocks all calendars busy. With --for, blocks every calendar EXCEPT that one."""
    mode = "reserved-for" if for_label else "busy"
    cfg = load()
    if for_label and for_label not in {a.label for a in cfg.accounts}:
        console.print(f"[red]unknown account label: {for_label}[/red]. Valid: {', '.join(a.label for a in cfg.accounts)}")
        raise typer.Exit(code=1)
    try:
        op = blocks.create(reason=reason, when=when, mode=mode, target_label=for_label, cfg=cfg)
    except blocks.BlockTimeError as e:
        console.print(f"[red]time parse error:[/red] {e}")
        raise typer.Exit(code=1)

    summary = (
        f"[green]✓[/green] block [bold]{op.block.block_id}[/bold] "
        f"({op.block.mode}{' → ' + op.block.target_label if op.block.target_label else ''})"
    )
    console.print(summary)
    console.print(f"  when: {op.block.start_iso} → {op.block.end_iso}")
    console.print(f"  affected calendars: {', '.join(op.block.affected_accounts)}")
    if op.errors:
        console.print(f"[yellow]warnings:[/yellow]")
        for err in op.errors:
            console.print(f"  - {err}")


@block_app.command("list")
def cmd_block_list(all: bool = typer.Option(False, "--all", help="Include past blocks")) -> None:
    """List held-time blocks."""
    items = blocks.list_blocks(future_only=not all)
    if not items:
        console.print("[dim](no blocks)[/dim]")
        return
    table = Table(title="held-time blocks", show_lines=False)
    table.add_column("ID")
    table.add_column("Reason")
    table.add_column("Mode")
    table.add_column("Reserved-for")
    table.add_column("Start")
    table.add_column("End")
    table.add_column("On calendars")
    for b in items:
        table.add_row(
            b.block_id,
            b.reason,
            b.mode,
            b.target_label or "",
            b.start_iso,
            b.end_iso,
            ", ".join(b.affected_accounts),
        )
    console.print(table)


@block_app.command("delete")
def cmd_block_delete(block_id: str = typer.Argument(...)) -> None:
    """Remove a held-time block from all affected calendars."""
    try:
        n, errors = blocks.delete(block_id)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
    console.print(f"[green]✓[/green] removed block {block_id} from {n} calendar(s)")
    if errors:
        console.print(f"[yellow]warnings:[/yellow]")
        for err in errors:
            console.print(f"  - {err}")


if __name__ == "__main__":
    app()
