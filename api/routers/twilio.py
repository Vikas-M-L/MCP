"""
Twilio test-call endpoint.
  POST /api/twilio/test  → place a real or simulated outbound call
"""
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.post("/api/twilio/test")
async def twilio_test_call() -> dict:
    """Trigger a real Twilio test call (or print simulation if not configured)."""
    from config.settings import get_settings
    cfg = get_settings()

    message = (
        "Hello! This is your Personal OS Agent calling from the SOLARIS X Hackathon demo. "
        "I detected a high priority email requiring immediate action. "
        "Confidence level: 94 percent. Action taken: send reply email. "
        "Please check your dashboard at localhost colon 8080 for details. Thank you."
    )

    if cfg.twilio_enabled:
        try:
            from twilio.rest import Client
            client   = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)
            safe_msg = message.replace("&", "and").replace("<", "").replace(">", "").replace('"', "'")

            if cfg.voice_approval_enabled:
                # Demo the full voice approval pipeline — speech input
                base_url = cfg.twilio_webhook_base_url.rstrip("/")
                twiml = (
                    f'<Response>'
                    f'<Gather input="speech" action="{base_url}/api/twilio/speech/test-call" '
                    f'method="POST" speechTimeout="3" language="en-US">'
                    f'<Say voice="alice">{safe_msg} '
                    f'Say yes to approve, no to reject, or modify followed by your instructions.</Say>'
                    f'</Gather>'
                    f'<Say voice="alice">No input detected. Check your dashboard at port 8080.</Say>'
                    f'</Response>'
                )
            else:
                twiml = (
                    f'<Response>'
                    f'<Say voice="alice">{safe_msg}</Say>'
                    f'<Pause length="1"/>'
                    f'</Response>'
                )
            call = await asyncio.to_thread(
                client.calls.create,
                twiml=twiml,
                to=cfg.twilio_to_number,
                from_=cfg.twilio_from_number,
            )
            from memory.redis_client import RedisClient
            await RedisClient.get_instance().append_activity_log({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "Dashboard",
                "action": f"TEST CALL placed — SID: {call.sid}",
            })
            return {"status": "placed", "call_sid": call.sid, "to": cfg.twilio_to_number}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    else:
        return {
            "status": "simulated",
            "message": message,
            "note": "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER in .env for a real call",
        }
