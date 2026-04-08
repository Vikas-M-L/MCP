"""
Approval endpoints — human-in-the-loop plan management.
  GET  /api/pending          → list plans awaiting human decision
  GET  /api/emails           → all email plans (every confidence level)
  GET  /api/feed             → activity log
  POST /api/approve/{id}     → approve and re-queue for Executor
  POST /api/reject/{id}      → reject and record in ChromaDB
  POST /api/poll/now         → trigger immediate Observer poll
"""
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from api.ws import manager

router = APIRouter()


@router.get("/api/pending")
async def get_pending() -> list[dict]:
    from memory.redis_client import RedisClient
    return await RedisClient.get_instance().get_dashboard_items()


@router.get("/api/emails")
async def get_all_emails() -> list[dict]:
    """All email plans sorted newest-first."""
    from memory.redis_client import RedisClient
    return await RedisClient.get_instance().get_all_emails()


@router.get("/api/feed")
async def activity_feed(limit: int = 50) -> list[dict]:
    """Recent activity log entries (newest first)."""
    from memory.redis_client import RedisClient
    return await RedisClient.get_instance().get_activity_log(limit=limit)


@router.post("/api/approve/{item_id}")
async def approve_action(item_id: str) -> dict:
    """Approve a pending action — re-queues to Executor for immediate execution."""
    from memory.redis_client import RedisClient
    redis = RedisClient.get_instance()
    item = await redis.get_dashboard_item(item_id)
    if not item:
        return JSONResponse({"error": "Item not found"}, status_code=404)

    item["approved_override"] = True
    await redis.push_approval(item)
    await redis.remove_dashboard_item(item_id)
    await redis.update_email_response(item_id, "approved")
    await redis.append_activity_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "Dashboard",
        "action": f"APPROVED: {item.get('action')} | {item.get('subject','')[:40]} (user decision)",
        "plan_id": item_id,
    })

    await manager.broadcast({"type": "refresh"})
    return {"status": "approved", "plan_id": item_id}


@router.post("/api/reject/{item_id}")
async def reject_action(item_id: str) -> dict:
    """Reject a pending action and record it in ChromaDB."""
    from memory.redis_client import RedisClient
    from memory.chroma_memory import ChromaMemory
    redis = RedisClient.get_instance()
    item = await redis.get_dashboard_item(item_id)
    if not item:
        return JSONResponse({"error": "Item not found"}, status_code=404)

    await redis.remove_dashboard_item(item_id)
    await redis.update_email_response(item_id, "rejected")

    try:
        memory = ChromaMemory.from_settings()
        await memory.record_outcome(item, {}, approved=False, executor="dashboard")
    except Exception:
        pass

    await redis.append_activity_log({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": "Dashboard",
        "action": f"REJECTED: {item.get('action')} | {item.get('subject','')[:40]} (user decision)",
        "plan_id": item_id,
    })

    await manager.broadcast({"type": "refresh"})
    return {"status": "rejected", "plan_id": item_id}


@router.post("/api/poll/now")
async def poll_now() -> dict:
    """
    Wake the Observer immediately instead of waiting for the next scheduled cycle.
    Called by the dashboard on page-load/refresh so the user always sees fresh data.
    """
    try:
        from agents.observer_agent import trigger_immediate_poll
        await trigger_immediate_poll()
        return {"status": "triggered", "message": "Observer poll triggered — check back in a few seconds"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}
