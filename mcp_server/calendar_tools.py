"""
Google Calendar MCP tools — read_calendar and create_event.
Shares OAuth credentials with Gmail (same Google Cloud project).
token.json covers both Gmail and Calendar scopes.
"""
from datetime import datetime, timezone, timedelta
from typing import Any

from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

from mcp_server.google_auth import get_credentials

_calendar_service = None  # module-level cache


def _get_calendar_service():
    """Build and return cached Calendar API resource using shared OAuth credentials."""
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service
    _calendar_service = build("calendar", "v3", credentials=get_credentials())
    return _calendar_service


def register_calendar_tools(mcp: FastMCP) -> None:
    """Register read_calendar and create_event tools onto the FastMCP instance."""

    @mcp.tool()
    def read_calendar(days_ahead: int = 7) -> list[dict[str, Any]]:
        """
        Fetch upcoming Google Calendar events for the next N days.
        Returns list of {id, summary, start, end, attendees, location, description}.
        """
        service = _get_calendar_service()
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)
        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = []
        for item in result.get("items", []):
            start = item["start"].get("dateTime", item["start"].get("date", ""))
            end_time = item["end"].get("dateTime", item["end"].get("date", ""))
            attendees = [
                a.get("email", "") for a in item.get("attendees", [])
            ]
            events.append(
                {
                    "id": item["id"],
                    "summary": item.get("summary", "(no title)"),
                    "start": start,
                    "end": end_time,
                    "attendees": attendees,
                    "location": item.get("location", ""),
                    "description": item.get("description", ""),
                }
            )
        return events

    @mcp.tool()
    def create_event(
        summary: str,
        start_datetime: str,
        end_datetime: str,
        description: str = "",
        attendees: list[str] | None = None,
        location: str = "",
    ) -> dict[str, Any]:
        """
        Create a Google Calendar event.
        start_datetime / end_datetime: ISO 8601 strings (e.g. '2026-04-09T14:00:00+05:30')
        Returns {event_id, html_link, status}.
        """
        service = _get_calendar_service()
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": start_datetime},
            "end": {"dateTime": end_datetime},
        }
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]

        event = (
            service.events().insert(calendarId="primary", body=body).execute()
        )
        return {
            "event_id": event["id"],
            "html_link": event.get("htmlLink", ""),
            "status": event.get("status", "confirmed"),
        }
