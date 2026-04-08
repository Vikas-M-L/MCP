"""
System bootstrap helpers — used by main.py to start and probe infrastructure.

Keeping these functions here (instead of inline in main.py) lets main.py stay
a thin 30-line entry-point and makes each concern independently testable.
"""
import asyncio
import threading


def start_mcp_server_thread() -> threading.Thread:
    """
    Run the MCP server in a background daemon thread with its own asyncio event loop.

    Using a thread (not multiprocessing) avoids Windows 'spawn' issues where
    the child process can't inherit the ASGI app object or sys.path correctly.
    The thread is daemon=True so it dies automatically when the main process exits.
    """
    def _run() -> None:
        import uvicorn
        from config.settings import get_settings
        from mcp_server.server import build_mcp_app

        cfg = get_settings()
        app = build_mcp_app()

        print(
            f"[MCP Server] PersonalOS starting on "
            f"http://{cfg.mcp_server_host}:{cfg.mcp_server_port} (SSE)"
        )

        config = uvicorn.Config(
            app.sse_app(),
            host=cfg.mcp_server_host,
            port=cfg.mcp_server_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        except Exception as exc:
            print(f"[MCP Server] ERROR: {exc}")
        finally:
            loop.close()

    t = threading.Thread(target=_run, daemon=True, name="mcp-server")
    t.start()
    return t


async def wait_for_mcp_server(timeout_s: int = 30) -> None:
    """
    Wait until the MCP server is fully serving HTTP requests.

    An HTTP GET probe is used instead of a raw TCP probe because the TCP port
    can be open before uvicorn has finished registering all ASGI routes.  When
    agents connect too early the MCP SSE client's internal post_writer task
    crashes immediately (httpx RemoteProtocolError with empty message), causing
    every tool call to fail silently for the rest of the session.

    Any HTTP response — including a 404 from an unknown path — confirms that
    the ASGI app is routing requests and is safe to connect to.
    """
    import httpx
    from config.settings import get_settings

    cfg = get_settings()
    url = f"http://{cfg.mcp_server_host}:{cfg.mcp_server_port}/"

    print(f"[Bootstrap] Waiting for MCP server at {url} ...")
    async with httpx.AsyncClient() as client:
        for _ in range(timeout_s * 2):
            try:
                await client.get(url, timeout=1.0)
                print("[Bootstrap] MCP server is ready")
                return
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
            await asyncio.sleep(0.5)

    raise TimeoutError(f"MCP server did not become ready within {timeout_s}s")


async def check_redis() -> None:
    """Verify Redis is reachable before starting agents."""
    from memory.redis_client import RedisClient

    redis = RedisClient.get_instance()
    if not await redis.ping():
        raise ConnectionError(
            "Cannot connect to Redis.\n"
            "Start it with:  redis-server\n"
            "Or via Docker:  docker run -p 6379:6379 redis"
        )
    print("[Bootstrap] Redis connection OK")


async def run(args) -> None:
    """Main coroutine — initialises everything and runs all agents concurrently."""
    from pathlib import Path

    from utils.logger import setup_logging
    from config.settings import get_settings

    Path("logs").mkdir(exist_ok=True)
    Path("sandbox").mkdir(exist_ok=True)

    setup_logging("logs/agent.log")
    cfg = get_settings()

    print("=" * 60)
    print("  PersonalOS Agent  —  SOLARIS X Hackathon 2026")
    print("=" * 60)
    print(f"  LLM Model  : {cfg.openrouter_model}")
    print(f"  Redis      : {cfg.redis_url}")
    print(f"  MCP Server : http://{cfg.mcp_server_host}:{cfg.mcp_server_port}")
    print(f"  Dashboard  : http://localhost:{cfg.dashboard_port}")
    print(f"  Twilio     : {'ENABLED' if cfg.twilio_enabled else 'SIMULATION MODE'}")
    print(f"  Poll mode  : {'DISABLED (--skip-poll)' if args.skip_poll else f'every {cfg.observer_poll_interval}s'}")
    print("=" * 60)

    await check_redis()

    if not args.no_mcp:
        await wait_for_mcp_server()

    from agents.observer_agent import ObserverAgent
    from agents.planner_agent import PlannerAgent
    from agents.executor_agent import ExecutorAgent
    from api.app import app as dashboard_app
    import uvicorn

    tasks = []

    if not args.skip_poll and not args.no_mcp:
        observer = ObserverAgent()
        tasks.append(asyncio.create_task(observer.start(), name="observer"))

    planner  = PlannerAgent()
    executor = ExecutorAgent()
    tasks.append(asyncio.create_task(planner.start(),  name="planner"))
    tasks.append(asyncio.create_task(executor.start(), name="executor"))

    dashboard_config = uvicorn.Config(
        dashboard_app,
        host="0.0.0.0",
        port=cfg.dashboard_port,
        log_level="warning",
    )
    dashboard_server = uvicorn.Server(dashboard_config)
    tasks.append(asyncio.create_task(dashboard_server.serve(), name="dashboard"))

    print(f"\n[Bootstrap] All agents started. Dashboard: http://localhost:{cfg.dashboard_port}")
    if args.skip_poll:
        print("[Bootstrap] Tip: Run  python tests/fixtures/seed_events.py  to inject demo events")
    print("[Bootstrap] Press Ctrl+C to stop\n")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        print("\n[Bootstrap] Shutting down...")
