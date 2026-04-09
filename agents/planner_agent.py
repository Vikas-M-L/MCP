"""
PlannerAgent — The Brain of the system.
Reads events from Redis events:queue (blocking), calls the OpenRouter LLM to
generate a structured JSON action plan with 2-3 alternatives, then applies
a decision scoring formula before pushing the best plan to approvals:pending.

Decision scoring:
  adjusted = base_confidence * urgency_multiplier * history_multiplier
  urgency_multiplier  : 1.0 – 1.3 based on urgency keyword count
  history_multiplier  : 0.9 – 1.1 based on ChromaDB historical approval rate
"""
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from openai import AsyncOpenAI

from agents.base_agent import BaseAgent
from agents.meeting_resolver import (
    _IST,
    detect_requested_time,
    find_free_slots,
    format_calendar_for_prompt,
    has_time_conflict,
    is_meeting_request,
    parse_busy_slots,
    parse_meeting_window,
)
from memory.redis_client import RedisClient
from memory.chroma_memory import ChromaMemory

# JSON schema the LLM must follow (injected into system prompt)
PLAN_SCHEMA = """{
  "action": "<mcp_tool_name>",
  "confidence": <integer 0-100>,
  "priority": "high|medium|low",
  "reason": "<why this action>",
  "requires_approval": <true|false>,
  "alternatives": [
    {"action": "<tool_name>", "confidence": <int>, "reason": "<reason>"},
    {"action": "<tool_name>", "confidence": <int>, "reason": "<reason>"}
  ],
  "explanation": "<why chosen over alternatives, what history informed this>",
  "action_args": {<tool-specific arguments as key-value pairs>}
}"""

AVAILABLE_TOOLS = """
- send_email(to, subject, body)       — reply or compose Gmail message
- read_emails(max_results, query)     — fetch more emails if needed
- create_event(summary, start_datetime, end_datetime, description, attendees) — book calendar slot
- read_calendar(days_ahead)           — check schedule before booking
- list_files(directory)               — inspect files in sandbox
- move_file(source, destination)      — organize a file
- no_action                           — do nothing (use for low-confidence or irrelevant events)
"""


def _build_confirmation_email(plan: dict[str, Any], event: dict[str, Any]) -> dict[str, str]:
    """
    Build a confirmation reply email when a meeting is auto-scheduled on a free slot.
    Sent by the Executor immediately after create_event succeeds.
    """
    payload    = event.get("payload", {})
    to         = payload.get("from", plan.get("from_addr", ""))
    orig_subj  = payload.get("subject", plan.get("subject", "our meeting"))
    start_iso  = plan.get("action_args", {}).get("start_datetime", "")
    summary    = plan.get("action_args", {}).get("summary", "the meeting")

    # Format datetime for the email body
    time_str = start_iso
    try:
        from agents.meeting_resolver import _parse_dt
        dt_ist = _parse_dt(start_iso).astimezone(_IST)
        time_str = dt_ist.strftime("%A, %d %b %Y at %I:%M %p IST")
    except Exception:
        pass

    body = (
        f"Hi,\n\n"
        f"I've gone ahead and scheduled {summary} for {time_str}. "
        f"A calendar invite has been sent to you.\n\n"
        f"Looking forward to it!\n\n"
        f"Best regards"
    )
    return {
        "to":      to,
        "subject": f"Re: {orig_subj}",
        "body":    body,
    }


def _extract_email_address(from_header: str) -> str:
    m = re.search(r"<([^>]+)>", from_header or "")
    if m:
        return m.group(1).strip()
    if "@" in (from_header or ""):
        return from_header.strip()
    return ""


def _calendar_free_slot_ready(event: dict[str, Any]) -> bool:
    """True when we parsed start/end and Google Calendar has no overlap."""
    return (
        bool(event.get("_meeting_start_iso"))
        and bool(event.get("_meeting_end_iso"))
        and event.get("_calendar_conflict") is False
    )


def _build_deterministic_meeting_plan(event: dict[str, Any]) -> dict[str, Any]:
    """
    Full plan dict when calendar is free for a parsed sender window — bypasses LLM.
    """
    payload = event.get("payload", {})
    start_iso = event["_meeting_start_iso"]
    end_iso = event["_meeting_end_iso"]
    from_raw = payload.get("from", "")
    attendee = _extract_email_address(from_raw)
    name = (from_raw.split("<")[0].strip() or "Guest").strip()
    subj = (payload.get("subject") or "").strip()

    if subj and len(subj) <= 120:
        summary = subj
    elif name:
        summary = f"Meeting with {name}"
    else:
        summary = "Scheduled event"
    return {
        "action": "create_event",
        "confidence": 100,
        "priority": "high",
        "reason": "Sender requested a specific time; calendar is free — auto-booked.",
        "requires_approval": False,
        "alternatives": [
            {
                "action": "send_email",
                "confidence": 40,
                "reason": "Reply by email instead of calendar (not recommended).",
            },
            {"action": "no_action", "confidence": 10, "reason": "Ignore request."},
        ],
        "explanation": (
            "Deterministic schedule: parsed date/time from the email and verified "
            "no overlap with existing calendar events."
        ),
        "action_args": {
            "summary": summary[:200],
            "start_datetime": start_iso,
            "end_datetime": end_iso,
            "description": "Scheduled automatically by PersonalOS Agent.",
            "attendees": [attendee] if attendee else [],
            "location": "",
        },
    }


def _build_call_text(plan: dict[str, Any], event: dict[str, Any]) -> str:
    """Build the spoken Twilio call script for this plan."""
    priority = plan.get("priority", "medium")
    action = plan.get("action", "no_action").replace("_", " ")
    confidence = plan.get("confidence", 0)
    reason = plan.get("reason", "")[:120]
    payload = event.get("payload", {})
    from_addr = payload.get("from", event.get("source", "unknown"))[:60]
    subject = payload.get("subject", event.get("summary", ""))[:80]

    # Calendar conflict — build a specific script so the user clearly understands
    # what was drafted and what they need to say.
    if plan.get("_is_conflict_reply"):
        requested_dt = event.get("_requested_dt", "the requested time")
        free_hint = plan.get("_conflict_free_hint", "")
        return (
            f"Hey! Your Personal OS Agent here. "
            f"You have a meeting request from {from_addr}. "
            f"Subject: {subject}. "
            f"I found a conflict in your calendar at {requested_dt}. "
            f"I have drafted a polite reply explaining the conflict"
            f"{' and suggesting ' + free_hint if free_hint else ''}. "
            f"Say yes to send the reply, no to reject, "
            f"or say modify followed by your instructions."
        )

    # Normal (non-meeting) plan — derive status line from confidence.
    if confidence >= 70:
        status_line = "Say yes to approve, no to reject, or modify followed by your instruction."
    else:
        status_line = "Action was discarded due to low confidence."

    return (
        f"Hey! Your Personal OS Agent here. "
        f"You have a {priority} priority email "
        f"from {from_addr}. "
        f"Subject: {subject}. "
        f"Recommended action: {action}. "
        f"Reason: {reason}. "
        f"{status_line}"
    )


class PlannerAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("Planner")
        from config.settings import get_settings
        cfg = get_settings()
        self._llm = AsyncOpenAI(
            base_url=cfg.openrouter_base_url,
            api_key=cfg.openrouter_api_key,
        )
        self._model = cfg.openrouter_model
        self._redis: RedisClient | None = None
        self._memory: ChromaMemory | None = None

    async def run(self) -> None:
        """Block on events:queue, plan each event, push to approvals:pending."""
        self._redis = RedisClient.get_instance()
        self._memory = ChromaMemory.from_settings()
        await self._memory.seed_default_preferences()

        self.logger.info("planner_started", model=self._model)
        print(f"[Planner] Ready — using model: {self._model}")

        while True:
            event = await self._redis.pop_event(timeout=0)
            if event is None:
                continue

            self.logger.info("planning_event", event_id=event["event_id"], type=event["type"])

            try:
                plan = await self._plan_event(event)
                if _calendar_free_slot_ready(event):
                    scored_plan = {**plan}
                    scored_plan["confidence"] = 100
                    scored_plan["scoring"] = {
                        "base": 100,
                        "urgency_mult": 1.0,
                        "history_mult": 1.0,
                        "approval_rate": 1.0,
                    }
                else:
                    scored_plan = await self._score_plan(plan, event)

                # Attach event metadata for executor + dashboard
                scored_plan["event_id"] = event["event_id"]
                scored_plan["event_type"] = event.get("type", "unknown")
                scored_plan["id"] = str(uuid.uuid4())
                scored_plan["created_at"] = datetime.now(timezone.utc).isoformat()
                scored_plan["user_response"] = "pending"

                # Enrich with context fields needed by the dashboard UI
                payload = event.get("payload", {})
                scored_plan["subject"] = payload.get(
                    "subject", event.get("summary", "")
                )
                scored_plan["from_addr"] = payload.get(
                    "from", event.get("source", "")
                )
                scored_plan["snippet"] = payload.get("snippet", "")[:200]
                scored_plan["urgency_keywords"] = event.get("urgency_keywords", [])

                # priority: use LLM classification if valid, else derive from confidence
                llm_priority = scored_plan.get("priority", "").lower()
                if llm_priority not in ("high", "medium", "low"):
                    c = scored_plan["confidence"]
                    llm_priority = "high" if c > 90 else "medium" if c >= 70 else "low"
                scored_plan["priority"] = llm_priority

                # Meeting-request routing:
                #   Calendar FREE  → auto-book (create_event) + confirmation email to sender.
                #                    approved_override=True, skip_call=True — no voice call.
                #   Calendar BUSY  → LLM drafted conflict reply (send_email).
                #                    Force confidence=85 so it ALWAYS routes to voice approval
                #                    call regardless of the LLM's confidence value.
                #                    User hears the conflict summary, says yes/no/modify.
                cal_conflict = event.get("_calendar_conflict")
                if _calendar_free_slot_ready(event):
                    # Parsed time + calendar free → deterministic create_event, no LLM drift.
                    scored_plan["approved_override"] = True
                    scored_plan["requires_approval"] = False
                    scored_plan["confidence"] = 100
                    scored_plan["skip_call"] = True
                    scored_plan["priority"] = "high"
                    scored_plan["confirmation_email"] = _build_confirmation_email(
                        scored_plan, event
                    )
                    print(
                        f"[Planner] Calendar FREE — auto-book + confirm email (no call): "
                        f"{scored_plan.get('action_args', {}).get('summary', 'event')}"
                    )
                elif cal_conflict is True:
                    # Calendar BUSY → always require voice approval before sending reply.
                    # Override whatever confidence the LLM produced so this never
                    # auto-executes (even if LLM said 92+).
                    scored_plan["confidence"] = 85
                    scored_plan["requires_approval"] = True
                    scored_plan["approved_override"] = False
                    scored_plan["skip_call"] = False
                    scored_plan["priority"] = "high"
                    scored_plan["_is_conflict_reply"] = True
                    # Carry free-slot hint into call_text (e.g. "Friday 10am or Monday 2pm")
                    free_slots = event.get("_free_slot_hints", [])
                    if free_slots:
                        scored_plan["_conflict_free_hint"] = " or ".join(free_slots[:2])
                    print(
                        f"[Planner] Calendar CONFLICT — conflict reply drafted, "
                        f"voice approval required (confidence forced to 85)"
                    )

                # Build spoken call script AFTER routing flags are set so
                # _is_conflict_reply and _conflict_free_hint are already present.
                scored_plan["call_text"] = _build_call_text(scored_plan, event)

                # Push to approvals queue (Executor picks this up)
                await self._redis.push_approval(scored_plan)

                # Store in emails:all so the dashboard email list shows live plans
                await self._redis.push_email_record(scored_plan)

                await self._redis.append_activity_log({
                    "timestamp": scored_plan["created_at"],
                    "agent": "Planner",
                    "action": (
                        f"Planned: {scored_plan['action']} "
                        f"(confidence={scored_plan['confidence']}%) "
                        f"| {scored_plan['subject'][:40]}"
                    ),
                    "event_id": event["event_id"],
                })

                print(
                    f"\n[Planner] Plan generated:"
                    f"\n  Action     : {scored_plan['action']}"
                    f"\n  Confidence : {scored_plan['confidence']}%"
                    f"\n  Priority   : {scored_plan['priority']}"
                    f"\n  Reason     : {scored_plan['reason']}"
                    f"\n  Approval?  : {scored_plan['requires_approval']}"
                )

            except Exception as exc:
                err_str = str(exc)
                self.logger.error("planning_failed", event_id=event["event_id"], error=err_str)
                print(f"[Planner] ERROR planning event {event['event_id']}: {exc}")

                # Store a failed record so the dashboard/tests can observe the attempt.
                # This is especially important for 429 rate-limit errors so the event
                # isn't silently swallowed.
                payload = event.get("payload", {})
                failed_record: dict[str, Any] = {
                    "id": str(uuid.uuid4()),
                    "event_id": event["event_id"],
                    "event_type": event.get("type", "unknown"),
                    "subject": payload.get("subject", event.get("summary", ""))[:120],
                    "from_addr": payload.get("from", event.get("source", "")),
                    "snippet": payload.get("snippet", "")[:200],
                    "action": "no_action",
                    "confidence": 0,
                    "priority": "low",
                    "user_response": "llm_failed",
                    "urgency_keywords": event.get("urgency_keywords", []),
                    "reason": f"LLM error: {err_str[:200]}",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "requires_approval": False,
                    "alternatives": [],
                    "explanation": "",
                    "action_args": {},
                    "scoring": {"base": 0, "urgency_mult": 1.0, "history_mult": 1.0},
                }
                try:
                    await self._redis.push_email_record(failed_record)
                    await self._redis.append_activity_log({
                        "timestamp": failed_record["created_at"],
                        "agent": "Planner",
                        "action": f"LLM_FAILED: {failed_record['subject'][:50]} — {err_str[:80]}",
                        "event_id": event["event_id"],
                    })
                except Exception:
                    pass

    # ── LLM Reasoning ─────────────────────────────────────────────────────────

    async def _plan_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Build prompts (fetches calendar for meetings), then LLM or deterministic plan."""
        system_prompt, user_prompt = await self._build_prompts(event)
        if _calendar_free_slot_ready(event):
            return _build_deterministic_meeting_plan(event)

        response = await self._llm.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=800,
        )
        raw = response.choices[0].message.content or ""
        plan = self._parse_llm_response(raw)
        # If calendar already proved free, never trust read_calendar / send_email from LLM.
        if _calendar_free_slot_ready(event):
            return _build_deterministic_meeting_plan(event)
        return plan

    async def _build_prompts(self, event: dict[str, Any]) -> tuple[str, str]:
        """
        Build system + user prompts with:
          - ChromaDB preference context
          - Calendar conflict/free detection for meeting request emails

        Sets event["_calendar_conflict"] (bool | None) as a side-effect so the
        caller can attach skip_call logic after _plan_event() returns.
        """
        # ── User preferences from ChromaDB ────────────────────────────────────
        preferences: list[dict] = []
        try:
            preferences = await self._memory.query_preferences(
                event.get("summary", event.get("type", "event")), n_results=3
            )
        except Exception:
            pass

        pref_context = ""
        if preferences:
            pref_context = "\n\nUser preferences (from memory):\n" + "\n".join(
                f"  - {p['document']}" for p in preferences
            )

        # ── Meeting-request calendar enrichment ───────────────────────────────
        calendar_context = ""
        meeting_rules = ""
        payload = event.get("payload", {})
        is_meeting = event.get("is_meeting_request") or (
            event.get("type") == "email" and is_meeting_request(payload)
        )

        if is_meeting:
            event.pop("_auto_book_free_slot", None)
            event.pop("_meeting_start_iso", None)
            event.pop("_meeting_end_iso", None)
            event["_calendar_conflict"] = None
            try:
                cal_raw = await self.call_tool("read_calendar", {"days_ahead": 14})
                cal_events = cal_raw if isinstance(cal_raw, list) else []
                busy = parse_busy_slots(cal_events)
                free = find_free_slots(busy, datetime.now(timezone.utc))

                # 1) Explicit date + range: "April 10 1pm to 2pm" (uses meeting_parse_timezone)
                window = parse_meeting_window(payload)
                # 2) Fallback: "tomorrow at 3pm" etc. (IST-relative)
                if window is None:
                    requested_dt = detect_requested_time(payload, now=datetime.now(_IST))
                    if requested_dt is not None:
                        s_utc = requested_dt.astimezone(timezone.utc)
                        e_utc = s_utc + timedelta(hours=1)
                        window = (s_utc, e_utc)

                if window is None:
                    event["_calendar_conflict"] = None
                    event["_requested_dt"] = None
                    conflict_bool = None
                    status_hint = (
                        "CALENDAR_STATUS: UNKNOWN — no specific date/time detected in the email."
                    )
                    recommended = "send_email (ask for preferred time / suggest free slots above)"
                    schedule_hint = ""
                else:
                    start_utc, end_utc = window
                    conflict_bool = has_time_conflict(start_utc, end_utc, busy)
                    event["_calendar_conflict"] = conflict_bool
                    event["_requested_dt"] = start_utc.astimezone(_IST).strftime(
                        "%a %d %b, %I:%M %p IST"
                    )
                    event["_meeting_start_iso"] = start_utc.isoformat()
                    event["_meeting_end_iso"] = end_utc.isoformat()
                    if not conflict_bool:
                        event["_auto_book_free_slot"] = True

                    if conflict_bool:
                        status_hint = (
                            f"CALENDAR_STATUS: CONFLICT — "
                            f"{event['_requested_dt']} overlaps an existing event."
                        )
                        recommended = (
                            "send_email (reply with conflict message + suggest free slots above)"
                        )
                        schedule_hint = ""
                        # Store human-readable free slot hints for the voice call script
                        event["_free_slot_hints"] = [
                            slot.astimezone(_IST).strftime("%A %I:%M %p")
                            for slot in free[:2]
                        ]
                    else:
                        status_hint = (
                            f"CALENDAR_STATUS: FREE — "
                            f"{event['_requested_dt']} is available (system will auto-book)."
                        )
                        recommended = "create_event (handled by system — do not use read_calendar)"
                        schedule_hint = (
                            f"\n  create_event args (reference):"
                            f"\n    start_datetime = \"{event['_meeting_start_iso']}\""
                            f"\n    end_datetime   = \"{event['_meeting_end_iso']}\""
                        )

                calendar_context = "\n\n" + format_calendar_for_prompt(cal_events, free)

                meeting_rules = f"""
MEETING / SCHEDULING REQUEST — follow these rules EXACTLY:
{status_hint}
Recommended action: {recommended}{schedule_hint}

→ If CALENDAR_STATUS = FREE (and this row is shown): the system auto-books — you should still output create_event with the same ISO times if asked, but normally this path skips the LLM.

→ If CALENDAR_STATUS = CONFLICT:
    - action = "send_email", confidence = 92, priority = "high"
    - action_args.to/subject/body: conflict explanation + 2 FREE slots from calendar context

→ If CALENDAR_STATUS = UNKNOWN:
    - action = "send_email" — ask for a specific date/time or offer FREE slots above"""

                print(
                    f"[Planner] Meeting — {status_hint} | "
                    f"{len(cal_events)} cal event(s), {len(free)} free slot(s)"
                )
            except Exception as exc:
                calendar_context = "\n\n[Calendar unavailable — skip conflict check]"
                event["_calendar_conflict"] = None
                event.pop("_auto_book_free_slot", None)
                print(f"[Planner] Calendar fetch failed: {exc}")

        # ── Assemble prompts ──────────────────────────────────────────────────
        system_prompt = f"""You are an intelligent personal assistant AI that decides what actions to take on behalf of the user.

Available MCP tools:{AVAILABLE_TOOLS}

User context:{pref_context}{meeting_rules}

Rules:
- ALWAYS respond with ONLY valid JSON matching this exact schema (no markdown, no extra text):
{PLAN_SCHEMA}
- confidence must be 0-100 (integer)
- priority: "high" for urgent/deadline/critical emails, "medium" for requests needing action, "low" for newsletters/promos
- action must be one of the available tool names or "no_action"
- provide exactly 2 alternatives (different from the main action)
- action_args must contain the correct arguments for the chosen tool
- requires_approval: true if confidence < 90 or action has side effects (sending emails, creating events)"""

        meeting_hint = "\n*** MEETING/SCHEDULING REQUEST DETECTED ***" if is_meeting else ""
        user_prompt = f"""Analyze this event and decide the best action:

Event type: {event.get('type', 'unknown')}
Summary: {event.get('summary', '')}
Urgency keywords found: {event.get('urgency_keywords', [])}
Timestamp: {event.get('timestamp', '')}
{meeting_hint}
Event details:
{json.dumps(event.get('payload', {}), indent=2, default=str)}{calendar_context}

What is the best action to take? Respond with JSON only."""

        return system_prompt, user_prompt

    def _parse_llm_response(self, raw: str) -> dict[str, Any]:
        """
        Extract JSON from LLM response, handling markdown code fences.
        Validates required keys and provides defaults for missing ones.
        """
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)

        try:
            plan = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM returned invalid JSON: {exc}\nRaw: {raw[:300]}")

        plan.setdefault("action", "no_action")
        plan.setdefault("confidence", 50)
        plan.setdefault("priority", "")
        plan.setdefault("reason", "No reason provided")
        plan.setdefault("requires_approval", True)
        plan.setdefault("alternatives", [])
        plan.setdefault("explanation", "")
        plan.setdefault("action_args", {})

        plan["confidence"] = max(0, min(100, int(plan["confidence"])))

        return plan

    # ── Decision Scoring ──────────────────────────────────────────────────────

    async def _score_plan(
        self, plan: dict[str, Any], event: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Apply urgency + historical approval adjustments to base LLM confidence.
        adjusted = base * urgency_multiplier * history_multiplier  (clamped 0-100)
        """
        base = plan["confidence"]
        urgency_keywords = event.get("urgency_keywords", [])

        # Urgency multiplier: +10% per keyword, max +30%
        keyword_count = len(urgency_keywords)
        urgency_mult = 1.0 + min(keyword_count * 0.1, 0.3)

        # History multiplier: 0.9 (poor history) to 1.1 (great history)
        action_type = plan.get("action", "unknown")
        approval_rate = 0.5
        try:
            approval_rate = await self._memory.get_approval_rate(action_type)
        except Exception:
            pass
        history_mult = 0.9 + (approval_rate * 0.2)  # range: 0.9 – 1.1

        adjusted = int(base * urgency_mult * history_mult)
        adjusted = max(0, min(100, adjusted))

        plan["confidence"] = adjusted
        # Use "scoring" key with short names to match the dashboard template
        plan["scoring"] = {
            "base": base,
            "urgency_mult": round(urgency_mult, 2),
            "history_mult": round(history_mult, 2),
            "approval_rate": round(approval_rate, 2),
        }

        if adjusted > 90:
            plan["requires_approval"] = False
        elif adjusted >= 70:
            plan["requires_approval"] = True
        else:
            plan["requires_approval"] = True

        return plan
