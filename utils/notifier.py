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
        # Serialise all outgoing calls — next call waits until the current one
        # reaches a terminal state (completed / failed / busy / no-answer).
        self._call_lock = asyncio.Lock()

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
        """Route to real Twilio call or simulation based on configuration.

        Phone calls are only placed for high-priority plans — medium/low priority
        plans are logged but do not trigger a call to avoid notification fatigue.
        """
        priority = (plan.get("priority") or "").lower()
        if priority != "high":
            logger.info(
                "call_skipped_low_priority",
                plan_id=plan.get("id"),
                action=plan.get("action"),
                priority=priority,
            )
            print(
                f"[Notifier] Skipping call — priority is '{priority}' "
                f"(calls only sent for high-priority)"
            )
            return

        # Prefer the pre-built call_text crafted by the Planner (more contextual)
        message = plan.get("call_text") or self._build_message(plan, result)
        if self._twilio_client:
            await self._make_real_call(message, plan)
        else:
            self._simulate_call(message, plan)

    async def voice_ask(self, plan: dict[str, Any]) -> bool:
        """
        Place an outbound voice call that speaks the pending action and
        gathers user speech for approval/rejection/modification.

        Requires TWILIO_WEBHOOK_BASE_URL to be set (e.g. ngrok public URL).
        Stores the plan in Redis under voice:plan:{plan_id} so the webhook
        can retrieve it when Twilio calls back.

        Returns True if the call was placed, False if not configured or failed.
        """
        if not self._twilio_client or not self._cfg.voice_approval_enabled:
            return False

        plan_id = plan.get("id", "unknown")
        base_url = self._cfg.twilio_webhook_base_url.rstrip("/")
        voice_url = f"{base_url}/api/twilio/voice/{plan_id}"

        # Persist plan for the webhook (5-minute TTL)
        import json
        from memory.redis_client import RedisClient
        redis = RedisClient.get_instance()
        await redis._redis.setex(f"voice:plan:{plan_id}", 300, json.dumps(plan))

        try:
            call = await asyncio.to_thread(
                self._twilio_client.calls.create,
                url=voice_url,
                to=self._cfg.twilio_to_number,
                from_=self._cfg.twilio_from_number,
            )
            logger.info(
                "voice_ask_call_placed",
                call_sid=call.sid,
                plan_id=plan_id,
                to=self._cfg.twilio_to_number,
            )
            print(
                f"\n[Notifier] VOICE ASK CALL placed"
                f"\n  Call SID : {call.sid}"
                f"\n  Plan ID  : {plan_id}"
                f"\n  Action   : {plan.get('action')}"
            )
            return True
        except Exception as exc:
            logger.error("voice_ask_call_failed", error=str(exc), plan_id=plan_id)
            print(f"[Notifier] Voice ask call failed: {exc}")
            return False

    async def _make_real_call(self, message: str, plan: dict[str, Any]) -> None:
        """
        Place an outbound Twilio call with inline TwiML.
        Acquires _call_lock so the next call waits until this one completes —
        the user hears each call fully before the next one rings.
        """
        safe_msg = (
            message.replace("&", "and")
            .replace("<", "")
            .replace(">", "")
            .replace('"', "'")
        )
        twiml = f'<Response><Say voice="alice">{safe_msg}</Say><Pause length="1"/></Response>'

        async with self._call_lock:
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
                # Wait for this call to reach a terminal state before allowing
                # the next call — ensures the user finishes hearing one message
                # before the phone rings again.
                await self._wait_for_call(call.sid)
            except Exception as exc:
                logger.error("twilio_call_failed", error=str(exc))
                print(f"[Notifier] Twilio call failed: {exc} — falling back to simulation")
                self._simulate_call(message, plan)

    async def _wait_for_call(self, call_sid: str, poll_interval: int = 3, timeout: int = 120) -> None:
        """Poll Twilio every `poll_interval` s until the call is in a terminal state."""
        _TERMINAL = {"completed", "failed", "busy", "no-answer", "canceled"}
        for _ in range(timeout // poll_interval):
            await asyncio.sleep(poll_interval)
            try:
                call = await asyncio.to_thread(
                    lambda: self._twilio_client.calls(call_sid).fetch()
                )
                if call.status in _TERMINAL:
                    logger.info("twilio_call_completed", call_sid=call_sid, status=call.status)
                    print(f"[Notifier] Call {call_sid[:8]}... ended — status: {call.status}")
                    return
            except Exception:
                return  # can't poll — release lock anyway

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
