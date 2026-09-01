from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import anthropic
from pydantic import BaseModel

from . import bestmate, db, granola
from .config import Config, anthropic_api_key, load

MODEL = "claude-sonnet-5"
LANES = ["inbox", "todo", "in_progress", "review", "done"]
AGENT_LANE_MOVES = {"review", "todo"}
MAX_RUNS_PER_TICK = 10
WATERMARK_KEY = "board_granola_watermark"
LAST_INGEST_KEY = "board_last_ingest"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


# ---------- agent output schemas (the full write surface: no Salience, ever) ----------

class AgentMessage(BaseModel):
    to_card: str | None      # card_id, or null to notify Kaya
    body: str


class BestmateItem(BaseModel):
    title: str
    content: str
    tags: list[str]


class RunResult(BaseModel):
    body: str                # full replacement card body (markdown, keep dated history)
    lane_move: str | None    # 'review' | 'todo' | null — never 'done'
    messages: list[AgentMessage]
    bestmate: list[BestmateItem]
    add_blocked_by: list[str]
    note: str                # one line: what this run did


class IngestItem(BaseModel):
    action: str              # 'new' | 'update' | 'skip'
    card_id: str | None      # required for 'update'
    title: str
    lane: str                # 'inbox' | 'todo' (only for 'new')
    tag: str                 # short lowercase category, from the meeting's folder when set
    detail: str
    source_note_id: str
    source_note_title: str
    source_date: str


class IngestResult(BaseModel):
    items: list[IngestItem]


# ---------- card CRUD ----------

def create_card(conn, *, title: str, prompt: str = "", body: str = "", lane: str = "inbox",
                origin: str = "manual", tag: str = "", sources: list[dict] | None = None) -> str:
    card_id = _id("c")
    pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM cards WHERE lane = %s",
                       (lane,)).fetchone()["p"]
    now = _now()
    conn.execute(
        """INSERT INTO cards (card_id, title, lane, position, prompt, body, tag, sources,
                              origin, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (card_id, title, lane, pos, prompt, body, tag.strip().lower(),
         json.dumps(sources or []), origin, now, now),
    )
    return card_id


def get_card(conn, card_id: str) -> dict | None:
    return conn.execute("SELECT * FROM cards WHERE card_id = %s", (card_id,)).fetchone()


def active_cards(conn) -> list[dict]:
    return conn.execute(
        "SELECT * FROM cards WHERE archived = 0 ORDER BY lane, position, updated_at DESC"
    ).fetchall()


def update_card(conn, card_id: str, *, title: str | None = None, prompt: str | None = None,
                body: str | None = None, tag: str | None = None,
                blocked_by: list[str] | None = None, mark_dirty: bool = True) -> None:
    card = get_card(conn, card_id)
    if not card:
        raise KeyError(f"unknown card: {card_id}")
    conn.execute(
        """UPDATE cards SET title = %s, prompt = %s, body = %s, tag = %s, blocked_by = %s,
           dirty = %s, updated_at = %s WHERE card_id = %s""",
        (title if title is not None else card["title"],
         prompt if prompt is not None else card["prompt"],
         body if body is not None else card["body"],
         tag.strip().lower() if tag is not None else card["tag"],
         json.dumps(blocked_by) if blocked_by is not None else card["blocked_by"],
         1 if mark_dirty else card["dirty"], _now(), card_id),
    )


def move_card(conn, card_id: str, lane: str) -> dict:
    """Move a card between lanes. Entering in_progress marks it dirty (eligible to
    fire); entering done notifies and unblocks its dependents."""
    if lane not in LANES:
        raise ValueError(f"unknown lane: {lane}")
    card = get_card(conn, card_id)
    if not card:
        raise KeyError(f"unknown card: {card_id}")
    pos = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM cards WHERE lane = %s",
                       (lane,)).fetchone()["p"]
    dirty = 1 if lane == "in_progress" else card["dirty"]
    conn.execute("UPDATE cards SET lane = %s, position = %s, dirty = %s, updated_at = %s WHERE card_id = %s",
                 (lane, pos, dirty, _now(), card_id))
    if lane == "done":
        _notify_dependents(conn, card)
    return get_card(conn, card_id)


def _notify_dependents(conn, done_card: dict) -> None:
    for c in active_cards(conn):
        if done_card["card_id"] in json.loads(c["blocked_by"] or "[]"):
            excerpt = (done_card["body"] or "").strip()[:2000]
            send_message(conn, from_card=done_card["card_id"], to_card=c["card_id"],
                         body=f"Blocker '{done_card['title']}' is done. Its final state:\n\n{excerpt}")


def send_message(conn, *, from_card: str | None, to_card: str | None, body: str) -> None:
    conn.execute(
        "INSERT INTO card_messages (msg_id, from_card, to_card, body, created_at) VALUES (%s, %s, %s, %s, %s)",
        (_id("m"), from_card, to_card, body, _now()),
    )
    if to_card:
        conn.execute("UPDATE cards SET dirty = 1, updated_at = %s WHERE card_id = %s", (_now(), to_card))


def unread_messages(conn, to_card: str | None) -> list[dict]:
    if to_card is None:
        return conn.execute(
            "SELECT * FROM card_messages WHERE to_card IS NULL AND read_at IS NULL ORDER BY created_at"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM card_messages WHERE to_card = %s AND read_at IS NULL ORDER BY created_at",
        (to_card,)).fetchall()


def card_thread(conn, card_id: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM card_messages WHERE from_card = %s OR to_card = %s ORDER BY created_at",
        (card_id, card_id)).fetchall()


def mark_read(conn, msg_id: str) -> None:
    conn.execute("UPDATE card_messages SET read_at = %s WHERE msg_id = %s", (_now(), msg_id))


def card_runs(conn, card_id: str, limit: int = 20) -> list[dict]:
    return conn.execute(
        "SELECT * FROM card_runs WHERE card_id = %s ORDER BY started_at DESC LIMIT %s",
        (card_id, limit)).fetchall()


def last_runs(conn) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT DISTINCT ON (card_id) * FROM card_runs ORDER BY card_id, started_at DESC"
    ).fetchall()
    return {r["card_id"]: r for r in rows}


def blockers_pending(conn, card: dict) -> list[dict]:
    pending = []
    for bid in json.loads(card["blocked_by"] or "[]"):
        b = get_card(conn, bid)
        if b and not b["archived"] and b["lane"] != "done":
            pending.append(b)
    return pending


def _creates_cycle(conn, card_id: str, new_blocker: str) -> bool:
    graph = {c["card_id"]: json.loads(c["blocked_by"] or "[]") for c in active_cards(conn)}
    graph.setdefault(card_id, []).append(new_blocker)
    seen: set[str] = set()

    def walk(node: str, path: set[str]) -> bool:
        if node in path:
            return True
        if node in seen:
            return False
        seen.add(node)
        return any(walk(b, path | {node}) for b in graph.get(node, []))

    return walk(card_id, set())


# ---------- ingest: Granola notes -> new/merged cards ----------

_INGEST_SYSTEM = (
    "You maintain Kaya Jones's autonomous kanban board. You receive recent Granola meeting "
    "summaries plus a digest of the cards already on the board. Extract ONLY items that are "
    "Kaya's to personally act on: things she explicitly committed to do, was directly asked "
    "to do, or decisions only she can make.\n\n"
    "Do NOT create items for: other attendees' action items or follow-ups (even from Kaya's "
    "meetings), general team/company plans nobody assigned to her, FYIs and context, ideas "
    "merely discussed with no commitment, or book/article recommendations unless she said "
    "she would read it. Meeting summaries mix everyone's tasks together — when ownership is "
    "unclear, use action='skip'. A short board of real commitments beats a complete one.\n\n"
    "For each item decide:\n"
    "- action='update' with card_id when it clearly continues a topic an existing card covers "
    "— the board should evolve, not pile up duplicates. `detail` is the new development only.\n"
    "- action='new' when no card covers it. lane='todo' for clear-cut tasks, lane='inbox' for "
    "things Kaya still needs to decide on. `detail` is 2-6 sentences of context so an agent "
    "could act without re-reading the meeting.\n"
    "- action='skip' for anything already captured or resolved (still include it with a short detail).\n\n"
    "Set tag to a short lowercase category for every item: use the meeting's folder name when "
    "one is given (e.g. 'betaworks', 'bestmate', 'deepmind hackathon' → 'hackathon'), otherwise "
    "infer one word from context ('camp', 'andus', 'personal', …). Reuse the tags already on "
    "the board rather than inventing near-duplicates.\n\n"
    "Always set source_note_id, source_note_title, and source_date (ISO date) from the meeting "
    "the item came from."
)


def ingest(*, cfg: Config | None = None, max_notes: int = 20) -> dict:
    with db.connect() as conn:
        watermark = db.get_state(conn, WATERMARK_KEY)
        digest = [
            {"card_id": c["card_id"], "title": c["title"], "lane": c["lane"], "tag": c["tag"],
             "summary": (c["body"] or "").strip().splitlines()[0][:200] if (c["body"] or "").strip() else ""}
            for c in active_cards(conn)
        ]
    notes = granola.fetch_notes(created_after=watermark, max_notes=max_notes)
    started = _now()
    if not notes:
        summary = {"at": started, "new": 0, "updated": 0, "skipped": 0, "notes": 0}
        with db.connect() as conn:
            db.set_state(conn, LAST_INGEST_KEY, json.dumps(summary))
        return summary

    client = anthropic.Anthropic(api_key=anthropic_api_key())
    payload = json.dumps({"meeting_notes": notes, "board_cards": digest}, indent=2)
    run_id = _id("r")
    try:
        response = client.messages.parse(
            model=MODEL, max_tokens=16000, system=_INGEST_SYSTEM,
            messages=[{"role": "user", "content": payload}],
            output_format=IngestResult,
        )
        result = response.parsed_output
    except Exception as e:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO card_runs (run_id, card_id, trigger, prompt_sent, raw_output,
                   status, note, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (run_id, "__ingest__", "ingest", payload, "", "failed", str(e)[:500], started, _now()),
            )
        raise

    counts = {"new": 0, "updated": 0, "skipped": 0}
    urls = {n["id"]: n.get("url", "") for n in notes}
    with db.connect() as conn:
        for item in result.items:
            src = {"id": item.source_note_id, "title": item.source_note_title,
                   "date": item.source_date, "url": urls.get(item.source_note_id, "")}
            header = f"**{item.source_date} · {item.source_note_title}**"
            if item.action == "update" and item.card_id:
                card = get_card(conn, item.card_id)
                if not card:
                    continue
                body = (card["body"].rstrip() + f"\n\n---\n{header}\n{item.detail}").strip()
                sources = json.loads(card["sources"] or "[]")
                if src["id"] not in [s.get("id") for s in sources]:
                    sources.append(src)
                tag = card["tag"] or item.tag.strip().lower()
                conn.execute(
                    "UPDATE cards SET body = %s, sources = %s, tag = %s, dirty = 1, updated_at = %s WHERE card_id = %s",
                    (body, json.dumps(sources), tag, _now(), item.card_id))
                counts["updated"] += 1
            elif item.action == "new":
                lane = item.lane if item.lane in ("inbox", "todo") else "inbox"
                create_card(conn, title=item.title, body=f"{header}\n{item.detail}",
                            lane=lane, origin="granola", tag=item.tag, sources=[src])
                counts["new"] += 1
            else:
                counts["skipped"] += 1
        new_watermark = max(n["created_at"] for n in notes if n["created_at"])
        if new_watermark:
            db.set_state(conn, WATERMARK_KEY, new_watermark)
        summary = {"at": started, "notes": len(notes), **counts}
        db.set_state(conn, LAST_INGEST_KEY, json.dumps(summary))
        conn.execute(
            """INSERT INTO card_runs (run_id, card_id, trigger, prompt_sent, raw_output,
               status, note, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (run_id, "__ingest__", "ingest", payload,
             result.model_dump_json(), "ok",
             f"{counts['new']} new, {counts['updated']} merged, {counts['skipped']} skipped from {len(notes)} notes",
             started, _now()),
        )
    return summary


# ---------- runner: one card, one structured Claude call ----------

_RUN_SYSTEM = (
    "You are the agent for one card on Kaya's autonomous kanban board (part of forever22). "
    "The card's prompt is your standing instruction; the body is the card's living state. "
    "Do the work the prompt asks for, using the context provided.\n\n"
    "Return:\n"
    "- body: the full replacement card body in markdown. Preserve the dated history the card "
    "has accumulated; append a new dated section for this run rather than erasing the past.\n"
    "- lane_move: 'review' when your output needs Kaya's eyes, 'todo' to send it back to the "
    "queue, or null to stay put. You can never move a card to done — only Kaya does that.\n"
    "- messages: to other cards (use their card_id from the board roster) when they need to "
    "know something or you need something from them; to_card=null sends a note to Kaya.\n"
    "- bestmate: durable knowledge worth ingesting into Kaya's knowledge twin (outcomes, "
    "decisions) — not routine status. Usually empty.\n"
    "- add_blocked_by: card_ids this card genuinely cannot proceed without. Usually empty.\n"
    "- note: one line describing what this run did.\n\n"
    "Be concrete and decisive. Draft the thing rather than describing how you would."
)


def compose_prompt(conn, card: dict) -> str:
    roster = [{"card_id": c["card_id"], "title": c["title"], "lane": c["lane"]}
              for c in active_cards(conn) if c["card_id"] != card["card_id"]]
    unread = [{"from": m["from_card"] or "kaya", "body": m["body"]}
              for m in unread_messages(conn, card["card_id"])]
    sources = json.loads(card["sources"] or "[]")
    parts = [
        f"# Card: {card['title']} ({card['card_id']})",
        f"Lane: {card['lane']} · origin: {card['origin']}",
        f"\n## Standing prompt\n{card['prompt'] or '(none — act on the body and messages)'}",
        f"\n## Current body\n{card['body'] or '(empty)'}",
    ]
    if sources:
        parts.append("\n## Sources\n" + "\n".join(f"- {s.get('date', '')} {s.get('title', '')} ({s.get('id', '')})" for s in sources))
    if unread:
        parts.append("\n## Unread messages\n" + json.dumps(unread, indent=2))
    parts.append("\n## Board roster\n" + json.dumps(roster, indent=2))
    parts.append(f"\nToday is {datetime.now().astimezone().strftime('%A %Y-%m-%d')}.")
    return "\n".join(parts)


def run_card(card_id: str, *, trigger: str = "manual", cfg: Config | None = None) -> dict:
    cfg = cfg or load()
    started = _now()
    with db.connect() as conn:
        card = get_card(conn, card_id)
        if not card:
            raise KeyError(f"unknown card: {card_id}")
        pending = blockers_pending(conn, card)
        if pending:
            return {"status": "blocked", "note": "blocked by: " + ", ".join(b["title"] for b in pending)}
        prompt = compose_prompt(conn, card)

    client = anthropic.Anthropic(api_key=anthropic_api_key())
    run_id = _id("r")
    try:
        response = client.messages.parse(
            model=MODEL, max_tokens=8000, system=_RUN_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_format=RunResult,
        )
        result = response.parsed_output
        raw = response.parsed_output.model_dump_json()
        status, note = "ok", result.note
    except Exception as e:
        result, raw = None, ""
        status, note = "failed", str(e)[:500]

    with db.connect() as conn:
        if result:
            _apply(conn, card_id, result, cfg)
        conn.execute(
            """INSERT INTO card_runs (run_id, card_id, trigger, prompt_sent, raw_output,
               status, note, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (run_id, card_id, trigger, prompt, raw, status, note, started, _now()),
        )
    return {"status": status, "note": note}


def _apply(conn, card_id: str, result: RunResult, cfg: Config) -> None:
    card = get_card(conn, card_id)
    blocked = json.loads(card["blocked_by"] or "[]")
    for bid in result.add_blocked_by:
        if bid != card_id and bid not in blocked and get_card(conn, bid) and not _creates_cycle(conn, card_id, bid):
            blocked.append(bid)
    conn.execute(
        "UPDATE cards SET body = %s, blocked_by = %s, dirty = 0, updated_at = %s WHERE card_id = %s",
        (result.body, json.dumps(blocked), _now(), card_id))
    if result.lane_move in AGENT_LANE_MOVES and result.lane_move != card["lane"]:
        move_card(conn, card_id, result.lane_move)
        conn.execute("UPDATE cards SET dirty = 0 WHERE card_id = %s", (card_id,))
    for m in unread_messages(conn, card_id):
        mark_read(conn, m["msg_id"])
    for msg in result.messages:
        to = msg.to_card if msg.to_card and get_card(conn, msg.to_card) else None
        send_message(conn, from_card=card_id, to_card=to, body=msg.body)
    for item in result.bestmate:
        bestmate.ingest(item.content, title=item.title,
                        tags=item.tags or ["forever22", "board"],
                        visibility=cfg.bestmate.visibility, source="forever22-board")


_RESEARCH_SYSTEM = (
    "You are a research agent for one card on Kaya Jones's kanban board. Use web search to "
    "investigate the open questions the card raises. Gather, don't act: no drafting, no "
    "decisions — return findings. Be concrete: names, dates, numbers, links. End with a "
    "'Sources:' list of the URLs that mattered, as markdown links. If the card gives you "
    "nothing researchable, say so in one paragraph instead of padding."
)

RESEARCH_MAX_SEARCHES = 8


def research_card(card_id: str, *, trigger: str = "research") -> dict:
    started = _now()
    with db.connect() as conn:
        card = get_card(conn, card_id)
        if not card:
            raise KeyError(f"unknown card: {card_id}")
        prompt = compose_prompt(conn, card)

    client = anthropic.Anthropic(api_key=anthropic_api_key())
    run_id = _id("r")
    queries: list[str] = []
    text_parts: list[str] = []
    sources: list[dict] = []
    try:
        response = client.messages.create(
            model=MODEL, max_tokens=16000, system=_RESEARCH_SYSTEM,
            tools=[{"type": "web_search_20260209", "name": "web_search",
                    "max_uses": RESEARCH_MAX_SEARCHES}],
            messages=[{"role": "user", "content": prompt}],
        )
        for block in response.content:
            if block.type == "server_tool_use" and block.name == "web_search":
                if isinstance(block.input, dict):
                    queries.append(block.input.get("query", ""))
            elif block.type == "text":
                text_parts.append(block.text)
                for c in getattr(block, "citations", None) or []:
                    url = getattr(c, "url", "")
                    if url and url not in [s["url"] for s in sources]:
                        sources.append({"title": getattr(c, "title", "") or url, "url": url})
        findings = "".join(text_parts).strip()
        status, note = "ok", f"{len(queries)} searches, {len(sources)} sources"
    except Exception as e:
        findings, status, note = "", "failed", str(e)[:500]

    raw = json.dumps({"queries": queries, "findings": findings, "sources": sources})
    with db.connect() as conn:
        if status == "ok" and findings:
            card = get_card(conn, card_id)
            date = datetime.now().astimezone().strftime("%Y-%m-%d")
            section = f"\n\n---\n**{date} · Research**\n{findings}"
            if sources:
                section += "\n\nSources:\n" + "\n".join(f"- [{s['title']}]({s['url']})" for s in sources)
            conn.execute(
                "UPDATE cards SET body = %s, dirty = 1, updated_at = %s WHERE card_id = %s",
                ((card["body"].rstrip() + section).strip(), _now(), card_id))
        conn.execute(
            """INSERT INTO card_runs (run_id, card_id, trigger, prompt_sent, raw_output,
               status, note, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (run_id, card_id, trigger, prompt, raw, status, note, started, _now()),
        )
    return {"status": status, "note": note}


def runnable(conn) -> list[dict]:
    return [c for c in active_cards(conn)
            if c["lane"] == "in_progress" and c["dirty"] and not blockers_pending(conn, c)]


def tick(*, cfg: Config | None = None, skip_ingest: bool = False) -> dict:
    """One scheduled pass: ingest, then fire dirty in-progress cards. Runs triggered by
    agent messages get one cascade hop; MAX_RUNS_PER_TICK caps the whole pass."""
    cfg = cfg or load()
    report: dict = {"ingest": None, "runs": []}
    if not skip_ingest:
        try:
            report["ingest"] = ingest(cfg=cfg)
        except Exception as e:
            report["ingest"] = {"error": str(e)[:300]}
    ran: set[str] = set()
    for hop in range(2):
        with db.connect() as conn:
            due = [c["card_id"] for c in runnable(conn) if c["card_id"] not in ran]
        for card_id in due:
            if len(ran) >= MAX_RUNS_PER_TICK:
                break
            ran.add(card_id)
            out = run_card(card_id, trigger="schedule", cfg=cfg)
            report["runs"].append({"card_id": card_id, **out})
    return report
