"""
Demo Twilio call — one shot test.
Run: python demo_call.py
"""
import sys
sys.path.insert(0, ".")
from config.settings import get_settings

cfg = get_settings()

if not cfg.twilio_enabled:
    print("[ERROR] Twilio not configured. Set TWILIO_* vars in .env")
    sys.exit(1)

from twilio.rest import Client

client = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)

message = (
    "Hello! This is your Personal OS Agent calling from the SOLARIS X Hackathon demo. "
    "I detected an urgent email from your professor about an assignment deadline. "
    "I have automatically drafted a reply on your behalf. "
    "Confidence level: 94 percent. Action taken: send reply email. "
    "Thank you. Goodbye."
)

twiml = f'<Response><Say voice="alice">{message}</Say></Response>'

print(f"[Demo] Placing call to {cfg.twilio_to_number} from {cfg.twilio_from_number} ...")

call = client.calls.create(
    twiml=twiml,
    to=cfg.twilio_to_number,
    from_=cfg.twilio_from_number,
)

print(f"[Demo] Call placed! SID: {call.sid}")
print(f"[Demo] Status: {call.status}")
print(f"[Demo] Your phone ({cfg.twilio_to_number}) should ring in a few seconds.")
