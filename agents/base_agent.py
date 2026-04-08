"""
BaseAgent — abstract base class for all agents.
Manages the MCP SSE client connection lifecycle and provides
a call_tool() helper with automatic retry.
"""
import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client


class BaseAgent(ABC):
    def __init__(self, name: str) -> None:
        self.name = name
        self._session: ClientSession | None = None
        self._sse_cm = None       # sse_client context manager (kept open)
        self._session_cm = None   # ClientSession context manager (kept open)
        # Logger is set up after setup_logging() runs in main.py
        self._logger = None

    @property
    def logger(self):
        if self._logger is None:
            from utils.logger import get_logger
            self._logger = get_logger(self.name)
        return self._logger

    # ── MCP Connection ────────────────────────────────────────────────────────

    async def connect_mcp(self) -> None:
        """
        Open SSE connection and initialize MCP ClientSession.
        Both context managers are stored as instance vars and kept alive
        for the entire agent lifetime — do NOT close/reopen per call.
        """
        from config.settings import get_settings
        url = get_settings().mcp_sse_url

        self.logger.info("mcp_connecting", url=url)
        # Open SSE transport
        self._sse_cm = sse_client(url=url)
        read_stream, write_stream = await self._sse_cm.__aenter__()

        # Open MCP session over the transport
        self._session_cm = ClientSession(read_stream, write_stream)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

        self.logger.info("mcp_connected", url=url)

    async def disconnect_mcp(self) -> None:
        """Clean up MCP session and SSE transport. Idempotent — safe to call multiple times."""
        if self._session_cm:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._session_cm = None
                self._session = None
        if self._sse_cm:
            try:
                await self._sse_cm.__aexit__(None, None, None)
            except Exception:
                pass
            finally:
                self._sse_cm = None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """
        Call an MCP tool with 3-attempt exponential backoff retry.
        Returns the tool result content.
        """
        last_exc = None
        for attempt in range(3):
            try:
                result = await self._session.call_tool(tool_name, arguments=arguments)
                # FastMCP >= 1.x returns list/dict results via structuredContent
                # with an empty content list.  Prefer structuredContent when set.
                sc = getattr(result, "structuredContent", None)
                if sc:
                    # {"result": [...]} for list-returning tools, plain dict otherwise
                    return sc.get("result", sc) if isinstance(sc, dict) else sc

                # Older / text-only tools put a single JSON TextContent item here.
                content = result.content
                if content and hasattr(content[0], "text"):
                    try:
                        return json.loads(content[0].text)
                    except (json.JSONDecodeError, ValueError):
                        return content[0].text
                return content
            except Exception as exc:
                last_exc = exc
                # Include the type name so empty-message exceptions (e.g.
                # anyio.ClosedResourceError) are still identifiable in logs.
                exc_label = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                if attempt < 2:
                    wait = 2 ** attempt  # 1s, 2s
                    self.logger.warning(
                        "tool_call_retry",
                        tool=tool_name,
                        attempt=attempt + 1,
                        wait=wait,
                        error=exc_label,
                    )
                    await asyncio.sleep(wait)

        exc_label = f"{type(last_exc).__name__}: {last_exc}" if str(last_exc) else type(last_exc).__name__
        self.logger.error("tool_call_failed", tool=tool_name, error=exc_label)
        raise RuntimeError(f"MCP tool '{tool_name}' failed after 3 attempts: {last_exc!r}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect to MCP then run the agent loop. Reconnects on disconnect."""
        while True:
            try:
                await self.connect_mcp()
                await self.run()
            except asyncio.CancelledError:
                # Task is being cancelled (e.g. Ctrl+C / asyncio.run() shutdown).
                # We MUST close the sse_client context manager here, from the same
                # task that opened it.  If we skip this, Python's event-loop
                # async-generator finalizer tries to aclose() it later from a
                # different task context, which causes anyio to raise:
                #   RuntimeError: Attempted to exit cancel scope in a different task
                # asyncio.shield() prevents a second CancelledError from aborting
                # the cleanup mid-way.
                try:
                    await asyncio.shield(self.disconnect_mcp())
                except Exception:
                    pass
                raise  # re-raise so the task terminates correctly
            except Exception as exc:
                self.logger.error("agent_crashed", error=str(exc))
                await self.disconnect_mcp()
                self.logger.info("agent_restarting", delay=5)
                await asyncio.sleep(5)

    @abstractmethod
    async def run(self) -> None:
        """Main agent loop — implemented by each subclass."""
        ...
