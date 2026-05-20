from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .config import REPO_ROOT

ENGAGEMENTS_DIR = REPO_ROOT / "engagements"
CLIENTS_DIR = ENGAGEMENTS_DIR / "clients"
COMMUNITY_DIR = ENGAGEMENTS_DIR / "community"

VALID_STATUSES = ("active", "paused", "completed")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass
class TimeLogEntry:
    date: str
    hours: float
    note: str = ""


@dataclass
class Contact:
    name: str
    email: str = ""
    role: str = ""


@dataclass
class Engagement:
    title: str
    slug: str
    type: str
    status: str = "active"
    client: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    hours_committed_per_week: float | None = None
    contacts: list[Contact] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    time_log: list[TimeLogEntry] = field(default_factory=list)
    notes: str = ""

    @property
    def path(self) -> Path:
        if self.type == "client":
            if not self.client:
                raise ValueError("client engagements need a client label")
            return CLIENTS_DIR / self.client / f"{self.slug}.md"
        if self.type == "community":
            return COMMUNITY_DIR / f"{self.slug}.md"
        raise ValueError(f"unknown engagement type: {self.type}")

    @property
    def total_hours(self) -> float:
        return sum(e.hours for e in self.time_log)


def slugify(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s or "untitled"


def _frontmatter_payload(eng: Engagement) -> dict[str, Any]:
    data = asdict(eng)
    data.pop("notes", None)
    for k in ("client",) if eng.type != "client" else ():
        data.pop(k, None)
    if not data.get("contacts"):
        data["contacts"] = []
    if not data.get("time_log"):
        data["time_log"] = []
    return data


def _parse_file(path: Path) -> Engagement:
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError(f"no frontmatter in {path}")
    front = yaml.safe_load(m.group(1)) or {}
    notes = m.group(2).strip()
    contacts = [Contact(**c) for c in (front.get("contacts") or [])]
    time_log = [TimeLogEntry(**t) for t in (front.get("time_log") or [])]
    return Engagement(
        title=front["title"],
        slug=front["slug"],
        type=front["type"],
        status=front.get("status", "active"),
        client=front.get("client"),
        start_date=front.get("start_date"),
        end_date=front.get("end_date"),
        hours_committed_per_week=front.get("hours_committed_per_week"),
        contacts=contacts,
        links=list(front.get("links") or []),
        tags=list(front.get("tags") or []),
        time_log=time_log,
        notes=notes,
    )


def save(eng: Engagement) -> Path:
    path = eng.path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _frontmatter_payload(eng)
    front_yaml = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).strip()
    body = eng.notes.strip() + "\n" if eng.notes.strip() else ""
    path.write_text(f"---\n{front_yaml}\n---\n\n{body}")
    return path


def load(slug: str) -> Engagement:
    for path in _all_paths():
        if path.stem == slug:
            return _parse_file(path)
    raise FileNotFoundError(f"engagement '{slug}' not found")


def _all_paths() -> list[Path]:
    paths: list[Path] = []
    if CLIENTS_DIR.exists():
        paths.extend(p for p in CLIENTS_DIR.rglob("*.md"))
    if COMMUNITY_DIR.exists():
        paths.extend(p for p in COMMUNITY_DIR.glob("*.md"))
    return sorted(paths)


def list_engagements(*, client: str | None = None, status: str | None = None,
                     type: str | None = None) -> list[Engagement]:
    out: list[Engagement] = []
    for path in _all_paths():
        try:
            eng = _parse_file(path)
        except Exception:
            continue
        if client and eng.client != client:
            continue
        if status and eng.status != status:
            continue
        if type and eng.type != type:
            continue
        out.append(eng)
    out.sort(key=lambda e: (e.status != "active", e.title.lower()))
    return out


def log_time(slug: str, *, hours: float, note: str = "", when: str | None = None) -> Engagement:
    eng = load(slug)
    eng.time_log.append(TimeLogEntry(date=when or date.today().isoformat(), hours=hours, note=note))
    save(eng)
    return eng


def create(*, title: str, type: str, client: str | None = None, status: str = "active",
           hours_per_week: float | None = None, start_date: str | None = None) -> Engagement:
    if type not in ("client", "community"):
        raise ValueError("type must be 'client' or 'community'")
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    if type == "client" and not client:
        raise ValueError("client engagements require --client")
    eng = Engagement(
        title=title,
        slug=slugify(title),
        type=type,
        status=status,
        client=client,
        start_date=start_date or date.today().isoformat(),
        hours_committed_per_week=hours_per_week,
    )
    if eng.path.exists():
        raise FileExistsError(f"engagement already exists at {eng.path}")
    save(eng)
    return eng
