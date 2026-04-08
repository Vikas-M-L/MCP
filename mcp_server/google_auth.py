"""
Shared Google OAuth helper.
Requests ALL required scopes (Gmail + Calendar) in one OAuth flow,
so both gmail_tools and calendar_tools share a single token.json.
"""
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Combined scopes for Gmail + Calendar
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

_credentials: Credentials | None = None


def get_credentials() -> Credentials:
    """
    Return valid Google credentials, running OAuth flow on first call if needed.
    Credentials are cached in memory and persisted to token.json.
    """
    global _credentials
    if _credentials and _credentials.valid:
        return _credentials

    from config.settings import get_settings
    cfg = get_settings()

    creds = None
    if os.path.exists(cfg.google_token_path):
        creds = Credentials.from_authorized_user_file(cfg.google_token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                cfg.google_credentials_path, SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(cfg.google_token_path, "w") as token:
            token.write(creds.to_json())

    _credentials = creds
    return creds
