"""Professional CLI entry point for the Personal OS Agent."""

import argparse
import asyncio
from typing import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PersonalOS Multi-Agent System")
    parser.add_argument(
        "--skip-poll",
        action="store_true",
        help="Disable ObserverAgent polling",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip starting the MCP server (for isolated testing)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

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


if __name__ == "__main__":
    main()
