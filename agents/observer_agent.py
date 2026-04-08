"""
ObserverAgent — The Eyes of the system.
Polls Gmail, Google Calendar, and the local filesystem every 60 seconds via MCP.
Normalizes raw data into structured events and pushes new (deduplicated) events
to Redis events:queue for the PlannerAgent to process.
"""
import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

from agents.base_agent import BaseAgent
from memory.redis_client import RedisClient

# Keywords that increase urgency scoring in the PlannerAgent
URGENCY_KEYWORDS = [
    "urgent", "asap", "deadline", "immediately", "critical",
    "overdue", "emergency", "important", "action required", "must",
]


class ObserverAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("Observer")

    async def start(self) -> None:
        """Override BaseAgent.start() — Observer manages its own MCP connection per cycle."""
        while True:
            try:
                await self.run()
            except asyncio.CancelledError:
                await self.disconnect_mcp()
                raise
            except Exception as exc:
                self.logger.error("observer_crashed", error=str(exc))
                await asyncio.sleep(5)

    async def run(self) -> None:
        """
        Poll all data sources every OBSERVER_POLL_INTERVAL seconds.

        The MCP SSE post_writer is prone to dropping after the first few
        requests when the server runs in a daemon thread (httpx ReadError on
        an empty keep-alive body).  To work around this, we reconnect the MCP
        session before every poll cycle instead of reusing a long-lived session.
        """
        from config.settings import get_settings
        redis = RedisClient.get_instance()
        interval = get_settings().observer_poll_interval

        self.logger.info("observer_started", poll_interval=interval)

        while True:
            # Fresh MCP connection per cycle — avoids SSE post_writer drop
            try:
                await self.disconnect_mcp()
            except Exception:
                pass
            try:
                await self.connect_mcp()
            except Exception as exc:
                self.logger.error("observer_mcp_reconnect_failed", error=str(exc))
                await asyncio.sleep(5)
                continue

            try:
                events = await self._poll_all_sources()
                new_count = 0
                for event in events:
                    if not await redis.is_event_seen(event["event_id"]):
                        await redis.push_event(event)
                        await redis.mark_event_seen(event["event_id"])
                        new_count += 1
                        self.logger.info(
                            "event_detected",
                            event_id=event["event_id"],
                            type=event["type"],
                            source=event["source"],
                        )

                if new_count:
                    print(f"\n[Observer] Detected {new_count} new event(s) — pushed to queue")
                else:
                    print(f"[Observer] Cycle complete — no new events")
                    self.logger.debug("observer_cycle_no_events")

            except Exception as exc:
                self.logger.error("observer_poll_error", error=str(exc))

            await asyncio.sleep(interval)

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _poll_all_sources(self) -> list[dict[str, Any]]:
        """Gather events from all three MCP tools concurrently.

        Raises RuntimeError when every source fails — that signals a broken
        MCP session and tells run() to propagate to start() for reconnect.
        """
        email_task = asyncio.create_task(self._poll_emails())
        calendar_task = asyncio.create_task(self._poll_calendar())
        file_task = asyncio.create_task(self._poll_files())

        results = await asyncio.gather(
            email_task, calendar_task, file_task, return_exceptions=True
        )
        events: list[dict] = []
        failures = 0
        for result in results:
            if isinstance(result, list):
                events.extend(result)
            elif isinstance(result, Exception):
                failures += 1
                self.logger.warning("source_poll_failed", error=str(result))

        if failures == len(results):
            raise RuntimeError(
                "All MCP sources failed — session likely broken; triggering reconnect"
            )
        return events

    async def _poll_emails(self) -> list[dict[str, Any]]:
        """Fetch recent unread emails and normalize them as events."""
        emails = await self.call_tool("read_emails", {"max_results": 20, "query": "is:unread"})
        if not isinstance(emails, list):
            return []
        return [self._normalize_email(e) for e in emails]

    async def _poll_calendar(self) -> list[dict[str, Any]]:
        """Fetch upcoming calendar events and normalize as events."""
        cal_events = await self.call_tool("read_calendar", {"days_ahead": 3})
        if not isinstance(cal_events, list):
            return []
        return [self._normalize_calendar(e) for e in cal_events]

    async def _poll_files(self) -> list[dict[str, Any]]:
        """List files in the sandbox root and emit an event if crowded (>20 files)."""
        files = await self.call_tool("list_files", {"directory": "."})
        if not isinstance(files, list):
            return []
        file_count = len([f for f in files if not f.get("is_dir", False)])
        if file_count > 20:
            return [self._normalize_file_overflow(files, file_count)]
        return []

    # ── Normalizers ───────────────────────────────────────────────────────────

    def _normalize_email(self, email: dict) -> dict[str, Any]:
        """Convert raw Gmail email dict to normalized agent event."""
        raw_text = f"{email.get('from', '')} {email.get('subject', '')} {email.get('snippet', '')}".lower()
        keywords = [kw for kw in URGENCY_KEYWORDS if kw in raw_text]
        event_id = hashlib.sha256(f"email:{email.get('id', '')}".encode()).hexdigest()[:16]
        return {
            "event_id": event_id,
            "type": "email",
            "source": "gmail",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": email,
            "urgency_keywords": keywords,
            "summary": f"Email from {email.get('from', 'unknown')}: {email.get('subject', '')}",
        }

    def _normalize_calendar(self, event: dict) -> dict[str, Any]:
        """Convert raw Calendar event dict to normalized agent event."""
        raw_text = f"{event.get('summary', '')} {event.get('description', '')}".lower()
        keywords = [kw for kw in URGENCY_KEYWORDS if kw in raw_text]
        event_id = hashlib.sha256(f"cal:{event.get('id', '')}".encode()).hexdigest()[:16]
        return {
            "event_id": event_id,
            "type": "calendar",
            "source": "google_calendar",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": event,
            "urgency_keywords": keywords,
            "summary": f"Calendar: {event.get('summary', '(no title)')} at {event.get('start', '')}",
        }

    def _normalize_file_overflow(
        self, files: list[dict], count: int
    ) -> dict[str, Any]:
        """Emit a single event when the sandbox folder is overflowing with files."""
        event_id = hashlib.sha256(f"files:overflow:{count}".encode()).hexdigest()[:16]
        return {
            "event_id": event_id,
            "type": "filesystem",
            "source": "local_fs",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": {"file_count": count, "files": files[:10]},  # sample first 10
            "urgency_keywords": [],
            "summary": f"Sandbox folder has {count} unsorted files",
        }
