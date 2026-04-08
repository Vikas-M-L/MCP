"""
WebSocket connection manager — broadcasts server-push events to all connected dashboards.
"""
import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect


class ConnectionManager:
    """Tracks active WebSocket connections and broadcasts JSON messages."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._connections = [c for c in self._connections if c is not ws]

    async def broadcast(self, data: dict) -> None:
        """Send JSON to all clients; silently drop stale connections."""
        payload = json.dumps(data)
        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


# Module-level singleton shared across all routers
manager = ConnectionManager()
