"""
ExecutorAgent — The Hands of the system.
Reads action plans from Redis approvals:pending (blocking) and routes them:
  confidence > 90   → auto-execute via MCP + Twilio call (high-priority only)
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
        from config.settings import get_settings
        self._redis = RedisClient.get_instance()
        self._memory = ChromaMemory.from_settings()
        self._notifier = Notifier()

        voice_enabled = get_settings().voice_approval_enabled
        self.logger.info("executor_started", voice_approval=voice_enabled)
        print(f"[Executor] Ready — voice approval: {'ON' if voice_enabled else 'OFF'}")

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

            if approved_override:
                # Already approved (via voice or dashboard) — execute immediately
                await self._auto_execute(plan)
            elif confidence > 90 and not voice_enabled:
                # Voice not configured — auto-execute high-confidence plans as before
                await self._auto_execute(plan)
            elif confidence >= 70:
                # Voice configured: ask via call first (all actionable plans)
                # Voice not configured: send to dashboard for manual approval
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

        # Send confirmation reply email when a meeting was auto-scheduled on a free slot.
        # The Planner attaches a pre-built confirmation_email dict to the plan.
        confirmation = plan.get("confirmation_email")
        if confirmation and action == "create_event" and result and "error" not in str(result).lower():
            try:
                conf_result = await self.call_tool("send_email", confirmation)
                self.logger.info(
                    "confirmation_email_sent",
                    to=confirmation.get("to"),
                    subject=confirmation.get("subject"),
                    result=str(conf_result)[:100],
                )
                print(
                    f"[Executor] Confirmation email sent"
                    f"\n  To      : {confirmation.get('to')}"
                    f"\n  Subject : {confirmation.get('subject')}"
                )
            except Exception as exc:
                self.logger.warning("confirmation_email_failed", error=str(exc))
                print(f"[Executor] Confirmation email failed: {exc}")

        # Notify user via Twilio — skipped when the plan explicitly opts out.
        # Meeting on a FREE slot → auto-scheduled + confirmation email sent, no call.
        # Meeting on a BUSY slot → conflict detected → calls the user.
        if not plan.get("skip_call"):
            await self._notifier.notify(plan, result)
        else:
            print(
                f"[Executor] Call skipped — slot was free, "
                f"event scheduled: {plan.get('action_args', {}).get('summary', '')}"
            )

        # Record outcome in ChromaDB for future learning
        try:
            await self._memory.record_outcome(plan, result, approved=True, executor="auto")
        except Exception:
            pass

        plan_id = plan.get("id")
        resp = "approved" if plan.get("approved_override") else "auto_executed"

        # Ensure plan exists in emails:all (Planner already stores it, but plans
        # injected via /api/events/inject bypass the Planner's push_email_record)
        plan["user_response"] = resp
        await self._redis.push_email_record(plan)

        if plan_id:
            await self._redis.update_email_response(plan_id, resp)

        await self._redis.append_activity_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "Executor",
            "action": f"AUTO: {action} | {plan.get('subject','')[:40]}",
            "confidence": plan.get("confidence"),
            "result": str(result)[:200],
            "plan_id": plan_id,
        })

    async def _push_to_dashboard(self, plan: dict[str, Any]) -> None:
        """Push to dashboard:pending for human approval via FastAPI.

        If voice approval is configured (TWILIO_WEBHOOK_BASE_URL set), also
        places an outbound call that speaks the action and captures spoken
        approve / reject / modify from the user.
        """
        plan["user_response"] = "pending"
        await self._redis.push_dashboard_item(plan)

        # Ensure plan is visible in the email list (upsert — push_email_record
        # uses HSET so it's safe to call even if Planner already stored it)
        await self._redis.push_email_record(plan)

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

        # Place a voice approval call if configured (non-blocking — failure is ok)
        if self._notifier:
            try:
                placed = await self._notifier.voice_ask(plan)
                if placed:
                    print(f"[Executor] VOICE CALL dispatched for approval — Plan: {plan.get('id')}")
            except Exception as exc:
                self.logger.warning("voice_ask_skipped", error=str(exc))

        await self._redis.append_activity_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "Executor",
            "action": f"PENDING: {plan.get('action')} | {plan.get('subject','')[:40]} (awaiting approval)",
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

        # Store in emails:all so dashboard shows all processed emails
        plan["user_response"] = "silent_discarded"
        await self._redis.push_email_record(plan)

        await self._redis.append_activity_log({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "Executor",
            "action": f"DISCARDED: {plan.get('action')} | {plan.get('subject','')[:40]} (confidence too low)",
            "confidence": plan.get("confidence"),
            "plan_id": plan.get("id"),
        })
