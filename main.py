"""
PersonalOS Agent — Entry Point
==============================
Starts the complete multi-agent system:
  1. MCP Tool Server  (daemon thread, port 8000, SSE)
  2. Observer Agent   (asyncio task — polls every 60s)
  3. Planner Agent    (asyncio task — LLM reasoning)
  4. Executor Agent   (asyncio task — confidence routing)
  5. FastAPI Dashboard (asyncio task — port 8080)

Run:
  python main.py              # full system (requires Google OAuth + Redis)
  python main.py --skip-poll  # disable Observer polling (use fixtures/seed_events.py)
  python main.py --no-mcp     # skip MCP server (for testing agents in isolation)

Demo flow:
  1. python tests/fixtures/seed_events.py   # pre-load 3 demo events into Redis
  2. python main.py --skip-poll             # Planner + Executor process seeded events

Scripts:
  python scripts/check.py               # pre-flight health check (run before main.py)
  python scripts/setup_google_auth.py   # complete Google OAuth flow
  python scripts/demo_call.py           # trigger a standalone Twilio test call
  .\\start.ps1                           # Windows: kill stale ports then start
"""
import argparse
import asyncio


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PersonalOS Multi-Agent System")
    parser.add_argument("--skip-poll", action="store_true",
                        help="Disable ObserverAgent polling")
    parser.add_argument("--no-mcp", action="store_true",
                        help="Skip starting the MCP server (for isolated testing)")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    if not args.no_mcp:
        print("[Main] Starting MCP tool server thread...")
        from core.bootstrap import start_mcp_server_thread
        start_mcp_server_thread()

    from core.bootstrap import run
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    finally:
        print("[Main] Stopped.")
