from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import anthropic
from pydantic import BaseModel

from . import bestmate, db
from .config import Config, anthropic_api_key, load

MODEL = "claude-opus-4-7"


class Issue(BaseModel):
    severity: str            # info | warning | alert
    area: str                # sync | mirror | aggregate | autolog | data
    description: str
    suggested_action: str


class HealthReport(BaseModel):
    status: str              # healthy | degraded | broken
    summary: str
    issues: list[Issue]


def _gh_runs(workflow: str, limit: int) -> list[dict]:
    try:
        out = subprocess.run(
            ["gh", "run", "list", "--workflow", workflow, "--limit", str(limit),
             "--json", "conclusion,status,createdAt,event,displayTitle"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode != 0:
            return []
        return json.loads(out.stdout or "[]")
    except Exception:
        return []


def gather_signals(cfg: Config) -> dict:
    """Collect everything the monitoring agent reasons over."""
    now = datetime.now(timezone.utc)
    signals: dict = {"generated_at": now.isoformat()}
    signals["sync_runs"] = _gh_runs("sync.yml", 12)
    signals["autolog_runs"] = _gh_runs("autolog.yml", 6)

    with db.connect() as conn:
        signals["accounts"] = [dict(r) for r in db.sync_status(conn)]
        signals["mirror_count"] = conn.execute("SELECT count(*) AS c FROM mirrors").fetchone()["c"]
        signals["aggregated_count"] = conn.execute("SELECT count(*) AS c FROM aggregated").fetchone()["c"]
        signals["event_count"] = conn.execute("SELECT count(*) AS c FROM events").fetchone()["c"]
        cutoff = (now - timedelta(days=8)).isoformat()
        rows = conn.execute(
            """SELECT engagement_slug, entry_date, hours, note
            FROM time_log
            WHERE source_event_id <> '' AND created_at > %s
            ORDER BY created_at DESC LIMIT 40""",
            (cutoff,),
        ).fetchall()
        signals["recent_autolog_entries"] = [dict(r) for r in rows]
    return signals


_SYSTEM = (
    "You are the monitoring agent for forever22, a personal calendar-automation system "
    "owned by Kaya. It runs two cloud crons: `sync` (every 15 min — pulls Google Calendar "
    "events across 4 accounts, mirrors busy times between them, and updates a unified "
    "calendar) and `autolog` (daily — logs completed client meetings as engagement hours).\n\n"
    "You are given the current system state. Assess its health and flag anything wrong or "
    "worth Kaya's attention: failed or missing workflow runs, stale syncs (last run long "
    "ago), suspicious auto-logged hours (e.g. a meeting logged at 14 hours), or unexpected "
    "drops in event/mirror/aggregate counts. Be concise and specific; tie each issue to a "
    "concrete next action. If everything looks fine, return status=healthy with an empty "
    "issues list. status is one of: healthy, degraded, broken. severity is one of: info, "
    "warning, alert. area is one of: sync, mirror, aggregate, autolog, data."
)


def assess(signals: dict) -> HealthReport:
    client = anthropic.Anthropic(api_key=anthropic_api_key())
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        system=_SYSTEM,
        messages=[{
            "role": "user",
            "content": "Current forever22 system state:\n\n"
                       + json.dumps(signals, indent=2, default=str),
        }],
        output_format=HealthReport,
    )
    return response.parsed_output


def _digest(report: HealthReport, generated_at: str) -> str:
    lines = [f"forever22 health check ({generated_at}): {report.status}", "", report.summary]
    if report.issues:
        lines.append("")
        for i in report.issues:
            lines.append(f"- [{i.severity}] {i.area}: {i.description} → {i.suggested_action}")
    return "\n".join(lines)


def run(*, cfg: Config | None = None) -> HealthReport:
    cfg = cfg or load()
    signals = gather_signals(cfg)
    report = assess(signals)
    bestmate.ingest(
        _digest(report, signals["generated_at"]),
        title=f"forever22 health {signals['generated_at'][:10]}",
        tags=["forever22", "monitoring"],
        visibility=cfg.bestmate.visibility,
    )
    return report
