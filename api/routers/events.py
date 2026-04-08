"""
Event injection endpoint — synthetic pipeline testing without Google OAuth.
  POST /api/events/inject  → push a fake email/calendar/filesystem event
"""
import hashlib
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.ws import manager

router = APIRouter()


@router.post("/api/events/inject")
async def inject_event(body: dict) -> dict:
    """
    Inject a synthetic event into events:queue for the Planner to process.
    Useful for live demos without Google OAuth credentials.
    """
    from memory.redis_client import RedisClient
    redis = RedisClient.get_instance()

    event_type = body.get("event_type", "email")
    urgent = body.get("urgent", False)

    urgency_keywords: list[str] = []
    if urgent:
        urgency_keywords = ["urgent", "deadline", "immediately"]

    if event_type == "email":
        from_addr = body.get("from", "demo@example.com")
        subject   = body.get("subject", "Test Email")
        snippet   = body.get("snippet", "")
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

        event: dict[str, Any] = {
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
        summary  = body.get("summary", "Test Meeting")
        start    = body.get("start", datetime.now(timezone.utc).isoformat())
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

    elif event_type == "filesystem":
        count    = body.get("file_count", 25)
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

    else:
        return JSONResponse({"error": f"Unknown event_type: {event_type}"}, status_code=400)

    await redis.push_event(event)
    await redis.append_activity_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "Dashboard",
        "action": f"INJECTED: {event_type} event — {event['summary'][:60]}",
    })

    await manager.broadcast({"type": "new_plan", "message": f"New {event_type} event injected"})
    return {"status": "injected", "event_id": event_id, "type": event_type}
