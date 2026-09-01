from __future__ import annotations

import time

import httpx

from .config import granola_api_key

BASE = "https://public-api.granola.ai/v1"


def fetch_notes(*, created_after: str | None = None, max_notes: int = 20) -> list[dict]:
    """Pull processed notes from the Granola public API, watermark forward. The list
    endpoint is metadata-only, so each note's summary needs a detail fetch (rate limit:
    5 req/s sustained)."""
    headers = {"Authorization": f"Bearer {granola_api_key()}"}
    metas: list[dict] = []
    cursor: str | None = None
    with httpx.Client(timeout=30, headers=headers) as client:
        while len(metas) < max_notes:
            params: dict = {}
            if created_after:
                params["created_after"] = created_after
            if cursor:
                params["cursor"] = cursor
            r = client.get(f"{BASE}/notes", params=params)
            r.raise_for_status()
            data = r.json()
            metas.extend(data.get("notes") or [])
            cursor = data.get("cursor")
            if not data.get("hasMore") or not cursor:
                break
        metas.sort(key=lambda n: n.get("created_at", ""))
        notes = []
        for m in metas[:max_notes]:
            r = client.get(f"{BASE}/notes/{m['id']}")
            if r.status_code == 429:
                time.sleep(2)
                r = client.get(f"{BASE}/notes/{m['id']}")
            r.raise_for_status()
            n = r.json()
            folders = n.get("folder_membership") or []
            notes.append({
                "id": n["id"],
                "title": n.get("title") or "(untitled meeting)",
                "created_at": n.get("created_at", ""),
                "url": n.get("web_url", ""),
                "folder": folders[0].get("name", "") if folders else "",
                "summary": (n.get("summary_markdown") or n.get("summary_text") or "")[:6000],
            })
            time.sleep(0.25)
    return notes
