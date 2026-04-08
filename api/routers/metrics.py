"""
Metrics endpoint — aggregate stats over all processed plans.
  GET /api/metrics  → priority/response breakdowns, confidence histogram, queue depths
"""
from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/metrics")
async def get_metrics() -> dict:
    """Aggregate stats over all plans in emails:all."""
    from memory.redis_client import RedisClient
    redis = RedisClient.get_instance()
    emails = await redis.get_all_emails()

    total = len(emails)
    by_priority: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    by_response: dict[str, int] = {
        "auto_executed": 0, "approved": 0,
        "pending": 0, "rejected": 0, "silent_discarded": 0,
    }
    conf_dist: dict[str, int] = {"0-30": 0, "31-60": 0, "61-80": 0, "81-100": 0}
    conf_sum = 0

    for e in emails:
        pr = e.get("priority", "low")
        if pr in by_priority:
            by_priority[pr] += 1
        resp = e.get("user_response", "pending")
        if resp in by_response:
            by_response[resp] += 1
        c = int(e.get("confidence") or 0)
        conf_sum += c
        if c <= 30:
            conf_dist["0-30"] += 1
        elif c <= 60:
            conf_dist["31-60"] += 1
        elif c <= 80:
            conf_dist["61-80"] += 1
        else:
            conf_dist["81-100"] += 1

    avg_conf = round(conf_sum / total, 1) if total else 0.0

    eq = await redis._redis.llen("events:queue")
    aq = await redis._redis.llen("approvals:pending")
    dp = await redis._redis.hlen("dashboard:pending")

    return {
        "total": total,
        "by_priority": by_priority,
        "by_response": by_response,
        "avg_confidence": avg_conf,
        "confidence_distribution": conf_dist,
        "queue_depths": {"events": eq, "approvals": aq, "dashboard_pending": dp},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
