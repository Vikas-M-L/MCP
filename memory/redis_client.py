"""
Async Redis client — singleton wrapper around redis.asyncio.
Provides typed helper methods for all queue operations used by the agents.

Key schema:
  events:queue       LIST  — Observer RPUSH, Planner BLPOP
  approvals:pending  LIST  — Planner RPUSH, Executor BLPOP
  dashboard:pending  HASH  — field=plan_id, value=JSON plan
  emails:all         HASH  — field=plan_id, ALL email plans (every confidence level)
  seen:event_ids     SET   — deduplication, TTL=86400s
  activity:log       LIST  — LTRIM to 500 entries
"""
import json
from typing import Any

import redis.asyncio as aioredis


class RedisClient:
    _instance: "RedisClient | None" = None

    def __init__(self, url: str) -> None:
        self._redis: aioredis.Redis = aioredis.from_url(
            url, encoding="utf-8", decode_responses=True
        )

    # ── Singleton ─────────────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "RedisClient":
        if cls._instance is None:
            from config.settings import get_settings
            cls._instance = cls(get_settings().redis_url)
        return cls._instance

    # ── Event queue (Observer → Planner) ─────────────────────────────────────

    async def push_event(self, event: dict[str, Any]) -> None:
        """RPUSH a normalized event JSON onto events:queue."""
        await self._redis.rpush("events:queue", json.dumps(event))

    async def pop_event(self, timeout: int = 0) -> dict[str, Any] | None:
        """
        BLPOP from events:queue.
        Blocks until an item is available (timeout=0 = indefinite).
        Returns None on timeout.
        """
        result = await self._redis.blpop(["events:queue"], timeout=timeout)
        if result:
            _, raw = result
            return json.loads(raw)
        return None

    # ── Approval queue (Planner → Executor) ──────────────────────────────────

    async def push_approval(self, plan: dict[str, Any]) -> None:
        """RPUSH a plan JSON onto approvals:pending."""
        await self._redis.rpush("approvals:pending", json.dumps(plan))

    async def pop_approval(self, timeout: int = 0) -> dict[str, Any] | None:
        """
        BLPOP from approvals:pending.
        Returns None on timeout.
        """
        result = await self._redis.blpop(["approvals:pending"], timeout=timeout)
        if result:
            _, raw = result
            return json.loads(raw)
        return None

    # ── Dashboard pending (Executor → FastAPI → Executor) ────────────────────

    async def push_dashboard_item(self, item: dict[str, Any]) -> None:
        """HSET a pending approval into dashboard:pending hash (field = plan id)."""
        await self._redis.hset("dashboard:pending", item["id"], json.dumps(item))

    async def get_dashboard_items(self) -> list[dict[str, Any]]:
        """HGETALL dashboard:pending → list of plan dicts."""
        raw = await self._redis.hgetall("dashboard:pending")
        return [json.loads(v) for v in raw.values()]

    async def get_dashboard_item(self, item_id: str) -> dict[str, Any] | None:
        """HGET a single item from dashboard:pending."""
        raw = await self._redis.hget("dashboard:pending", item_id)
        return json.loads(raw) if raw else None

    async def remove_dashboard_item(self, item_id: str) -> None:
        """HDEL an item from dashboard:pending."""
        await self._redis.hdel("dashboard:pending", item_id)

    # ── Email records — all emails regardless of confidence (Dashboard view) ──

    async def push_email_record(self, plan: dict[str, Any]) -> None:
        """HSET a plan into emails:all hash (field = plan id). Stores every email."""
        await self._redis.hset("emails:all", plan["id"], json.dumps(plan))

    async def get_all_emails(self) -> list[dict[str, Any]]:
        """HGETALL emails:all → list sorted newest-first by created_at."""
        raw = await self._redis.hgetall("emails:all")
        records = [json.loads(v) for v in raw.values()]
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return records

    async def update_email_response(self, plan_id: str, response: str) -> None:
        """Update the user_response field of an existing emails:all record."""
        raw = await self._redis.hget("emails:all", plan_id)
        if raw:
            record = json.loads(raw)
            record["user_response"] = response
            await self._redis.hset("emails:all", plan_id, json.dumps(record))

    # ── Deduplication (Observer) ──────────────────────────────────────────────

    async def mark_event_seen(self, event_id: str) -> None:
        """
        Record event_id as seen with a 24-hour individual TTL.

        Uses per-event SETEX keys (seen_event:<id>) instead of a shared Set.
        The shared-Set approach called EXPIRE on the entire key every time any
        event was added, resetting the 24-hour clock for *all* existing events
        on each new one — events would never expire as long as activity continued.
        """
        await self._redis.setex(f"seen_event:{event_id}", 86400, "1")

    async def is_event_seen(self, event_id: str) -> bool:
        """Check if event_id has been seen within the last 24 hours."""
        return bool(await self._redis.exists(f"seen_event:{event_id}"))

    async def clear_event_seen(self, event_id: str) -> None:
        """Remove a single event from the dedup cache (used by demo seed scripts)."""
        await self._redis.delete(f"seen_event:{event_id}")

    # ── Activity log (all agents write, dashboard reads) ─────────────────────

    async def append_activity_log(self, entry: dict[str, Any]) -> None:
        """
        RPUSH an activity log entry, then LTRIM to keep only the last 500.
        """
        pipe = self._redis.pipeline()
        pipe.rpush("activity:log", json.dumps(entry))
        pipe.ltrim("activity:log", -500, -1)
        await pipe.execute()

    async def get_activity_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """LRANGE activity:log, most recent N entries."""
        raw = await self._redis.lrange("activity:log", -limit, -1)
        return [json.loads(r) for r in reversed(raw)]

    # ── Session state (general K/V) ───────────────────────────────────────────

    async def set_session_state(
        self, key: str, value: dict[str, Any], ttl: int = 3600
    ) -> None:
        await self._redis.setex(f"session:{key}", ttl, json.dumps(value))

    async def get_session_state(self, key: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"session:{key}")
        return json.loads(raw) if raw else None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._redis.aclose()

    async def ping(self) -> bool:
        """Health check — returns True if Redis is reachable."""
        try:
            return await self._redis.ping()
        except Exception:
            return False
