"""
PersonalOS Agent — Main Entry Point
====================================
Starts the complete multi-agent system:
  1. MCP Tool Server  (daemon thread, port 8000, SSE)
  2. Observer Agent   (asyncio task — polls every 60s)
  3. Planner Agent    (asyncio task — LLM reasoning)
  4. Executor Agent   (asyncio task — confidence routing)
  5. FastAPI Dashboard (asyncio task — port 8080)

Run:
  python main.py              # full system (requires Google OAuth + Redis)
  python main.py --skip-poll  # disable Observer polling (use seed_events.py instead)
  python main.py --no-mcp     # skip MCP server (for unit testing agents in isolation)

Demo flow:
  1. python tests/seed_events.py   # pre-load 3 demo events into Redis
  2. python main.py --skip-poll    # Planner + Executor process seeded events
"""
import argparse
import asyncio
import threading
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PersonalOS Multi-Agent System")
    parser.add_argument(
        "--skip-poll",
        action="store_true",
        help="Disable ObserverAgent polling (use seed_events.py to inject test events)",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip starting the MCP server (for testing agents in isolation)",
    )
    return parser.parse_args()


def _start_mcp_server_thread() -> threading.Thread:
    """
    Run the MCP server in a background daemon thread with its own asyncio event loop.
    Using a thread (not multiprocessing) avoids Windows 'spawn' issues where
    the child process can't inherit the ASGI app object or sys.path correctly.
    The thread is daemon=True so it dies automatically when the main process exits.
    """
    def _run() -> None:
        import asyncio
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

        # Each thread needs its own event loop on Windows
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


async def _wait_for_mcp_server(timeout_s: int = 30) -> None:
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

    print(f"[Main] Waiting for MCP server at {url} ...")
    async with httpx.AsyncClient() as client:
        for _ in range(timeout_s * 2):  # probe every 0.5 s
            try:
                await client.get(url, timeout=1.0)
                print("[Main] MCP server is ready")
                return
            except (httpx.ConnectError, httpx.ConnectTimeout):
                pass
            await asyncio.sleep(0.5)

    raise TimeoutError(f"MCP server did not become ready within {timeout_s}s")


async def _check_redis() -> None:
    """Verify Redis is reachable before starting agents."""
    from memory.redis_client import RedisClient
    redis = RedisClient.get_instance()
    if not await redis.ping():
        raise ConnectionError(
            "Cannot connect to Redis.\n"
            "Start it with:  redis-server\n"
            "Or via Docker:  docker run -p 6379:6379 redis"
        )
    print("[Main] Redis connection OK")


async def main(args: argparse.Namespace) -> None:
    """Main coroutine — initializes everything and runs all agents concurrently."""
    from utils.logger import setup_logging
    from config.settings import get_settings

    # Ensure runtime directories exist
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

    await _check_redis()

    if not args.no_mcp:
        await _wait_for_mcp_server()

    from agents.observer_agent import ObserverAgent
    from agents.planner_agent import PlannerAgent
    from agents.executor_agent import ExecutorAgent
    from api.dashboard import app as dashboard_app
    import uvicorn

    tasks = []

    if not args.skip_poll and not args.no_mcp:
        observer = ObserverAgent()
        tasks.append(asyncio.create_task(observer.start(), name="observer"))

    planner = PlannerAgent()
    executor = ExecutorAgent()
    tasks.append(asyncio.create_task(planner.start(), name="planner"))
    tasks.append(asyncio.create_task(executor.start(), name="executor"))

    dashboard_config = uvicorn.Config(
        dashboard_app,
        host="0.0.0.0",
        port=cfg.dashboard_port,
        log_level="warning",
    )
    dashboard_server = uvicorn.Server(dashboard_config)
    tasks.append(asyncio.create_task(dashboard_server.serve(), name="dashboard"))

    print(f"\n[Main] All agents started. Dashboard: http://localhost:{cfg.dashboard_port}")
    if args.skip_poll:
        print("[Main] Tip: Run  python tests/seed_events.py  to inject demo events")
    print("[Main] Press Ctrl+C to stop\n")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        print("\n[Main] Shutting down...")


if __name__ == "__main__":
    args = _parse_args()

    if not args.no_mcp:
        print("[Main] Starting MCP tool server thread...")
        _start_mcp_server_thread()

    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    finally:
        print("[Main] Stopped.")
