"""
PersonalOS FastAPI Application — port 8080.

Endpoints are split into focused routers:
  api/routers/approvals.py   — plan approval / rejection / feed
  api/routers/events.py      — synthetic event injection
  api/routers/health.py      — live service health checks
  api/routers/metrics.py     — aggregate stats / confidence histogram
  api/routers/preferences.py — ChromaDB user preference CRUD
  api/routers/twilio.py      — outbound call trigger

Static assets served from api/static/:
  dashboard.html             — single-page frontend
  style.css                  — all CSS
  app.js                     — all JavaScript

WebSocket:
  api/ws.py                  — ConnectionManager + /ws endpoint
"""
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.ws import manager
from api.routers import approvals, events, health, metrics, preferences, twilio, voice

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="PersonalOS Dashboard", version="1.3.0")

# ── Static files ───────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

# ── Routers ────────────────────────────────────────────────────────────────────
app.include_router(approvals.router)
app.include_router(events.router)
app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(preferences.router)
app.include_router(twilio.router)
app.include_router(voice.router)


# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Pushes 'refresh' events to all connected dashboards."""
    await manager.connect(websocket)
    try:
        while True:
            await __import__("asyncio").sleep(20)
            await websocket.send_text('{"type":"ping"}')
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


# ── Dashboard UI ───────────────────────────────────────────────────────────────
@app.get("/")
async def dashboard_page() -> FileResponse:
    return FileResponse(_STATIC_DIR / "dashboard.html")
