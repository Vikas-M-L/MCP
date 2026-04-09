"""
Voice email test — pushes a send_email plan directly to approvals:pending.

The Executor picks it up, routes it through voice approval (if configured),
calls your phone, waits for you to speak, then sends the email.

Usage:
    python scripts/test_voice_email.py
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone

# ── Config ─────────────────────────────────────────────────────────────────────
TO_EMAIL    = "vikas935314@gmail.com"
SUBJECT     = "PersonalOS Agent — Voice Approved Message"
BODY        = ""  # Filled by your voice — say "modify <your message> done"


async def main() -> None:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from memory.redis_client import RedisClient
    from config.settings import get_settings

    cfg = get_settings()
    redis = RedisClient.get_instance()

    plan_id = str(uuid.uuid4())
    plan = {
        "id":              plan_id,
        "event_id":        "test-voice-" + plan_id[:8],
        "event_type":      "email",
        "action":          "send_email",
        "action_args":     {"to": TO_EMAIL, "subject": SUBJECT, "body": BODY},
        "confidence":      78,
        "priority":        "high",
        "reason":          f"Test voice approval — send email to {TO_EMAIL}",
        "subject":         SUBJECT,
        "from_addr":       "test@personalos.demo",
        "snippet":         "Voice approval test email",
        "urgency_keywords": ["urgent"],
        "requires_approval": True,
        "approved_override": False,
        "alternatives":    [],
        "explanation":     "Direct voice test",
        "scoring":         {"base": 78, "urgency_mult": 1.0, "history_mult": 1.0},
        "call_text": (
            f"Hey! Your Personal OS Agent here. "
            f"I need your approval to send a test email to {TO_EMAIL}. "
            f"Say yes to approve, no to reject, "
            f"or modify followed by your custom message."
        ),
        "user_response":   "pending",
        "created_at":      datetime.now(timezone.utc).isoformat(),
    }

    await redis.push_approval(plan)
    await redis.push_email_record(plan)
    await redis.append_activity_log({
        "timestamp": plan["created_at"],
        "agent":     "TestScript",
        "action":    f"TEST VOICE: injected send_email plan → {TO_EMAIL}",
        "plan_id":   plan_id,
    })

    print(f"\n{'='*55}")
    print(f"  VOICE TEST PLAN INJECTED")
    print(f"{'='*55}")
    print(f"  Plan ID   : {plan_id}")
    print(f"  Action    : send_email")
    print(f"  To        : {TO_EMAIL}")
    print(f"  Confidence: 78%")
    print(f"  Voice     : {'ON — phone will ring' if cfg.voice_approval_enabled else 'OFF — check .env'}")
    print(f"{'='*55}")

    if not cfg.voice_approval_enabled:
        print("\n  WARNING: TWILIO_WEBHOOK_BASE_URL not set.")
        print("  Set it in .env and restart the server.\n")
    else:
        print(f"\n  Webhook URL : {cfg.twilio_webhook_base_url}")
        print(f"  Calling     : {cfg.twilio_to_number}")
        print(f"\n  Your phone will ring in a few seconds.")
        print(f"  Say 'yes' to send the email.")
        print(f"  Say 'no' to reject.")
        print(f"  Say 'modify reply with your own message' to customise.\n")

    await redis.close()


if __name__ == "__main__":
    asyncio.run(main())
