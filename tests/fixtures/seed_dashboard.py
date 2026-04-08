"""
Direct dashboard seed — pushes pre-built plans to emails:all + dashboard:pending.
Use this when the LLM rate limit is hit, for demo purposes.

Run: python tests/seed_dashboard.py
Then: python main.py --skip-poll
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone

import redis.asyncio as aioredis

PLANS = [
    {
        "id": str(uuid.uuid4()),
        "event_id": "demo-high-001",
        "event_type": "email",
        "action": "send_email",
        "confidence": 97,
        "priority": "high",
        "user_response": "auto_executed",
        "subject": "Assignment deadline - URGENT - Submit by 6PM today",
        "from_addr": "professor.sharma@rnsinstitute.edu",
        "snippet": "Dear student, this is an urgent reminder that your final project is due today. Please submit immediately or contact me.",
        "urgency_keywords": ["urgent", "deadline", "immediately"],
        "reason": "Student has an imminent assignment deadline requiring immediate action. Auto-drafted reply to professor acknowledging receipt.",
        "explanation": "send_email chosen over no_action because the deadline is today and non-response could lead to academic penalty.",
        "requires_approval": False,
        "alternatives": [
            {"action": "create_event", "confidence": 40, "reason": "Could create a reminder but deadline is already today."},
            {"action": "no_action", "confidence": 10, "reason": "Ignoring an urgent academic deadline is risky."},
        ],
        "action_args": {
            "to": "professor.sharma@rnsinstitute.edu",
            "subject": "Re: Assignment deadline - URGENT",
            "body": "Dear Professor Sharma,\n\nThank you for the reminder. I have noted the 6PM deadline and will submit my project before then.\n\nBest regards,\nStudent",
        },
        "scoring": {"base": 92, "urgency_mult": 1.3, "history_mult": 0.95},
        "call_text": "Hello! This is your Personal OS Agent. I detected a high priority email from professor.sharma@rnsinstitute.edu. Subject: Assignment deadline URGENT Submit by 6PM today. Confidence level: 97 percent. Recommended action: send email. Reason: Student has an imminent assignment deadline requiring immediate action. Action has been automatically executed.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    },
    {
        "id": str(uuid.uuid4()),
        "event_id": "demo-high-002",
        "event_type": "email",
        "action": "send_email",
        "confidence": 94,
        "priority": "high",
        "user_response": "auto_executed",
        "subject": "Reply ASAP – important project update needed",
        "from_addr": "manager@company.com",
        "snippet": "Hi, I need your status update on the Q2 project ASAP. The client is waiting. Please respond by EOD.",
        "urgency_keywords": ["urgent", "asap", "important"],
        "reason": "Manager requesting urgent project status update with client dependency. Time-critical.",
        "explanation": "Immediate reply needed to unblock client. Auto-drafted professional status update.",
        "requires_approval": False,
        "alternatives": [
            {"action": "no_action", "confidence": 5, "reason": "Ignoring manager request would be inappropriate."},
            {"action": "create_event", "confidence": 15, "reason": "Scheduling a meeting is slower than a reply."},
        ],
        "action_args": {
            "to": "manager@company.com",
            "subject": "Re: Q2 Project Status Update",
            "body": "Hi,\n\nThank you for following up. The Q2 project is progressing well. I will send a detailed status report within the next 2 hours.\n\nBest regards",
        },
        "scoring": {"base": 88, "urgency_mult": 1.3, "history_mult": 1.05},
        "call_text": "Hello! This is your Personal OS Agent. I detected a high priority email from manager@company.com. Subject: Reply ASAP important project update needed. Confidence level: 94 percent. Recommended action: send email. Auto-reply has been sent to your manager.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    },
    {
        "id": str(uuid.uuid4()),
        "event_id": "demo-med-001",
        "event_type": "email",
        "action": "create_event",
        "confidence": 82,
        "priority": "medium",
        "user_response": "pending",
        "subject": "Can we reschedule our 3PM sync meeting?",
        "from_addr": "teammate.rahul@company.com",
        "snippet": "Hey, something came up. Can we move our 3PM standup to 4PM? Let me know if that works for you.",
        "urgency_keywords": [],
        "reason": "Teammate requesting meeting reschedule. Needs calendar update and confirmation reply.",
        "explanation": "Creating a new calendar event at 4PM is the clearest way to confirm the reschedule.",
        "requires_approval": True,
        "alternatives": [
            {"action": "send_email", "confidence": 70, "reason": "Could just reply confirming 4PM without creating event."},
            {"action": "no_action", "confidence": 20, "reason": "Ignoring reschedule request is unprofessional."},
        ],
        "action_args": {
            "summary": "Standup sync with Rahul (rescheduled)",
            "start_datetime": "2026-04-09T16:00:00+05:30",
            "end_datetime": "2026-04-09T16:30:00+05:30",
        },
        "scoring": {"base": 80, "urgency_mult": 1.0, "history_mult": 1.02},
        "call_text": "Hello! This is your Personal OS Agent. I detected a medium priority email from teammate.rahul@company.com. Subject: Can we reschedule our 3PM sync meeting? Confidence level: 82 percent. Recommended action: create event. Please review and approve or reject on your dashboard.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    },
    {
        "id": str(uuid.uuid4()),
        "event_id": "demo-med-002",
        "event_type": "email",
        "action": "send_email",
        "confidence": 75,
        "priority": "medium",
        "user_response": "pending",
        "subject": "Interview with Google — Confirmation Required",
        "from_addr": "recruiter@google.com",
        "snippet": "Hi, we would like to confirm your availability for the technical interview on April 10th at 3PM IST. Please confirm by tomorrow.",
        "urgency_keywords": [],
        "reason": "Recruiter requesting interview confirmation. Time-sensitive but not urgent.",
        "explanation": "Sending a confirmation email is the appropriate action to secure the interview slot.",
        "requires_approval": True,
        "alternatives": [
            {"action": "create_event", "confidence": 60, "reason": "Could create calendar event simultaneously."},
            {"action": "no_action", "confidence": 5, "reason": "Not confirming may result in losing the slot."},
        ],
        "action_args": {
            "to": "recruiter@google.com",
            "subject": "Re: Interview Confirmation — April 10th",
            "body": "Hi,\n\nThank you for reaching out. I confirm my availability for the technical interview on April 10th at 3PM IST.\n\nLooking forward to it!\n\nBest regards",
        },
        "scoring": {"base": 73, "urgency_mult": 1.0, "history_mult": 1.02},
        "call_text": "Hello! This is your Personal OS Agent. I detected a medium priority email from recruiter@google.com. Subject: Interview with Google, Confirmation Required. Confidence level: 75 percent. Recommended action: send confirmation email. Please review and approve or reject on your dashboard.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    },
    {
        "id": str(uuid.uuid4()),
        "event_id": "demo-low-001",
        "event_type": "email",
        "action": "no_action",
        "confidence": 55,
        "priority": "low",
        "user_response": "silent_discarded",
        "subject": "This week in AI: 10 things you need to know",
        "from_addr": "newsletter@techdigest.io",
        "snippet": "Welcome to this week's AI roundup. ChatGPT updates, Gemini news, and the top papers from arXiv...",
        "urgency_keywords": [],
        "reason": "Weekly newsletter with no actionable content. No user intervention required.",
        "explanation": "No action needed for informational newsletters. Silently discarded.",
        "requires_approval": False,
        "alternatives": [
            {"action": "send_email", "confidence": 5, "reason": "No reason to reply to a newsletter."},
            {"action": "move_file", "confidence": 3, "reason": "Not applicable to email."},
        ],
        "action_args": {},
        "scoring": {"base": 55, "urgency_mult": 1.0, "history_mult": 1.0},
        "call_text": "Hello! This is your Personal OS Agent. I detected a low priority email from newsletter@techdigest.io. Subject: This week in AI. Confidence level: 55 percent. Action was silently discarded due to low confidence.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    },
    {
        "id": str(uuid.uuid4()),
        "event_id": "demo-low-002",
        "event_type": "email",
        "action": "no_action",
        "confidence": 42,
        "priority": "low",
        "user_response": "silent_discarded",
        "subject": "Candidates 2026 Special Offer! - 26% Off on Premium",
        "from_addr": "offers@somesite.com",
        "snippet": "Exclusive deal just for you! Get 26% off on our premium plan. Limited time offer. Use code SAVE26.",
        "urgency_keywords": [],
        "reason": "Promotional marketing email. No action required.",
        "explanation": "Discounts and promotional offers do not require any automated action.",
        "requires_approval": False,
        "alternatives": [
            {"action": "send_email", "confidence": 2, "reason": "No need to reply to promotional emails."},
        ],
        "action_args": {},
        "scoring": {"base": 42, "urgency_mult": 1.0, "history_mult": 1.0},
        "call_text": "Hello! This is your Personal OS Agent. I detected a low priority promotional email. No action was taken. Confidence level: 42 percent.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    },
]


async def seed(redis_url: str = "redis://localhost:6379/0") -> None:
    r = aioredis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    try:
        await r.ping()
    except Exception as e:
        print(f"[Seed] ERROR: Cannot connect to Redis — {e}")
        return

    print("\n[SeedDashboard] Injecting plans directly into emails:all + dashboard:pending...\n")

    for plan in PLANS:
        await r.hset("emails:all", plan["id"], json.dumps(plan))
        if plan["user_response"] == "pending":
            await r.hset("dashboard:pending", plan["id"], json.dumps(plan))
        label = {"auto_executed": "AUTO  ", "pending": "DASH  ", "silent_discarded": "SILENT"}.get(plan["user_response"], "?")
        print(f"  [{label}] ({plan['confidence']:3d}%) [{plan['priority'].upper():<6}] {plan['subject'][:55]}")

    await r.aclose()
    print(f"\n[SeedDashboard] Done! {len(PLANS)} plans seeded.")
    print("[SeedDashboard] Open: http://localhost:8080")


if __name__ == "__main__":
    asyncio.run(seed())
