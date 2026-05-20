from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config.yaml"
ACCOUNTS_DIR = REPO_ROOT / "accounts"
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "events.db"


@dataclass(frozen=True)
class Account:
    label: str
    email: str
    color: str


@dataclass(frozen=True)
class BestmateSettings:
    target: str
    visibility: str


@dataclass(frozen=True)
class SyncSettings:
    past_days: int
    future_days: int
    bestmate_digest: bool


@dataclass(frozen=True)
class GoogleOAuthSettings:
    client_secrets_file: Path
    scopes: list[str]


@dataclass(frozen=True)
class MirrorSettings:
    enabled: bool
    mirror_title: str
    rules: dict[str, list[str]]


@dataclass(frozen=True)
class AggregateSettings:
    enabled: bool
    host_account: str
    calendar_name: str


@dataclass(frozen=True)
class Config:
    accounts: list[Account]
    bestmate: BestmateSettings
    sync: SyncSettings
    google_oauth: GoogleOAuthSettings
    mirror: MirrorSettings
    aggregate: AggregateSettings

    def account(self, label: str) -> Account:
        for a in self.accounts:
            if a.label == label:
                return a
        raise KeyError(f"unknown account label: {label}")

    def token_path(self, label: str) -> Path:
        return ACCOUNTS_DIR / f"{label}.json"


def load() -> Config:
    with CONFIG_PATH.open() as f:
        raw = yaml.safe_load(f)
    accounts = [Account(**a) for a in raw["accounts"]]
    bm = BestmateSettings(**raw["bestmate"])
    sync = SyncSettings(**raw["sync"])
    g = raw["google_oauth"]
    client_secrets = g["client_secrets_file"]
    if not Path(client_secrets).is_absolute():
        client_secrets = REPO_ROOT / client_secrets
    google = GoogleOAuthSettings(client_secrets_file=Path(client_secrets), scopes=list(g["scopes"]))
    m = raw.get("mirror", {"enabled": False, "mirror_title": "Busy", "rules": {}})
    mirror = MirrorSettings(
        enabled=bool(m.get("enabled", False)),
        mirror_title=m.get("mirror_title", "Busy"),
        rules={k: list(v) for k, v in (m.get("rules") or {}).items()},
    )
    a = raw.get("aggregate", {})
    aggregate = AggregateSettings(
        enabled=bool(a.get("enabled", False)),
        host_account=a.get("host_account", "personal"),
        calendar_name=a.get("calendar_name", "forever22 — All"),
    )
    return Config(accounts=accounts, bestmate=bm, sync=sync, google_oauth=google,
                  mirror=mirror, aggregate=aggregate)
