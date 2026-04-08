"""
Notifier — Twilio outbound call or [SIMULATED CALL] fallback.
If TWILIO_* env vars are set, places a real phone call with TwiML inline voice.
Otherwise, prints a detailed simulation block to console and logs it.

Twilio's REST client is synchronous — all calls are wrapped in asyncio.to_thread()
to avoid blocking the executor's event loop.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any

from utils.logger import get_logger

logger = get_logger("Notifier")


class Notifier:
    def __init__(self) -> None:
        from config.settings import get_settings
        self._cfg = get_settings()
        self._twilio_client = None

        if self._cfg.twilio_enabled:
            try:
                from twilio.rest import Client
                self._twilio_client = Client(
                    self._cfg.twilio_account_sid,
                    self._cfg.twilio_auth_token,
                )
                logger.info("twilio_initialized", from_number=self._cfg.twilio_from_number)
            except Exception as exc:
                logger.warning("twilio_init_failed", error=str(exc))
                self._twilio_client = None

    async def notify(self, plan: dict[str, Any], result: dict[str, Any]) -> None:
        """Route to real Twilio call or simulation based on configuration."""
        message = self._build_message(plan, result)
        if self._twilio_client:
            await self._make_real_call(message, plan)
        else:
            self._simulate_call(message, plan)

    async def _make_real_call(self, message: str, plan: dict[str, Any]) -> None:
        """
        Place an outbound Twilio call with inline TwiML.
        Uses twiml= parameter so no public URL is required.
        """
        # Sanitize message for TwiML (escape XML special chars)
        safe_msg = (
            message.replace("&", "and")
            .replace("<", "")
            .replace(">", "")
            .replace('"', "'")
        )
        twiml = f'<Response><Say voice="alice">{safe_msg}</Say><Pause length="1"/><Say voice="alice">Press any key to confirm.</Say></Response>'

        try:
            call = await asyncio.to_thread(
                self._twilio_client.calls.create,
                twiml=twiml,
                to=self._cfg.twilio_to_number,
                from_=self._cfg.twilio_from_number,
            )
            logger.info(
                "twilio_call_placed",
                call_sid=call.sid,
                to=self._cfg.twilio_to_number,
                action=plan.get("action"),
            )
            print(
                f"\n[Notifier] CALL PLACED via Twilio"
                f"\n  Call SID  : {call.sid}"
                f"\n  To        : {self._cfg.twilio_to_number}"
                f"\n  Message   : {message[:120]}..."
            )
        except Exception as exc:
            logger.error("twilio_call_failed", error=str(exc))
            print(f"[Notifier] Twilio call failed: {exc} — falling back to simulation")
            self._simulate_call(message, plan)

    def _simulate_call(self, message: str, plan: dict[str, Any]) -> None:
        """Print a formatted simulation block — useful when Twilio is not configured."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        border = "=" * 60
        print(f"""
{border}
[SIMULATED CALL]  {ts}
  To      : +1-xxx-xxx-xxxx (TWILIO_TO_NUMBER not set)
  From    : PersonalOS Agent
  Action  : {plan.get('action', 'unknown')}
  Confidence: {plan.get('confidence', '?')}%

  Message:
  "{message}"

  → Set TWILIO_* env vars to place a real call
{border}""")
        logger.info(
            "simulated_call",
            action=plan.get("action"),
            confidence=plan.get("confidence"),
            message=message[:200],
        )

    def _build_message(self, plan: dict[str, Any], result: dict[str, Any]) -> str:
        """Compose the spoken message for the call."""
        action = plan.get("action", "an action")
        reason = plan.get("reason", "")
        confidence = plan.get("confidence", 0)
        result_str = str(result.get("status", result))[:80] if result else "completed"

        return (
            f"Hello, this is your Personal OS Agent. "
            f"I have automatically taken action on your behalf. "
            f"Action taken: {action.replace('_', ' ')}. "
            f"Reason: {reason}. "
            f"Confidence level: {confidence} percent. "
            f"Result: {result_str}. "
            f"If this was incorrect, please check your dashboard at localhost colon 8080."
        )
