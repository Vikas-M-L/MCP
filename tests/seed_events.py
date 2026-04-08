"""
Demo seed script — injects 3 realistic synthetic events into Redis events:queue.
Run this BEFORE main.py (with --skip-poll) for a clean hackathon demo
that doesn't require real Google OAuth credentials.

Events seeded:
  1. Email from professor (urgent + deadline) → expect confidence > 90% → AUTO-EXECUTE
  2. Unread email from colleague 2h ago       → expect confidence 70-90% → DASHBOARD
  3. Newsletter email                         → expect confidence < 70%  → SILENT

Usage:
  python tests/seed_events.py
  python main.py --skip-poll
"""
import asyncio
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis


SAMPLE_EVENTS = [
    # ── Event 1: HIGH CONFIDENCE (>90%) — auto-execute + phone call ────────────
    {
        "event_id": "demo-001",
        "type": "email",
        "source": "gmail",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "id": "gmail-msg-001",
            "from": "professor.sharma@rnsinstitute.edu",
            "subject": "Assignment deadline - URGENT - Submit by 6PM today",
            "snippet": "Dear student, this is an urgent reminder that your final project is due today. Please submit immediately or contact me.",
            "date": datetime.now(timezone.utc).isoformat(),
            "unread": True,
        },
        "urgency_keywords": ["urgent", "deadline", "immediately"],
        "summary": "Email from professor.sharma@rnsinstitute.edu: Assignment deadline - URGENT - Submit by 6PM today",
    },

    # ── Event 2: MEDIUM CONFIDENCE (70-90%) — dashboard approval ──────────────
    {
        "event_id": "demo-002",
        "type": "email",
        "source": "gmail",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "id": "gmail-msg-002",
            "from": "teammate.rahul@company.com",
            "subject": "Can we reschedule our 3PM sync meeting?",
            "snippet": "Hey, something came up. Can we move our 3PM standup to 4PM? Let me know if that works.",
            "date": datetime.now(timezone.utc).isoformat(),
            "unread": True,
        },
        "urgency_keywords": [],
        "summary": "Email from teammate.rahul@company.com: Can we reschedule our 3PM sync meeting?",
    },

    # ── Event 3: LOW CONFIDENCE (<70%) — silent discard ──────────────────────
    {
        "event_id": "demo-003",
        "type": "email",
        "source": "gmail",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "id": "gmail-msg-003",
            "from": "newsletter@techdigest.io",
            "subject": "This week in AI: 10 things you need to know",
            "snippet": "Welcome to this week's roundup. Here are the top AI stories from around the web...",
            "date": datetime.now(timezone.utc).isoformat(),
            "unread": True,
        },
        "urgency_keywords": [],
        "summary": "Email from newsletter@techdigest.io: This week in AI: 10 things you need to know",
    },

    # ── Bonus Event: Calendar conflict ─────────────────────────────────────────
    {
        "event_id": "demo-004",
        "type": "calendar",
        "source": "google_calendar",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "id": "cal-event-001",
            "summary": "Interview with Google — Senior Engineer Role",
            "start": "2026-04-08T15:00:00+05:30",
            "end": "2026-04-08T16:00:00+05:30",
            "attendees": ["recruiter@google.com", "you@company.com"],
            "location": "Google Meet",
            "description": "Technical interview round 2. Please be prepared with system design questions.",
        },
        "urgency_keywords": [],
        "summary": "Calendar: Interview with Google — Senior Engineer Role at 2026-04-08T15:00:00+05:30",
    },
]


async def seed(redis_url: str = "redis://localhost:6379/0", n: int | None = None) -> None:
    """Push sample events into Redis events:queue."""
    r = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)

    events_to_seed = SAMPLE_EVENTS[:n] if n else SAMPLE_EVENTS

    print(f"\n[Seed] Connecting to Redis at {redis_url}...")
    try:
        await r.ping()
    except Exception as e:
        print(f"[Seed] ERROR: Cannot connect to Redis — {e}")
        print("[Seed] Start Redis with: redis-server  or  docker run -p 6379:6379 redis")
        return

    # Clear old demo events to avoid duplicates
    for event in events_to_seed:
        await r.srem("seen:event_ids", event["event_id"])

    # Push events
    for i, event in enumerate(events_to_seed, 1):
        await r.rpush("events:queue", json.dumps(event))
        confidence_label = {
            "demo-001": ">90% (AUTO-EXECUTE)",
            "demo-002": "70-90% (DASHBOARD)",
            "demo-003": "<70% (SILENT)",
            "demo-004": "~75% (DASHBOARD)",
        }.get(event["event_id"], "?")
        print(f"  [{i}] Seeded: {event['summary'][:70]}")
        print(f"       Type: {event['type']} | Expected: {confidence_label}")

    queue_len = await r.llen("events:queue")
    await r.aclose()

    print(f"\n[Seed] Done! {len(events_to_seed)} events in events:queue (total queue length: {queue_len})")
    print("[Seed] Now run: python main.py --skip-poll")
    print("[Seed] Dashboard: http://localhost:8080")


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    asyncio.run(seed(n=n))
