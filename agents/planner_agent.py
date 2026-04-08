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

                # Attach event metadata for executor context
                scored_plan["event_id"] = event["event_id"]
                scored_plan["event_type"] = event.get("type", "unknown")
                scored_plan["id"] = str(uuid.uuid4())
                scored_plan["created_at"] = datetime.now(timezone.utc).isoformat()

                await self._redis.push_approval(scored_plan)
                await self._redis.append_activity_log({
                    "timestamp": scored_plan["created_at"],
                    "agent": "Planner",
                    "action": f"Planned: {scored_plan['action']} (confidence={scored_plan['confidence']})",
                    "event_id": event["event_id"],
                })

                print(
                    f"\n[Planner] Plan generated:"
                    f"\n  Action     : {scored_plan['action']}"
                    f"\n  Confidence : {scored_plan['confidence']}%"
                    f"\n  Reason     : {scored_plan['reason']}"
                    f"\n  Approval?  : {scored_plan['requires_approval']}"
                )

            except Exception as exc:
                self.logger.error("planning_failed", event_id=event["event_id"], error=str(exc))
                print(f"[Planner] ERROR planning event {event['event_id']}: {exc}")

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
        # Semantic search on event summary for relevant user preferences
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
        # Strip markdown fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()

        # Find first { ... } block
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(0)

        try:
            plan = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM returned invalid JSON: {exc}\nRaw: {raw[:300]}")

        # Ensure required keys with safe defaults
        plan.setdefault("action", "no_action")
        plan.setdefault("confidence", 50)
        plan.setdefault("reason", "No reason provided")
        plan.setdefault("requires_approval", True)
        plan.setdefault("alternatives", [])
        plan.setdefault("explanation", "")
        plan.setdefault("action_args", {})

        # Clamp confidence
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
        approval_rate = 0.5  # default neutral
        try:
            approval_rate = await self._memory.get_approval_rate(action_type)
        except Exception:
            pass
        history_mult = 0.9 + (approval_rate * 0.2)  # range: 0.9 – 1.1

        adjusted = int(base * urgency_mult * history_mult)
        adjusted = max(0, min(100, adjusted))

        plan["confidence"] = adjusted
        plan["scoring_details"] = {
            "base_confidence": base,
            "urgency_multiplier": round(urgency_mult, 2),
            "history_multiplier": round(history_mult, 2),
            "approval_rate_history": round(approval_rate, 2),
        }

        # Auto-set requires_approval based on final confidence
        if adjusted > 90:
            plan["requires_approval"] = False
        elif adjusted >= 70:
            plan["requires_approval"] = True
        else:
            plan["requires_approval"] = True  # silent discard, but flag it

        return plan
