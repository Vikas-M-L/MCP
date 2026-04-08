"""
Gmail MCP tools — read_emails and send_email.
Uses Google OAuth 2.0. On first run, opens browser to authorize.
token.json is saved and auto-refreshed on subsequent runs.
"""
import base64
from email.mime.text import MIMEText
from typing import Any

from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

from mcp_server.google_auth import get_credentials

_gmail_service = None  # module-level cache


def _get_gmail_service():
    """Build and return cached Gmail API resource using shared OAuth credentials."""
    global _gmail_service
    if _gmail_service is not None:
        return _gmail_service
    _gmail_service = build("gmail", "v1", credentials=get_credentials())
    return _gmail_service


def register_gmail_tools(mcp: FastMCP) -> None:
    """Register read_emails and send_email tools onto the FastMCP instance."""

    @mcp.tool()
    def read_emails(max_results: int = 10, query: str = "") -> list[dict[str, Any]]:
        """
        Fetch recent emails from Gmail inbox.
        Returns list of {id, from, subject, snippet, date, unread}.
        query: optional Gmail search string (e.g. 'is:unread', 'from:boss@company.com')
        """
        service = _get_gmail_service()
        q = query if query else "in:inbox"
        result = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q=q)
            .execute()
        )
        messages = result.get("messages", [])
        emails = []
        for msg in messages:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=msg["id"], format="metadata")
                .execute()
            )
            headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            labels = full.get("labelIds", [])
            emails.append(
                {
                    "id": full["id"],
                    "from": headers.get("From", ""),
                    "subject": headers.get("Subject", "(no subject)"),
                    "snippet": full.get("snippet", ""),
                    "date": headers.get("Date", ""),
                    "unread": "UNREAD" in labels,
                }
            )
        return emails

    @mcp.tool()
    def send_email(to: str, subject: str, body: str) -> dict[str, Any]:
        """
        Send an email via Gmail API.
        Returns {message_id, status}.
        """
        service = _get_gmail_service()
        mime = MIMEText(body)
        mime["to"] = to
        mime["subject"] = subject
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        sent = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw})
            .execute()
        )
        return {"message_id": sent["id"], "status": "sent"}
