"""
Backward-compatibility shim — re-exports the FastAPI app from api.app.

All actual logic lives in:
  api/app.py          — FastAPI factory + router wiring
  api/routers/        — individual endpoint modules
  api/ws.py           — WebSocket manager
  api/ui.py           — dashboard HTML string
"""
from api.app import app  # noqa: F401  (re-export for main.py and tests)

__all__ = ["app"]
