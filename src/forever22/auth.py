from __future__ import annotations

import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import ACCOUNTS_DIR, Config, load


def authorize(label: str, *, cfg: Config | None = None) -> Path:
    cfg = cfg or load()
    account = cfg.account(label)
    secrets = cfg.google_oauth.client_secrets_file
    if not secrets.exists():
        raise FileNotFoundError(
            f"OAuth client secrets not found at {secrets}. "
            "Download Desktop OAuth credentials from Google Cloud Console and save as credentials.json."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), cfg.google_oauth.scopes)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        authorization_prompt_message=(
            f"\nAuthorizing label '{label}' (expected email: {account.email}).\n"
            "Sign into the matching Google account in the browser window that just opened.\n"
        ),
    )
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    token_path = cfg.token_path(label)
    token_path.write_text(creds.to_json())
    return token_path


def load_credentials(label: str, *, cfg: Config | None = None) -> Credentials:
    cfg = cfg or load()
    token_path = cfg.token_path(label)
    if not token_path.exists():
        raise FileNotFoundError(
            f"No token for '{label}'. Run `f22 auth --label {label}` first."
        )
    creds = Credentials.from_authorized_user_info(json.loads(token_path.read_text()), cfg.google_oauth.scopes)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        except RefreshError as e:
            raise RuntimeError(
                f"Refresh failed for '{label}': {e}. Re-run `f22 auth --label {label}`."
            ) from e
    return creds
