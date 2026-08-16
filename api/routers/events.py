"""
Event injection endpoint — synthetic pipeline testing without Google OAuth.
  POST /api/events/inject  → push a fake email/calendar/filesystem event
"""
import hashlib
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.ws import manager

router = APIRouter()


# ── Request schema ─────────────────────────────────────────────────────────────

class InjectEventRequest(BaseModel):
    """Validated body for POST /api/events/inject."""
    event_type: Literal["email", "calendar", "filesystem"] = Field(
        default="email",
        description="Type of event to inject into the pipeline.",
    )
    urgent: bool = Field(
        default=False,
        description="If true, pre-populates urgency_keywords with urgent/deadline/immediately.",
    )
    # Email fields
    from_addr: str = Field(
        default="demo@example.com",
        alias="from",
        description="Sender address (email events only).",
    )
    subject: str = Field(
        default="Test Email",
        description="Email subject or calendar event title.",
    )
    snippet: str = Field(
        default="",
        description="Email body snippet (email events only).",
    )
    # Calendar fields
    start: str = Field(
        default="",
        description="ISO 8601 start time (calendar events only). Defaults to now.",
    )
    # Filesystem fields
    file_count: int = Field(
        default=25,
        ge=1,
        description="Simulated file count (filesystem events only).",
    )

    model_config = {"populate_by_name": True}


# ── Endpoint ───────────────────────────────────────────────────────────────────

@router.post("/api/events/inject")
async def inject_event(body: InjectEventRequest) -> dict:
    """
    Inject a synthetic event into events:queue for the Planner to process.
    Useful for live demos without Google OAuth credentials.
    """
    from memory.redis_client import RedisClient
    redis = RedisClient.get_instance()

    event_type = body.event_type
    urgent = body.urgent

    urgency_keywords: list[str] = []
    if urgent:
        urgency_keywords = ["urgent", "deadline", "immediately"]

    event: dict[str, Any]

    if event_type == "email":
        from_addr = body.from_addr
        subject   = body.subject
        snippet   = body.snippet
        raw = f"{from_addr} {subject} {snippet}".lower()
        extra_kw = [
            kw for kw in
            ["urgent", "asap", "deadline", "critical", "emergency", "important", "action required"]
            if kw in raw
        ]
        for kw in extra_kw:
            if kw not in urgency_keywords:
                urgency_keywords.append(kw)

        event_id = "inj-" + hashlib.sha256(
            f"email:{from_addr}:{subject}:{datetime.now().isoformat()}".encode()
        ).hexdigest()[:12]

        event = {
            "event_id": event_id,
            "type": "email",
            "source": "gmail",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "id": event_id,
                "from": from_addr,
                "subject": subject,
                "snippet": snippet,
                "date": datetime.now(timezone.utc).isoformat(),
                "unread": True,
            },
            "urgency_keywords": urgency_keywords,
            "summary": f"Email from {from_addr}: {subject}",
        }

    elif event_type == "calendar":
        summary  = body.subject
        start    = body.start or datetime.now(timezone.utc).isoformat()
        event_id = "inj-" + hashlib.sha256(
            f"cal:{summary}:{start}".encode()
        ).hexdigest()[:12]
        event = {
            "event_id": event_id,
            "type": "calendar",
            "source": "google_calendar",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {
                "id": event_id,
                "summary": summary,
                "start": start,
                "end": start,
                "attendees": [],
                "location": "",
                "description": "",
            },
            "urgency_keywords": urgency_keywords,
            "summary": f"Calendar: {summary} at {start}",
        }

    else:  # filesystem — Literal already validates, but keep for completeness
        count    = body.file_count
        event_id = "inj-" + hashlib.sha256(
            f"files:overflow:{count}:{datetime.now().isoformat()}".encode()
        ).hexdigest()[:12]
        event = {
            "event_id": event_id,
            "type": "filesystem",
            "source": "local_fs",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"file_count": count, "files": []},
            "urgency_keywords": [],
            "summary": f"Sandbox folder has {count} unsorted files",
        }

    await redis.push_event(event)
    await redis.append_activity_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "Dashboard",
        "action": f"INJECTED: {event_type} event — {event['summary'][:60]}",
    })

    await manager.broadcast({"type": "new_plan", "message": f"New {event_type} event injected"})
    return {"status": "injected", "event_id": event_id, "type": event_type}
