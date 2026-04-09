"""
Twilio voice approval webhook — speech-to-intent pipeline.

Flow:
  1. Executor places a Twilio call with url=/api/twilio/voice/{plan_id}
  2. Twilio fetches TwiML from GET /api/twilio/voice/{plan_id}
     → Returns <Gather input="speech"> asking user to approve/reject/modify
  3. User speaks; Twilio POSTs SpeechResult to /api/twilio/speech/{plan_id}
  4. LLM classifies intent: APPROVE / REJECT / MODIFY / UNCLEAR
  5. APPROVE/MODIFY → push plan back to approvals:pending with approved_override=True
     REJECT         → remove from dashboard, mark rejected
     UNCLEAR        → re-prompt the user
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Form
from fastapi.responses import Response

from api.ws import manager
from memory.redis_client import RedisClient
from utils.logger import get_logger

router = APIRouter()
logger = get_logger("VoiceApproval")

_VOICE_PLAN_TTL = 300  # seconds a pending voice plan lives in Redis


# ── TwiML helper ──────────────────────────────────────────────────────────────

def _twiml(inner_xml: str) -> Response:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner_xml}</Response>'
    return Response(content=xml, media_type="application/xml")


def _safe(text: str) -> str:
    """Strip XML-unsafe characters from a string destined for <Say>."""
    return (
        text.replace("&", "and")
            .replace("<", "")
            .replace(">", "")
            .replace('"', "'")
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.api_route("/api/twilio/voice/{plan_id}", methods=["GET", "POST"])
async def voice_prompt(plan_id: str) -> Response:
    """
    Return TwiML that speaks the pending action and gathers user voice input.
    Twilio calls this URL when the outbound call is answered.
    """
    from config.settings import get_settings
    cfg = get_settings()

    redis = RedisClient.get_instance()
    raw = await redis._redis.get(f"voice:plan:{plan_id}")

    if not raw:
        return _twiml('<Say voice="alice">Sorry, this action request has already expired or been processed.</Say>')

    plan = json.loads(raw)
    base_url = cfg.twilio_webhook_base_url.rstrip("/")
    speech_url = f"{base_url}/api/twilio/speech/{plan_id}"

    # Prefer the planner's call_text when available (more contextual)
    if plan.get("call_text"):
        spoken = _safe(plan["call_text"])
        prompt = (
            f"{spoken} "
            "Say yes to approve, no to reject, "
            "or say modify followed by your instructions."
        )
    else:
        action = _safe(plan.get("action", "an action").replace("_", " "))
        subject = _safe(plan.get("subject", ""))
        reason = _safe((plan.get("reason") or "")[:120])
        prompt = (
            f"Hey! Your Personal OS needs your approval. "
            f"Requested action: {action}. "
            f"{'Email subject: ' + subject + '. ' if subject else ''}"
            f"{'Reason: ' + reason + '. ' if reason else ''}"
            "Say yes to approve, no to reject, "
            "or say modify followed by your instructions."
        )

    gather_xml = (
        f'<Gather input="speech" action="{speech_url}" method="POST" '
        f'speechTimeout="10" language="en-US" '
        f'enhanced="true" speechModel="phone_call" profanityFilter="false">'
        f'<Say voice="alice">{prompt}</Say>'
        f'</Gather>'
        f'<Say voice="alice">We did not catch that. '
        f'Please check your dashboard at port 8080. Goodbye.</Say>'
    )
    return _twiml(gather_xml)


@router.post("/api/twilio/speech/{plan_id}")
async def voice_speech(
    plan_id: str,
    SpeechResult: str = Form(default=""),
) -> Response:
    """
    Receive Twilio's SpeechResult, classify intent via LLM, and act.
    Returns TwiML spoken confirmation to the caller.
    """
    try:
        return await _handle_speech(plan_id, SpeechResult)
    except Exception as exc:
        logger.error("voice_speech_handler_crashed", plan_id=plan_id, error=str(exc))
        print(f"[VoiceApproval] HANDLER ERROR for plan {plan_id}: {exc}")
        return _twiml(
            '<Say voice="alice">Sorry, something went wrong. '
            'Please approve or reject from your dashboard. Goodbye.</Say>'
        )


async def _handle_speech(plan_id: str, SpeechResult: str) -> Response:
    from config.settings import get_settings
    cfg = get_settings()

    redis = RedisClient.get_instance()
    raw = await redis._redis.get(f"voice:plan:{plan_id}")

    print(f"[VoiceApproval] Speech received — plan={plan_id} speech='{SpeechResult[:80]!r}'")

    if not raw:
        return _twiml(
            '<Say voice="alice">This action has already been processed. Goodbye.</Say>'
        )

    plan = json.loads(raw)
    user_speech = SpeechResult.strip()
    action_label = _safe(plan.get("action", "the action").replace("_", " "))

    logger.info("voice_speech_received", plan_id=plan_id, speech=user_speech[:100])

    # Fast-path: no speech detected — re-prompt without wasting an LLM call
    if not user_speech:
        base_url = cfg.twilio_webhook_base_url.rstrip("/")
        speech_url = f"{base_url}/api/twilio/speech/{plan_id}"
        return _twiml(
            f'<Gather input="speech" action="{speech_url}" method="POST" '
            f'speechTimeout="10" enhanced="true" speechModel="phone_call" profanityFilter="false">'
            f'<Say voice="alice">Sorry, I did not hear you. '
            f'Please say yes to approve, no to reject, '
            f'or modify followed by your instructions.</Say>'
            f'</Gather>'
            f'<Say voice="alice">No response received. Check your dashboard. Goodbye.</Say>'
        )

    intent, modification = await _detect_intent(user_speech, plan)

    # ── APPROVE ───────────────────────────────────────────────────────────────
    if intent == "APPROVE":
        plan["approved_override"] = True
        await redis.push_approval(plan)
        await redis._redis.delete(f"voice:plan:{plan_id}")
        await _log_activity(redis, "VOICE APPROVED", plan, user_speech)
        await manager.broadcast({"type": "refresh"})
        logger.info("voice_approved", plan_id=plan_id)
        return _twiml(
            f'<Say voice="alice">Got it! I will {action_label} right away. Goodbye!</Say>'
        )

    # ── REJECT ────────────────────────────────────────────────────────────────
    if intent == "REJECT":
        await redis.remove_dashboard_item(plan_id)
        await redis.update_email_response(plan_id, "rejected_by_voice")
        await redis._redis.delete(f"voice:plan:{plan_id}")
        await _log_activity(redis, "VOICE REJECTED", plan, user_speech)
        await manager.broadcast({"type": "refresh"})
        logger.info("voice_rejected", plan_id=plan_id)
        return _twiml(
            '<Say voice="alice">Okay, I will not proceed. Action discarded. Goodbye!</Say>'
        )

    # ── MODIFY ────────────────────────────────────────────────────────────────
    if intent == "MODIFY":
        instruction = _clean_instruction(modification or user_speech)
        plan["approved_override"] = True
        plan["voice_modification"] = instruction
        # Inject instruction into the most appropriate text field of action_args
        args = plan.get("action_args") or {}
        for field in ("body", "description", "destination"):
            if field in args:
                existing = (args[field] or "").strip()
                if not existing:
                    # Empty body — wrap voice instruction in a clean template
                    args[field] = (
                        f"Hi,\n\n"
                        f"{instruction}\n\n"
                        f"---\n"
                        f"Sent via PersonalOS Agent (voice approved)\n"
                        f"Powered by SOLARIS X"
                    )
                else:
                    # LLM already drafted a body — append the instruction
                    args[field] = f"{existing}\n\n{instruction}"
                break
        else:
            args["voice_instruction"] = instruction
        plan["action_args"] = args
        await redis.push_approval(plan)
        await redis._redis.delete(f"voice:plan:{plan_id}")
        await _log_activity(redis, "VOICE MODIFIED+APPROVED", plan, user_speech)
        await manager.broadcast({"type": "refresh"})
        logger.info("voice_modified", plan_id=plan_id, instruction=instruction[:80])
        mod_summary = _safe(instruction[:60])
        return _twiml(
            f'<Say voice="alice">Got it! I will {action_label} with your changes: '
            f'{mod_summary}. Goodbye!</Say>'
        )

    # ── UNCLEAR — re-prompt ───────────────────────────────────────────────────
    base_url = cfg.twilio_webhook_base_url.rstrip("/")
    speech_url = f"{base_url}/api/twilio/speech/{plan_id}"
    return _twiml(
        f'<Gather input="speech" action="{speech_url}" method="POST" '
        f'speechTimeout="10" enhanced="true" speechModel="phone_call" profanityFilter="false">'
        f'<Say voice="alice">Sorry, I did not understand. '
        f'Please say yes to approve, no to reject, '
        f'or say modify followed by your instructions.</Say>'
        f'</Gather>'
        f'<Say voice="alice">No response received. Check your dashboard at port 8080. Goodbye.</Say>'
    )


# ── Intent detection ──────────────────────────────────────────────────────────

async def _detect_intent(speech: str, plan: dict) -> tuple[str, str]:
    """
    Call the LLM to classify user speech as APPROVE / REJECT / MODIFY / UNCLEAR.
    Falls back to keyword matching if the LLM call fails.
    Returns (intent, modification_instruction).
    """
    from config.settings import get_settings
    from openai import AsyncOpenAI

    cfg = get_settings()
    client = AsyncOpenAI(base_url=cfg.openrouter_base_url, api_key=cfg.openrouter_api_key)
    action = plan.get("action", "the requested action")

    try:
        resp = await client.chat.completions.create(
            model=cfg.openrouter_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an intent classifier for a voice-controlled AI agent. "
                        "Classify user speech and respond ONLY with valid JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f'The AI agent asked for approval to: "{action}".\n'
                        f'The user responded: "{speech}"\n\n'
                        "Classify the intent and return JSON:\n"
                        '{"intent": "APPROVE"|"REJECT"|"MODIFY"|"UNCLEAR", '
                        '"modification": "<the exact instruction content after stripping trigger words like modify/change/but>"}\n\n'
                        "Rules:\n"
                        '- "yes", "sure", "go ahead", "do it", "approve", "okay" → APPROVE, modification=""\n'
                        '- "no", "don\'t", "stop", "reject", "cancel", "skip" → REJECT, modification=""\n'
                        '- ANY message containing an instruction → MODIFY, modification=<just the instruction, no trigger word>\n'
                        '  Examples:\n'
                        '  "modify send the message I have worked" → MODIFY, modification="send the message I have worked"\n'
                        '  "yes but make it polite" → MODIFY, modification="make it polite"\n'
                        '  "reply that I will send it by evening" → MODIFY, modification="I will send it by evening"\n'
                        '  "change to I am unavailable" → MODIFY, modification="I am unavailable"\n'
                        "- Anything truly unclear → UNCLEAR"
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=80,
        )
        content = resp.choices[0].message.content.strip()
        # Strip markdown fences if present
        if "```" in content:
            parts = content.split("```")
            content = parts[1].lstrip("json").strip() if len(parts) > 1 else content
        parsed = json.loads(content)
        return parsed.get("intent", "UNCLEAR"), parsed.get("modification", "")
    except Exception as exc:
        logger.warning("intent_llm_failed", error=str(exc), using="keyword_fallback")
        return _keyword_intent(speech)


_MODIFY_TRIGGERS = [
    "modify ", "change ", "but ", "however ", "instead ",
    "reply that ", "tell them ", "say that ", "make it ",
    "write ", "send message ", "and send ",
]

# User can say any of these at the end to signal message is complete
_STOP_WORDS = ["done", "stop", "over", "end", "finish", "that's it",
               "thats it", "complete", "finished", "okay done", "ok done"]


def _clean_instruction(speech: str) -> str:
    """Strip leading trigger words and trailing stop words from the instruction."""
    text = speech.strip()
    lower = text.lower()

    # Strip leading trigger word
    for trigger in _MODIFY_TRIGGERS:
        if lower.startswith(trigger):
            text = text[len(trigger):].strip()
            lower = text.lower()
            break

    # Strip trailing stop word
    for stop in _STOP_WORDS:
        if lower.endswith(stop):
            text = text[: len(text) - len(stop)].strip().rstrip(",. ")
            break

    return text or speech


def _keyword_intent(speech: str) -> tuple[str, str]:
    """Simple keyword fallback when the LLM call fails."""
    import re
    text = speech.lower()

    def has_word(word: str) -> bool:
        """Whole-word match to avoid 'no' matching 'know' or 'notify'."""
        return bool(re.search(rf"\b{re.escape(word)}\b", text))

    # Check MODIFY first — "no but change it" should be MODIFY, not REJECT
    modify_words = ["but", "however", "instead", "modify", "change",
                    "reply that", "tell them", "say that", "make it"]
    if any(has_word(w) for w in modify_words):
        return "MODIFY", _clean_instruction(speech)

    reject_words = ["no", "nope", "stop", "reject", "cancel", "skip", "negative", "don't"]
    if any(has_word(w) for w in reject_words):
        return "REJECT", ""

    approve_words = ["yes", "sure", "go ahead", "do it", "approve",
                     "okay", "yep", "yeah", "correct", "proceed"]
    if any(has_word(w) for w in approve_words):
        return "APPROVE", ""

    return "UNCLEAR", ""


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _log_activity(redis: RedisClient, label: str, plan: dict, speech: str) -> None:
    await redis.append_activity_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "VoiceApproval",
        "action": f"{label}: {plan.get('action')} | said: '{speech[:60]}'",
        "confidence": plan.get("confidence"),
        "plan_id": plan.get("id"),
    })
