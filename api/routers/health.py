"""
Health check endpoint — live status of all backend services.
  GET /api/health  → Redis, LLM, Google, Twilio, ChromaDB, MCP server
"""
import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/health")
async def health_check() -> dict:
    """Live health status of all services."""
    from config.settings import get_settings
    cfg = get_settings()
    checks: dict[str, dict] = {}
    overall = True

    # Redis
    try:
        from memory.redis_client import RedisClient
        redis = RedisClient.get_instance()
        t0 = time.perf_counter()
        ok = await redis.ping()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        eq = await redis._redis.llen("events:queue")
        aq = await redis._redis.llen("approvals:pending")
        dp = await redis._redis.hlen("dashboard:pending")
        checks["redis"] = {
            "status": "ok" if ok else "error",
            "latency_ms": ms,
            "queues": {"events": eq, "approvals": aq, "dashboard_pending": dp},
        }
    except Exception as e:
        checks["redis"] = {"status": "error", "error": str(e)}
        overall = False

    # OpenRouter / Groq LLM
    try:
        if not cfg.openrouter_api_key:
            checks["openrouter"] = {"status": "error", "error": "OPENROUTER_API_KEY not set"}
            overall = False
        else:
            import httpx
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=5.0) as hclient:
                r = await hclient.get(
                    f"{cfg.openrouter_base_url}/models",
                    headers={"Authorization": f"Bearer {cfg.openrouter_api_key}"},
                )
            ms = round((time.perf_counter() - t0) * 1000, 1)
            checks["openrouter"] = (
                {"status": "ok", "model": cfg.openrouter_model, "latency_ms": ms}
                if r.status_code == 200
                else {"status": "error", "error": f"HTTP {r.status_code}"}
            )
    except Exception as e:
        checks["openrouter"] = {"status": "error", "error": str(e)[:100]}
        overall = False

    # Google APIs
    try:
        from pathlib import Path
        if Path(cfg.google_token_path).exists() and Path(cfg.google_credentials_path).exists():
            from mcp_server.google_auth import get_credentials
            from googleapiclient.discovery import build
            creds   = await asyncio.to_thread(get_credentials)
            gmail   = await asyncio.to_thread(build, "gmail", "v1", credentials=creds)
            t0      = time.perf_counter()
            profile = await asyncio.to_thread(lambda: gmail.users().getProfile(userId="me").execute())
            ms      = round((time.perf_counter() - t0) * 1000, 1)
            checks["google"] = {
                "status": "ok",
                "account": profile.get("emailAddress", ""),
                "latency_ms": ms,
            }
        else:
            checks["google"] = {
                "status": "warning",
                "error": "OAuth not completed — run scripts/setup_google_auth.py",
            }
    except Exception as e:
        checks["google"] = {"status": "error", "error": str(e)[:100]}

    # Twilio
    if cfg.twilio_enabled:
        try:
            from twilio.rest import Client
            t0   = time.perf_counter()
            tw   = Client(cfg.twilio_account_sid, cfg.twilio_auth_token)
            acct = await asyncio.to_thread(lambda: tw.api.accounts(cfg.twilio_account_sid).fetch())
            ms   = round((time.perf_counter() - t0) * 1000, 1)
            checks["twilio"] = {
                "status": "ok",
                "account": acct.friendly_name,
                "from": cfg.twilio_from_number,
                "to": cfg.twilio_to_number,
                "latency_ms": ms,
            }
        except Exception as e:
            checks["twilio"] = {"status": "error", "error": str(e)[:100]}
            overall = False
    else:
        checks["twilio"] = {"status": "simulation_mode", "note": "TWILIO_* vars not set"}

    # ChromaDB
    try:
        import chromadb
        t0   = time.perf_counter()
        client = await asyncio.to_thread(chromadb.PersistentClient, cfg.chroma_persist_path)
        cols   = await asyncio.to_thread(client.list_collections)
        ms     = round((time.perf_counter() - t0) * 1000, 1)
        checks["chromadb"] = {
            "status": "ok",
            "collections": [c.name for c in cols],
            "latency_ms": ms,
        }
    except Exception as e:
        checks["chromadb"] = {"status": "error", "error": str(e)[:100]}
        overall = False

    # MCP Server — FastMCP returns 404 on root; any HTTP response confirms the app is up.
    mcp_root = f"http://{cfg.mcp_server_host}:{cfg.mcp_server_port}/"
    try:
        import httpx
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=5.0) as hclient:
            await hclient.get(mcp_root)
        ms = round((time.perf_counter() - t0) * 1000, 1)
        checks["mcp_server"] = {"status": "ok", "url": cfg.mcp_sse_url, "latency_ms": ms}
    except Exception:
        checks["mcp_server"] = {"status": "error", "url": cfg.mcp_sse_url, "error": "Not reachable"}

    return {
        "overall": "healthy" if overall else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": checks,
    }
