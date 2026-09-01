from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable

from dateutil import parser as dtparser
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import json
import threading

from .. import bestmate, board, db, engagements
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


_running_cards: set[str] = set()
_running_lock = threading.Lock()


def _run_card_bg(card_id: str, trigger: str, research: bool = False) -> None:
    with _running_lock:
        if card_id in _running_cards:
            return
        _running_cards.add(card_id)
    try:
        if research:
            board.research_card(card_id)
        else:
            board.run_card(card_id, trigger=trigger)
    finally:
        with _running_lock:
            _running_cards.discard(card_id)


def _spawn_run(card_id: str, trigger: str, research: bool = False) -> None:
    threading.Thread(target=_run_card_bg, args=(card_id, trigger, research), daemon=True).start()


def _tag_color(tag: str, account_colors: dict[str, str]) -> str:
    for label, color in account_colors.items():
        if label.lower() in tag:
            return color
    hue = sum(ord(ch) * 7 for ch in tag) % 360
    return f"hsl({hue}, 55%, 55%)"


def _card_view(c: dict, runs: dict, unread_counts: dict, pending_titles: list[str],
               account_colors: dict[str, str]) -> dict:
    r = runs.get(c["card_id"])
    return {
        **c,
        "tag_color": _tag_color(c["tag"], account_colors) if c["tag"] else "",
        "blocked": pending_titles,
        "unread": unread_counts.get(c["card_id"], 0),
        "running": c["card_id"] in _running_cards,
        "warn": bool(r and r["status"] != "ok"),
        "last_run_note": (r["note"] or "") if r else "",
        "last_run_at": r["started_at"][:16].replace("T", " ") if r else "",
    }


@app.get("/board", response_class=HTMLResponse)
def board_page(request: Request):
    cfg = _cfg()
    with db.connect() as conn:
        cards = board.active_cards(conn)
        runs = board.last_runs(conn)
        user_msgs = board.unread_messages(conn, None)
        titles = {c["card_id"]: c["title"] for c in cards}
        counts: dict[str, int] = {}
        for m in conn.execute(
            "SELECT to_card, count(*) AS n FROM card_messages WHERE to_card IS NOT NULL AND read_at IS NULL GROUP BY to_card"
        ).fetchall():
            counts[m["to_card"]] = m["n"]
        colors = {a.label: a.color for a in cfg.accounts}
        lanes = {lane: [] for lane in board.LANES}
        for c in cards:
            pending = [b["title"] for b in board.blockers_pending(conn, c)]
            lanes[c["lane"]].append(_card_view(c, runs, counts, pending, colors))
        last_ingest = db.get_state(conn, board.LAST_INGEST_KEY)
    return TEMPLATES.TemplateResponse(request, "board.html", {
        **_base_context(cfg),
        "lanes": lanes,
        "lane_order": board.LANES,
        "user_msgs": [{**m, "from_title": titles.get(m["from_card"], m["from_card"] or "board")} for m in user_msgs],
        "last_ingest": json.loads(last_ingest) if last_ingest else None,
    })


@app.post("/board/cards")
def board_create(title: str = Form(...), prompt: str = Form(""), lane: str = Form("todo")):
    with db.connect() as conn:
        board.create_card(conn, title=title, prompt=prompt, lane=lane)
    return RedirectResponse("/board", status_code=303)


@app.post("/board/cards/{card_id}/move")
def board_move(card_id: str, lane: str = Form(...)):
    with db.connect() as conn:
        card = board.move_card(conn, card_id, lane)
        fire = lane == "in_progress" and not board.blockers_pending(conn, card)
    if fire:
        _spawn_run(card_id, "lane")
    return {"ok": True, "fired": fire}


def _run_view(r: dict, titles: dict[str, str]) -> dict:
    actions = []
    if r["raw_output"]:
        try:
            out = json.loads(r["raw_output"])
            if "queries" in out:
                for q in out["queries"]:
                    actions.append({"tool": "web_search", "detail": q})
                if out.get("findings"):
                    first = next((ln for ln in out["findings"].splitlines() if ln.strip()), "")
                    actions.append({"tool": "update_body",
                                    "detail": f"research · {len(out.get('sources') or [])} sources · {first[:60]}"})
                return {**r, "actions": actions}
            body = out.get("body") or ""
            first = next((ln for ln in body.splitlines() if ln.strip()), "")
            actions.append({"tool": "update_body", "detail": f"{len(body)} chars · {first[:70]}"})
            if out.get("lane_move"):
                actions.append({"tool": "move_card", "detail": f"→ {out['lane_move']}"})
            for m in out.get("messages") or []:
                to = titles.get(m.get("to_card"), "kaya") if m.get("to_card") else "kaya"
                actions.append({"tool": "send_message", "detail": f"→ {to}: {(m.get('body') or '')[:70]}"})
            for b in out.get("bestmate") or []:
                actions.append({"tool": "bestmate_ingest", "detail": b.get("title", "")})
            for bid in out.get("add_blocked_by") or []:
                actions.append({"tool": "add_blocker", "detail": titles.get(bid, bid)})
        except (json.JSONDecodeError, AttributeError):
            pass
    return {**r, "actions": actions}


@app.get("/board/cards/{card_id}", response_class=HTMLResponse)
def board_card(request: Request, card_id: str):
    cfg = _cfg()
    with db.connect() as conn:
        c = board.get_card(conn, card_id)
        if not c:
            raise HTTPException(status_code=404, detail=f"card '{card_id}' not found")
        runs = board.card_runs(conn, card_id)
        thread = board.card_thread(conn, card_id)
        titles = {x["card_id"]: x["title"] for x in board.active_cards(conn)}
        pending = [b["title"] for b in board.blockers_pending(conn, c)]
    return TEMPLATES.TemplateResponse(request, "card_detail.html", {
        **_base_context(cfg),
        "c": c,
        "tag_color": _tag_color(c["tag"], {a.label: a.color for a in cfg.accounts}) if c["tag"] else "",
        "sources": json.loads(c["sources"] or "[]"),
        "blocked_by": json.loads(c["blocked_by"] or "[]"),
        "pending": pending,
        "runs": [_run_view(r, titles) for r in runs],
        "thread": thread,
        "titles": titles,
        "lane_order": board.LANES,
        "running": card_id in _running_cards,
    })


@app.post("/board/cards/{card_id}/edit")
def board_edit(card_id: str, title: str = Form(...), prompt: str = Form(""),
               body: str = Form(""), tag: str = Form(""), blocked_by: str = Form("")):
    blockers = [b.strip() for b in blocked_by.split(",") if b.strip()]
    with db.connect() as conn:
        board.update_card(conn, card_id, title=title, prompt=prompt, body=body, tag=tag, blocked_by=blockers)
    return RedirectResponse(f"/board/cards/{card_id}", status_code=303)


@app.post("/board/cards/{card_id}/message")
def board_message(card_id: str, body: str = Form(...), run_now: str = Form("")):
    with db.connect() as conn:
        if not board.get_card(conn, card_id):
            raise HTTPException(status_code=404)
        board.send_message(conn, from_card=None, to_card=card_id, body=body)
    if run_now:
        _spawn_run(card_id, "manual")
    return RedirectResponse(f"/board/cards/{card_id}", status_code=303)


@app.post("/board/cards/{card_id}/run")
def board_run_now(card_id: str):
    _spawn_run(card_id, "manual")
    return RedirectResponse(f"/board/cards/{card_id}", status_code=303)


@app.post("/board/cards/{card_id}/research")
def board_research(card_id: str):
    _spawn_run(card_id, "research", research=True)
    return RedirectResponse(f"/board/cards/{card_id}", status_code=303)


@app.post("/board/cards/{card_id}/archive")
def board_archive(card_id: str):
    with db.connect() as conn:
        conn.execute("UPDATE cards SET archived = 1, updated_at = %s WHERE card_id = %s",
                     (datetime.now(timezone.utc).isoformat(), card_id))
    return RedirectResponse("/board", status_code=303)


@app.post("/board/ingest")
def board_ingest_now():
    threading.Thread(target=board.ingest, daemon=True).start()
    return RedirectResponse("/board", status_code=303)


@app.post("/board/messages/{msg_id}/read")
def board_msg_read(msg_id: str):
    with db.connect() as conn:
        board.mark_read(conn, msg_id)
    return RedirectResponse("/board", status_code=303)
