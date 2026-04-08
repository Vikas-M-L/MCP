# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

---

## [1.2.0] — 2026-04-08

### Fixed

- **`planner_agent.py` — `emails:all` never populated in live pipeline**: the planner never called `redis.push_email_record()`, so the dashboard email list was permanently blank when running `main.py`. Plans are now stored in `emails:all` immediately after `push_approval()`.
- **`executor_agent.py` — `emails:all` not updated by Executor**: `_auto_execute`, `_push_to_dashboard`, and `_silent_discard` all now call `push_email_record()` so the email list reflects every routed plan regardless of confidence.
- **`planner_agent.py` — plans missing critical dashboard UI fields**: live plans lacked `subject`, `from_addr`, `snippet`, `priority`, `urgency_keywords`, and `call_text`. Dashboard cards showed blank subjects and empty sender fields. All fields are now populated from the event payload before storing.
- **`planner_agent.py` — scoring field mismatch**: the planner stored `scoring_details` with keys `base_confidence`, `urgency_multiplier`, `history_multiplier`, but the dashboard template read `item.scoring?.base`, `item.scoring?.urgency_mult`, `item.scoring?.history_mult`. Renamed key to `scoring` and aligned inner key names.
- **`tests/seed_events.py` — stale dedup clearing**: `r.srem("seen:event_ids", event_id)` was a no-op after the dedup system was migrated to individual `seen_event:<id>` keys. Fixed to `r.delete(f"seen_event:{event_id}")`.
- **`utils/notifier.py` — `call_text` ignored**: `_build_message()` always generated a generic script even when `plan["call_text"]` contained a fully crafted contextual message. `notify()` now uses `plan["call_text"]` when present.

### Added

- **Dashboard — Analytics tab**: confidence distribution bar chart (pure CSS), auto-execute rate, average confidence, priority/response breakdown, queue depth indicators. Powered by `/api/metrics`.
- **Dashboard — Inject Event tab**: form to push synthetic email, calendar, or filesystem events directly into `events:queue` without Google OAuth. Supports urgency flag to boost Planner confidence. Ideal for live demos.
- **Dashboard — Preferences tab**: lists all ChromaDB user-preference documents and provides an "Add Preference" form that calls `POST /api/preferences`.
- **Dashboard — Test Call button**: placed in the page header; triggers `POST /api/twilio/test` and shows a toast with the Twilio call SID or simulation details.
- **Dashboard — Navigation tabs**: the single-page app is now tabbed (Approvals / Analytics / Inject Event / Preferences) using pure JS show/hide — no page reload.
- **`GET /api/metrics`**: returns `total`, `by_priority`, `by_response`, `avg_confidence`, `confidence_distribution` (histogram), and `queue_depths`.
- **`GET /api/preferences`**: lists all ChromaDB `user_preferences` collection documents with metadata.
- **`POST /api/preferences`**: adds a natural-language preference statement with a configurable category.
- **`POST /api/events/inject`**: injects a synthetic event (email, calendar, or filesystem) into `events:queue` and broadcasts a WebSocket notification.
- **`POST /api/twilio/test`**: places a real Twilio test call or returns a simulation payload if credentials are absent.
- **`GET /ws`** — WebSocket endpoint: a `ConnectionManager` broadcasts `{"type":"refresh"}` to all connected clients when plans are approved, rejected, or injected. Dashboard JS connects on load and falls back to 8 s polling.
- **`planner_agent.py` — `_build_call_text()`**: module-level helper that builds a contextual Twilio call script from the event and plan, including priority label, from address, subject, action, reason, and routing outcome.
- **`planner_agent.py` — LLM `priority` field**: the LLM schema now includes a `priority` field (`high`/`medium`/`low`) so the Planner classifies email importance from content rather than solely from the confidence score.
- **`memory/redis_client.py` — `clear_event_seen()`**: new helper to delete a single `seen_event:<id>` key; used by demo seed scripts to reset deduplication for specific events.
- **`config/settings.py` — `huggingface_token` field**: HuggingFace token is now a proper pydantic-settings field instead of being read directly from `os.environ` in `chroma_memory.py`.
- **`requirements.txt` — `websockets>=12.0`**: added explicit dependency required by FastAPI's WebSocket support.
- **`.env.example` — `HUGGINGFACE_TOKEN`**: added with inline comment; grouped near OpenRouter LLM settings.

### Changed

- **`config/settings.py`** — default `openrouter_model` updated from `qwen/qwen3-6b:free` to `openai/gpt-oss-20b:free` to match the recommended model.
- **`.env.example`** — `HUGGINGFACE_TOKEN` moved next to OpenRouter keys (logical grouping); default model updated.
- **`executor_agent.py` — activity log messages** now include the plan's `subject` (first 40 chars) for easier log scanning.
- **Dashboard WebSocket client** replaces `setInterval(refresh, 3000)` with instant server-push; polling fallback interval extended to 8 s.
- **`api/dashboard.py`** — `approve_action` and `reject_action` now call `manager.broadcast()` after mutating state, so all connected dashboards refresh immediately.

---

## [1.1.0] — 2026-04-08

### Fixed

- **`base_agent.py` — Ctrl+C shutdown crash** (`RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`): `BaseAgent.start()` now catches `asyncio.CancelledError` and calls `disconnect_mcp()` via `asyncio.shield()` before re-raising, so the `sse_client` async generator is always closed from the task that opened it. Previously, Python's event-loop async-generator finalizer tried to `aclose()` the generator from a different task during shutdown, violating anyio's cancel-scope constraints.
- **`api/dashboard.py` — MCP health probe always failed**: the `/api/health` endpoint was probing the SSE streaming endpoint (`/sse`), which hangs indefinitely. Changed to probe the root URL (`/`) — any HTTP response confirms the server is alive.
- **`check.py` — MCP health probe always failed**: same fix as above applied to the pre-flight checker.
- **`memory/redis_client.py` — `seen:event_ids` TTL broken**: the previous implementation used `SADD` + `EXPIRE` on a shared Redis Set, resetting the 24-hour TTL on every insertion and causing unbounded growth. Replaced with individual `SETEX` keys (`seen_event:<id>`) so each event ID expires independently after 24 hours.
- **`api/dashboard.py` — double ChromaDB recording on approval**: the `approve_action` endpoint was recording outcomes in ChromaDB, and the Executor recorded them again after execution. Removed the duplicate call from the dashboard; the Executor is now the sole recorder.
- **`mcp_server/server.py` — duplicate tool registration**: `build_mcp_app()` could re-register tools if called more than once. Added an idempotency guard (`_tools_registered` flag).
- **`mcp_server/filesystem_tools.py` — timezone-naive datetime**: `list_files` produced local-time strings without timezone info. Fixed to emit UTC ISO 8601 timestamps using `datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()`. Also removed the `__import__('datetime', ...)` hack in favour of a top-level import.
- **`check.py` — `UnicodeEncodeError` on Windows**: box-drawing characters (`═`, `─`, `→`) raised `charmap` encoding errors on PowerShell. Added `sys.stdout.reconfigure(encoding='utf-8')` at startup.
- **`main.py` — race condition at startup**: replaced the TCP-only readiness probe (`socket.create_connection`) with an HTTP GET probe so agents wait until uvicorn has fully registered all ASGI routes before connecting.

### Added

- **Improved MCP error logging in `base_agent.py`**: exception labels now include the type name when `str(exc)` is empty (e.g. `BrokenResourceError`, `ClosedResourceError`), making retry logs actionable.
- **Observer reconnect logic** (`observer_agent.py`): if all three MCP sources fail in a single polling cycle, a `RuntimeError` is raised to trigger a clean session reconnect in the base agent's `start()` loop.

### Changed (Cleanups)

- `executor_agent.py` — removed unused `import asyncio`.
- `planner_agent.py` — removed unused `import asyncio`.
- `observer_agent.py` — removed unused `import json`.
- `base_agent.py` — moved `import json` to module top-level.
- `mcp_server/server.py` — updated stale docstring (multiprocessing → daemon thread).
- `main.py` — removed unused `import sys` and dead `except KeyboardInterrupt` block inside `main()`.

---

## [1.0.0] — 2026-04-07

### Added

- Initial release for SOLARIS X Hackathon 2026.
- **`ObserverAgent`**: polls Gmail (`read_emails`), Google Calendar (`read_calendar`), and sandbox filesystem (`list_files`) on a configurable interval. Deduplicates events via Redis and pushes novel events to `events:queue`.
- **`PlannerAgent`**: LLM-powered planning via OpenRouter (OpenAI-compatible). Produces structured JSON plans with `action`, `confidence`, `urgency`, `alternatives`. Weights scores using ChromaDB historical approval rates.
- **`ExecutorAgent`**: confidence-based routing (auto / dashboard / discard). Executes approved plans via MCP tool calls. Records all outcomes in ChromaDB. Sends Twilio phone call notifications on auto-execution.
- **MCP Tool Server**: FastMCP over SSE (port 8000). Tools: `read_emails`, `send_email`, `read_calendar`, `create_event`, `list_files`, `move_file`.
- **FastAPI Dashboard** (port 8080): real-time human approval UI, JSON API endpoints, multi-service health check.
- **Redis Client**: async singleton for all queue, deduplication, and activity-log operations.
- **ChromaDB Memory**: persistent vector store for `user_preferences` and `action_outcomes` collections.
- **Twilio Notifier**: outbound voice call on auto-executed actions; falls back to console simulation when credentials are absent.
- **`check.py`**: 35-point pre-flight health checker covering Python version, env vars, Redis, OpenRouter, Gmail, Calendar, Twilio, ChromaDB, embedding model, packages, and MCP server.
- **`setup_google_auth.py`**: one-shot Google OAuth 2.0 browser flow.
- **`tests/seed_events.py`**: injects three synthetic demo events for development without Google credentials.
- **Structlog** console + file logging with JSON output.
- **`--skip-poll`** and **`--no-mcp`** CLI flags for development and testing.

---

[Unreleased]: https://github.com/your-org/personal-os-agent/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/your-org/personal-os-agent/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/your-org/personal-os-agent/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/your-org/personal-os-agent/releases/tag/v1.0.0
