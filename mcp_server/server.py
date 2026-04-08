"""
MCP Tool Server — PersonalOS.
Runs in a daemon thread (started by main.py via threading.Thread).
Exposes 6 tools over SSE transport on port 8000:
  Gmail:    read_emails, send_email
  Calendar: read_calendar, create_event
  FS:       list_files, move_file
"""
from mcp.server.fastmcp import FastMCP

from mcp_server.gmail_tools import register_gmail_tools
from mcp_server.calendar_tools import register_calendar_tools
from mcp_server.filesystem_tools import register_filesystem_tools

# Module-level FastMCP instance — tools are registered once via build_mcp_app()
mcp = FastMCP("PersonalOS")
_tools_registered = False


def build_mcp_app() -> FastMCP:
    """Register all tools and return the configured FastMCP instance.

    Idempotent: calling this more than once (e.g. in tests) will not
    double-register tools on the shared mcp instance.
    """
    global _tools_registered
    if not _tools_registered:
        register_gmail_tools(mcp)
        register_calendar_tools(mcp)
        register_filesystem_tools(mcp)
        _tools_registered = True
    return mcp


def run_server() -> None:
    """
    Standalone entry point — run the MCP server directly (e.g. python -m mcp_server.server).
    In normal operation main.py starts the server via _start_mcp_server_thread(),
    which calls build_mcp_app() + uvicorn directly without going through this function.

    FastMCP.run() does not accept host/port kwargs in this version.
    Instead we call sse_app() to get the ASGI app and run it directly
    with uvicorn, which gives full control over host and port.
    """
    import uvicorn
    from config.settings import get_settings
    cfg = get_settings()

    app = build_mcp_app()
    print(
        f"[MCP Server] PersonalOS starting on "
        f"http://{cfg.mcp_server_host}:{cfg.mcp_server_port} (SSE)"
    )
    # sse_app() returns the Starlette ASGI app for SSE transport
    uvicorn.run(
        app.sse_app(),
        host=cfg.mcp_server_host,
        port=cfg.mcp_server_port,
        log_level="warning",
    )


if __name__ == "__main__":
    run_server()
