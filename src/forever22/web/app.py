from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from dateutil import parser as dtparser
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import bestmate, db, engagements
from ..config import Account, Config, load

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

app = FastAPI(title="forever22")
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


def _cfg() -> Config:
    return load()


def _accounts(cfg: Config) -> dict[str, Account]:
    return {a.label: a for a in cfg.accounts}


def _local_midnight(dt: datetime) -> datetime:
    return datetime.combine(dt.date(), time.min).astimezone()


def _parse(iso: str) -> datetime:
    d = dtparser.isoparse(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone()


def _events_in_range(start: datetime, end: datetime):
    with db.connect() as conn:
        rows = db.events_in_range(conn, start_iso=start.isoformat(), end_iso=end.isoformat())
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(r) -> dict:
    s, e = _parse(r["start_iso"]), _parse(r["end_iso"])
    return {
        "label": r["account_label"],
        "start": s,
        "end": e,
        "when": f"{s.strftime('%H:%M')}–{e.strftime('%H:%M')}",
        "summary": r["summary"] or "(no title)",
        "is_busy": bool(r["is_busy"]),
    }


def _base_context(cfg: Config) -> dict:
    return {
        "accounts": cfg.accounts,
        "account_color": {a.label: a.color for a in cfg.accounts},
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return RedirectResponse("/today")


@app.get("/today", response_class=HTMLResponse)
def today(request: Request):
    cfg = _cfg()
    start = _local_midnight(datetime.now().astimezone())
    end = start + timedelta(days=1)
    return TEMPLATES.TemplateResponse(
        request,
        "today.html",
        {
            **_base_context(cfg),
            "day_label": start.strftime("%A · %b %d, %Y"),
            "events": _events_in_range(start, end),
        },
    )


@app.get("/week", response_class=HTMLResponse)
def week(request: Request, days: int = 7):
    cfg = _cfg()
    start = _local_midnight(datetime.now().astimezone())
    days_list = []
    for i in range(days):
        d_start = start + timedelta(days=i)
        d_end = d_start + timedelta(days=1)
        days_list.append({
            "label": d_start.strftime("%A · %b %d"),
            "iso": d_start.date().isoformat(),
            "events": _events_in_range(d_start, d_end),
        })
    return TEMPLATES.TemplateResponse(
        request,
        "week.html",
        {**_base_context(cfg), "days": days_list},
    )


@app.get("/engagements", response_class=HTMLResponse)
def engagements_list(request: Request, client: str | None = None, status: str | None = None):
    cfg = _cfg()
    items = engagements.list_engagements(client=client, status=status)
    return TEMPLATES.TemplateResponse(
        request,
        "engagements.html",
        {**_base_context(cfg), "engagements": items, "filter_client": client, "filter_status": status},
    )


@app.get("/engagements/{slug}", response_class=HTMLResponse)
def engagement_detail(request: Request, slug: str):
    cfg = _cfg()
    try:
        eng = engagements.load(slug)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"engagement '{slug}' not found")
    return TEMPLATES.TemplateResponse(
        request,
        "engagement_detail.html",
        {**_base_context(cfg), "e": eng},
    )


@app.post("/ask")
def ask(query: str = Form(...), redirect_to: str = Form("/")):
    cfg = _cfg()
    result = bestmate.ask(query, target=cfg.bestmate.target)
    answer = result.output if result.ok else f"(bestmate error: {result.error})"
    return RedirectResponse(
        url=f"{redirect_to}?q={query}&a={answer[:2000]}",
        status_code=303,
    )


@app.get("/status")
def web_status():
    with db.connect() as conn:
        rows = db.sync_status(conn)
    return {"accounts": [dict(r) for r in rows]}
