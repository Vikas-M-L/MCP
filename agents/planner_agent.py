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
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI

from agents.base_agent import BaseAgent
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


def _build_call_text(plan: dict[str, Any], event: dict[str, Any]) -> str:
    """Build the spoken Twilio call script for this plan."""
    priority = plan.get("priority", "medium")
    action = plan.get("action", "no_action").replace("_", " ")
    confidence = plan.get("confidence", 0)
    reason = plan.get("reason", "")[:120]
    from_addr = event["payload"].get("from", event.get("source", "unknown"))[:60]
    subject = event["payload"].get("subject", event.get("summary", ""))[:80]

    if confidence > 90:
        status_line = "Action has been automatically executed."
    elif confidence >= 70:
        status_line = "Please review and approve or reject on your dashboard."
    else:
        status_line = "Action was silently discarded due to low confidence."

    return (
        f"Hello! This is your Personal OS Agent. "
        f"I detected a {priority} priority event "
        f"from {from_addr}. "
        f"Subject: {subject}. "
        f"Confidence level: {confidence} percent. "
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

                # Build spoken call script (used by Twilio notifier)
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
        """Build prompt, call OpenRouter LLM, parse strict JSON response."""
        system_prompt, user_prompt = await self._build_prompts(event)

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
        return self._parse_llm_response(raw)

    async def _build_prompts(self, event: dict[str, Any]) -> tuple[str, str]:
        """Build system + user prompt with ChromaDB preference context injected."""
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

        system_prompt = f"""You are an intelligent personal assistant AI that decides what actions to take on behalf of the user.

Available MCP tools:{AVAILABLE_TOOLS}

User context:{pref_context}

Rules:
- ALWAYS respond with ONLY valid JSON matching this exact schema (no markdown, no extra text):
{PLAN_SCHEMA}
- confidence must be 0-100 (integer)
- priority: "high" for urgent/deadline/critical emails, "medium" for requests needing action, "low" for newsletters/promos
- action must be one of the available tool names or "no_action"
- provide exactly 2 alternatives (different from the main action)
- action_args must contain the correct arguments for the chosen tool
- requires_approval: true if confidence < 90 or action has side effects (sending emails, creating events)"""

        user_prompt = f"""Analyze this event and decide the best action:

Event type: {event.get('type', 'unknown')}
Summary: {event.get('summary', '')}
Urgency keywords found: {event.get('urgency_keywords', [])}
Timestamp: {event.get('timestamp', '')}

Event details:
{json.dumps(event.get('payload', {}), indent=2, default=str)}

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
