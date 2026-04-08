"""
ExecutorAgent — The Hands of the system.
Reads action plans from Redis approvals:pending (blocking) and routes them:
  confidence > 90   → auto-execute via MCP + Twilio notification
  confidence 70-90  → push to FastAPI dashboard for human approval
  confidence < 70   → silent discard (logged only)
"""
from datetime import datetime, timezone
from typing import Any

from agents.base_agent import BaseAgent
from memory.redis_client import RedisClient
from memory.chroma_memory import ChromaMemory

# Maps planner action names → MCP tool names (they match, but kept explicit for clarity)
ACTION_TOOL_MAP: dict[str, str] = {
    "send_email": "send_email",
    "read_emails": "read_emails",
    "create_event": "create_event",
    "read_calendar": "read_calendar",
    "list_files": "list_files",
    "move_file": "move_file",
}


class ExecutorAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("Executor")
        self._redis: RedisClient | None = None
        self._memory: ChromaMemory | None = None
        self._notifier = None

    async def run(self) -> None:
        """Block on approvals:pending, route each plan by confidence."""
        from utils.notifier import Notifier
        self._redis = RedisClient.get_instance()
        self._memory = ChromaMemory.from_settings()
        self._notifier = Notifier()

        self.logger.info("executor_started")
        print("[Executor] Ready — routing by confidence threshold")

        while True:
            plan = await self._redis.pop_approval(timeout=0)
            if plan is None:
                continue

            confidence = plan.get("confidence", 0)
            plan_id = plan.get("id", "unknown")
            approved_override = plan.get("approved_override", False)

            self.logger.info(
                "routing_plan",
                plan_id=plan_id,
                action=plan.get("action"),
                confidence=confidence,
                override=approved_override,
            )

            if confidence > 90 or approved_override:
                await self._auto_execute(plan)
            elif confidence >= 70:
                await self._push_to_dashboard(plan)
            else:
                await self._silent_discard(plan)

    # ── Routing Methods ───────────────────────────────────────────────────────

    async def _auto_execute(self, plan: dict[str, Any]) -> None:
        """Execute the MCP action immediately, then notify via Twilio/simulation."""
        action = plan.get("action", "no_action")
        action_args = plan.get("action_args", {})

        print(f"\n[Executor] AUTO-EXECUTE: {action}")
        print(f"           Reason: {plan.get('reason', '')}")

        result = {"status": "skipped", "detail": "no_action"}

        if action in ACTION_TOOL_MAP and action != "no_action":
            try:
                result = await self.call_tool(ACTION_TOOL_MAP[action], action_args)
                self.logger.info("action_executed", action=action, result=str(result)[:200])
                print(f"[Executor] Result: {result}")
            except Exception as exc:
                result = {"status": "error", "error": str(exc)}
                self.logger.error("action_failed", action=action, error=str(exc))
                print(f"[Executor] ERROR executing {action}: {exc}")

        # Notify user (Twilio or simulation)
        await self._notifier.notify(plan, result)

        # Record outcome in ChromaDB for future learning
        try:
            await self._memory.record_outcome(plan, result, approved=True, executor="auto")
        except Exception:
            pass

        # Update user_response in emails:all
        plan_id = plan.get("id")
        if plan_id:
            resp = "approved" if plan.get("approved_override") else "auto_executed"
            await self._redis.update_email_response(plan_id, resp)

        # Append to activity log
        await self._redis.append_activity_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "Executor",
            "action": f"AUTO: {action}",
            "confidence": plan.get("confidence"),
            "result": str(result)[:200],
            "plan_id": plan_id,
        })

    async def _push_to_dashboard(self, plan: dict[str, Any]) -> None:
        """Push to dashboard:pending for human approval via FastAPI."""
        await self._redis.push_dashboard_item(plan)

        self.logger.info(
            "pushed_to_dashboard",
            plan_id=plan.get("id"),
            action=plan.get("action"),
            confidence=plan.get("confidence"),
        )
        print(
            f"\n[Executor] DASHBOARD APPROVAL REQUIRED"
            f"\n  Action    : {plan.get('action')}"
            f"\n  Confidence: {plan.get('confidence')}%"
            f"\n  Reason    : {plan.get('reason')}"
            f"\n  → Open http://localhost:8080 to approve/reject"
        )

        await self._redis.append_activity_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "Executor",
            "action": f"PENDING: {plan.get('action')} (awaiting dashboard approval)",
            "confidence": plan.get("confidence"),
            "plan_id": plan.get("id"),
        })

    async def _silent_discard(self, plan: dict[str, Any]) -> None:
        """Log and discard low-confidence plans."""
        self.logger.info(
            "silent_discard",
            plan_id=plan.get("id"),
            action=plan.get("action"),
            confidence=plan.get("confidence"),
            reason=plan.get("reason"),
        )
        print(
            f"[Executor] SILENT DISCARD: {plan.get('action')} "
            f"(confidence={plan.get('confidence')}% < 70)"
        )

        # Record as rejected in ChromaDB (informs future confidence scoring)
        try:
            await self._memory.record_outcome(
                plan, {}, approved=False, executor="rejected"
            )
        except Exception:
            pass

        await self._redis.append_activity_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "Executor",
            "action": f"DISCARDED: {plan.get('action')} (confidence too low)",
            "confidence": plan.get("confidence"),
            "plan_id": plan.get("id"),
        })
