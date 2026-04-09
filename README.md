# PersonalOS Agent

> An autonomous multi-agent system that monitors your Gmail, Google Calendar, and local filesystem — then plans and executes actions on your behalf, with a human-in-the-loop approval dashboard.

Built for the **SOLARIS X Hackathon 2026**.

---

## Overview

PersonalOS Agent is a production-grade agentic pipeline composed of three autonomous agents that communicate through a shared **Redis** event queue and an **MCP (Model Context Protocol)** tool server:

| Agent | Role | Description |
|---|---|---|
| **Observer** | Eyes | Polls Gmail, Google Calendar, and sandbox filesystem every 60 s via MCP tools. Deduplicates events and pushes them to Redis. |
| **Planner** | Brain | Consumes events, calls an LLM (via OpenRouter) to produce structured JSON action plans with confidence scores. Weights urgency and historical approval rates from ChromaDB. |
| **Executor** | Hands | Routes plans by confidence: auto-executes high-confidence actions, sends borderline ones to the approval dashboard, and discards low-confidence ones. Records all outcomes to ChromaDB. |

```
┌───────────────────────────────────────────────────────────────────┐
│                          PersonalOS Agent                         │
│                                                                   │
│  ┌──────────────┐   events    ┌──────────────┐   plans    ┌──────────────┐
│  │   Observer   │────────────▶│   Planner    │──────────▶│   Executor   │
│  │  (polls MCP) │   (Redis)   │  (OpenRouter)│  (Redis)   │  (MCP tools) │
│  └──────┬───────┘             └──────────────┘            └──────┬───────┘
│         │                                                         │
│         └───────────────── MCP Tool Server ─────────────────────┘
│                      (Gmail · Calendar · Files)
│
│         ┌──────────────────────────────────────────┐
│         │  Dashboard :8080  (WebSocket + REST API)  │  ← human approval UI
│         └──────────────────────────────────────────┘
│
│           Redis · ChromaDB (vector memory) · Twilio (calls)
└───────────────────────────────────────────────────────────────────┘
```

---

## Features

- **Real-time email monitoring** — watches Gmail for unread messages, deduplicates across sessions, and triggers planning when important emails arrive.
- **Calendar awareness** — reads upcoming Google Calendar events within a configurable look-ahead window; creates new events via natural-language plans.
- **Filesystem assistant** — lists and reorganises files inside a sandboxed directory without any risk of escaping the sandbox root.
- **LLM-powered planning** — any OpenRouter-compatible model (GPT-4o, Claude, Qwen, Llama, etc.) produces structured JSON plans with urgency, confidence, and alternative action proposals.
- **Confidence-based routing**:
  - `> 90 %` → executed automatically, notification sent via Twilio.
  - `70 – 89 %` → queued for human approval on the dashboard.
  - `< 70 %` → discarded with a log entry.
- **Tabbed approval dashboard** — four panels in a single-page app:
  - **Approvals** — approve or reject pending actions; search, filter by priority, bulk-approve, export CSV.
  - **Analytics** — confidence distribution chart, auto-execute rate, priority/response breakdowns, queue depth indicators.
  - **Inject Event** — push synthetic email/calendar/filesystem events directly into the pipeline without Google OAuth (ideal for live demos).
  - **Preferences** — view and add ChromaDB user-preference statements that personalise every planning decision.
- **WebSocket real-time updates** — the dashboard receives instant server-push notifications when plans are approved, rejected, or injected; falls back to polling when WebSocket is unavailable.
- **Test Call button** — trigger a real Twilio call (or see a simulation) from the dashboard header without waiting for an actual email.
- **Vector memory** — ChromaDB stores user preference embeddings and historical approval rates, which the Planner uses to continuously improve its confidence calibration.
- **MCP protocol** — all tool calls go through a structured SSE-based JSON-RPC server; agents never call external APIs directly.
- **Pre-flight checker** — `scripts/check.py` validates every external dependency before the system starts.
- **Graceful Ctrl+C shutdown** — all MCP SSE connections are cleanly closed before the event loop exits.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | Required for `asyncio` improvements and `X \| Y` union type hints |
| Redis | 6+ | `redis-server` or Docker |
| Google Cloud project | — | Gmail API + Calendar API enabled |
| OpenRouter API key | — | Free tier sufficient for demos |
| Twilio account | — | Optional — leave blank to use simulation mode |

---

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/your-org/personal-os-agent.git
cd personal-os-agent
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set OPENROUTER_API_KEY
```

### 3. Set up Google OAuth

Download `credentials.json` from [Google Cloud Console](https://console.cloud.google.com) → **APIs & Services → Credentials**, then run the one-shot OAuth flow:

```bash
python scripts/setup_google_auth.py
```

This opens a browser, authorises both Gmail and Calendar scopes, and saves `secrets/token.json` locally.

### 4. Start Redis

```bash
# Native
redis-server

# Or Docker
docker run -d -p 6379:6379 redis:alpine
```

### 5. Run the pre-flight check

```bash
python scripts/check.py
```

All checks should pass (the MCP server check shows `FAIL` — that is expected; it starts with `main.py`).

### 6. Start the system

```bash
python main.py
```

Open the dashboard at **http://localhost:8080**.

---

## Demo Mode (no Google OAuth required)

### Option A — Seed script (recommended)

```bash
# Terminal 1 — start with polling disabled
python main.py --skip-poll

# Terminal 2 — inject 4 realistic demo events (email + calendar)
python tests/fixtures/seed_events.py
```

The Planner and Executor process the seeded events immediately. High-confidence plans auto-execute and trigger a Twilio call; medium-confidence plans appear on the dashboard for manual approval.

### Option B — Dashboard injection (no extra terminal needed)

1. Start `python main.py --skip-poll`
2. Open **http://localhost:8080**
3. Click the **Inject Event** tab
4. Fill in a subject, sender, snippet, check **"Mark as urgent"**, and click **Inject Event →**

The event flows through the full pipeline in real time on-screen.

### Option C — Pre-built dashboard seed

```bash
python main.py --skip-poll
python tests/fixtures/seed_dashboard.py   # injects 6 ready-made plans directly
```

---

## Configuration

All settings are loaded from `.env` via `pydantic-settings`. Every value has a sensible default; only `OPENROUTER_API_KEY` is strictly required.

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | *(required)* | OpenRouter API key — get one at [openrouter.ai](https://openrouter.ai) |
| `OPENROUTER_MODEL` | `openai/gpt-oss-20b:free` | Any model slug from OpenRouter |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible base URL |
| `HUGGINGFACE_TOKEN` | *(blank)* | Optional — increases HuggingFace model download rate limits |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `CHROMA_PERSIST_PATH` | `./chroma_data` | Directory for ChromaDB on-disk persistence |
| `CHROMA_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | SentenceTransformers embedding model |
| `MCP_SERVER_HOST` | `127.0.0.1` | MCP tool server bind address |
| `MCP_SERVER_PORT` | `8000` | MCP tool server port |
| `DASHBOARD_PORT` | `8080` | Human approval dashboard port |
| `GOOGLE_CREDENTIALS_PATH` | `./secrets/credentials.json` | Path to Google OAuth client secrets |
| `GOOGLE_TOKEN_PATH` | `./secrets/token.json` | Path for storing OAuth access/refresh tokens |
| `FS_ALLOWED_ROOT` | `./sandbox` | Root directory for all filesystem tool operations |
| `TWILIO_ACCOUNT_SID` | *(blank = simulation)* | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | *(blank = simulation)* | Twilio auth token |
| `TWILIO_FROM_NUMBER` | — | E.164 Twilio phone number (e.g. `+15551234567`) |
| `TWILIO_TO_NUMBER` | — | E.164 destination number for notifications |
| `OBSERVER_POLL_INTERVAL` | `60` | Seconds between Observer polling cycles |

---

## Running Options

```bash
# Full system — all agents + MCP server + dashboard
python main.py

# Disable Observer (inject events manually)
python main.py --skip-poll

# Skip MCP server (for testing agents in isolation, no tool calls)
python main.py --no-mcp
```

---

## Dashboard

The approval dashboard is a tabbed single-page app served at **http://localhost:8080**.

### REST API

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Full dashboard SPA (HTML) |
| `/api/emails` | `GET` | All plans sorted newest-first (every confidence level) |
| `/api/pending` | `GET` | Plans currently awaiting human approval |
| `/api/approve/{id}` | `POST` | Approve a plan — re-queues to Executor for execution |
| `/api/reject/{id}` | `POST` | Reject and discard a plan, records outcome in ChromaDB |
| `/api/feed` | `GET` | Activity log — last 50 entries (newest first) |
| `/api/health` | `GET` | Multi-service health check (Redis, LLM, Google, Twilio, ChromaDB, MCP) |
| `/api/metrics` | `GET` | Confidence histogram, priority/response breakdown, avg confidence, queue depths |
| `/api/preferences` | `GET` | List all ChromaDB user-preference documents |
| `/api/preferences` | `POST` | Add a new natural-language preference statement |
| `/api/events/inject` | `POST` | Inject a synthetic event into the pipeline (demo mode) |
| `/api/twilio/test` | `POST` | Trigger a real or simulated Twilio test call |

### WebSocket

| Endpoint | Description |
|---|---|
| `/ws` | WebSocket — server pushes `{"type":"refresh"}` when plans change; keep-alive ping every 20 s |

---

## Testing

The project ships a comprehensive pytest suite covering both unit logic and live integration against the running system.

### Install test dependencies

```bash
pip install -e .[dev]
```

> Alternatively: `pip install pytest pytest-asyncio httpx`

### Unit tests — no running system needed

```bash
pytest tests/test_backend.py -m unit -v
```

Covers (18 tests, ~25 s):
- Redis client: connectivity, queue round-trip, deduplication, email records, activity log
- Observer: email/calendar/filesystem normalizers, urgency keyword detection
- Planner: JSON parsing (valid, markdown-fenced, invalid, clamping, defaults), confidence scoring
- Executor: routing thresholds (>90 auto, 70–90 dashboard, <70 discard), ACTION_TOOL_MAP completeness

### Integration tests — requires `python main.py` running

```bash
# Start the system first
python main.py

# Then in a second terminal:
pytest tests/test_backend.py -m integration -v
```

Covers (15 tests, ~4 min):
- Dashboard HTML, `/api/health`, `/api/emails`, `/api/metrics`, `/api/feed`, `/api/preferences`
- `POST /api/poll/now` — Observer wakes immediately
- MCP server TCP reachability
- Real Observer poll cycle via `GET /api/emails`
- Synthetic event injection (email, calendar, invalid type)
- Approve/reject flow with 404 guard for non-existent IDs
- Full approve lifecycle: inject → Planner → dashboard:pending → approve → emails:all

### Full suite

```bash
pytest tests/test_backend.py -v
```

> **Note:** The `test_inject_urgent_email_flows_to_emails_all` test is automatically skipped when the OpenRouter free-tier daily rate limit is reached (HTTP 429). This is expected on the free plan — it confirms the Planner correctly surfaces errors to the dashboard rather than silently dropping events.

### Standalone pipeline runner

```bash
# Reads real Gmail, calls the LLM, and pushes results directly to Redis/dashboard
python scripts/pipeline_runner.py
```

---

## Project Structure

```
personal-os-agent/
├── agents/
│   ├── base_agent.py          # Abstract base: MCP lifecycle + tool call retries
│   ├── observer_agent.py      # Polls Gmail / Calendar / FS, pushes to Redis
│   ├── planner_agent.py       # LLM reasoning, produces enriched JSON action plans
│   └── executor_agent.py      # Confidence routing, MCP execution, ChromaDB recording
├── api/
│   ├── app.py                 # FastAPI factory — wires all routers and WebSocket
│   ├── ws.py                  # WebSocket ConnectionManager (broadcast helper)
│   ├── static/
│   │   ├── dashboard.html     # Single-page frontend (served as static file)
│   │   ├── style.css          # All CSS
│   │   └── app.js             # All JavaScript + WebSocket client
│   └── routers/
│       ├── approvals.py       # GET/POST /api/pending, /emails, /approve, /reject, /poll
│       ├── events.py          # POST /api/events/inject
│       ├── health.py          # GET /api/health
│       ├── metrics.py         # GET /api/metrics
│       ├── preferences.py     # GET/POST /api/preferences
│       └── twilio.py          # POST /api/twilio/test
├── config/
│   └── settings.py            # Pydantic-settings config (loaded once, lru_cache)
├── core/
│   └── bootstrap.py           # App bootstrap: MCP thread, agent tasks, uvicorn startup
├── memory/
│   ├── redis_client.py        # Async Redis singleton: queues, dedup, activity log
│   └── chroma_memory.py       # ChromaDB vector memory: preferences + outcomes
├── mcp_server/
│   ├── server.py              # FastMCP app factory, idempotent tool registration
│   ├── gmail_tools.py         # read_emails, send_email
│   ├── calendar_tools.py      # read_calendar, create_event
│   ├── filesystem_tools.py    # list_files, move_file (sandboxed)
│   └── google_auth.py         # Shared OAuth2 flow (Gmail + Calendar scopes)
├── utils/
│   ├── logger.py              # Structlog setup (colorized console + JSON file)
│   └── notifier.py            # Twilio outbound calls (or simulation fallback)
├── tests/
│   ├── fixtures/
│   │   ├── seed_events.py     # Inject 4 synthetic events for --skip-poll demo
│   │   └── seed_dashboard.py  # Inject 6 pre-built plans directly to emails:all
│   ├── test_backend.py        # Pytest suite: 33 unit + integration tests
│   └── conftest.py            # Pytest configuration and shared helpers
├── scripts/
│   ├── check.py               # Pre-flight health checker (run before main.py)
│   ├── setup_google_auth.py   # One-shot Google OAuth setup helper
│   ├── demo_call.py           # One-shot Twilio call test (requires TWILIO_* in .env)
│   ├── pipeline_runner.py     # Standalone runner: real Gmail → Planner → Redis → dashboard
│   └── start.ps1              # Windows: kill stale ports then start main.py
├── docs/
│   └── ARCHITECTURE.md        # Deep-dive architecture documentation
├── secrets/                   # Google OAuth credentials (gitignored)
├── logs/                      # Runtime log output (created at startup)
├── sandbox/                   # Filesystem tool sandbox root (created at startup)
├── main.py                    # Entry point — starts all agents concurrently
├── start.ps1                  # Thin Windows launcher (delegates to scripts/start.ps1)
├── pyproject.toml             # Project metadata + dependencies (replaces requirements files)
├── requirements.txt           # Convenience pin-list for plain pip install
├── .env.example
├── CHANGELOG.md
└── CONTRIBUTING.md
```

---

## MCP Tools Reference

The MCP tool server exposes 6 tools over SSE JSON-RPC at `http://127.0.0.1:8000/sse`:

| Tool | Args | Returns |
|---|---|---|
| `read_emails` | `max_results=10`, `query=""` | `[{id, from, subject, snippet, date, unread}]` |
| `send_email` | `to`, `subject`, `body` | `{message_id, status}` |
| `read_calendar` | `days_ahead=7` | `[{id, summary, start, end, attendees, location, description}]` |
| `create_event` | `summary`, `start_datetime`, `end_datetime`, `description?`, `attendees?`, `location?` | `{event_id, html_link, status}` |
| `list_files` | `directory="."` | `[{name, path, size_bytes, modified_iso, is_dir}]` |
| `move_file` | `source`, `destination` | `{source, destination, success, error}` |

All `*_datetime` values are ISO 8601 strings (e.g. `2026-04-09T14:00:00+05:30`).

---

## Technology Stack

| Layer | Technology |
|---|---|
| Agents | Python 3.11, asyncio |
| LLM | OpenRouter (OpenAI-compatible API) |
| Tool protocol | MCP (Model Context Protocol) over SSE |
| Data store | Redis (queues, dedup, activity log) |
| Vector memory | ChromaDB + sentence-transformers |
| Google APIs | Gmail API v1, Calendar API v3, OAuth 2.0 |
| Phone notifications | Twilio Voice API |
| Web dashboard | FastAPI + vanilla JS (static HTML/CSS/JS, no template engine) |
| Real-time | WebSocket (`fastapi.WebSocket` + `websockets`) |
| HTTP client | httpx (async) |
| Configuration | pydantic-settings |
| Logging | structlog + colorama |

---

## Security Notes

- `secrets/credentials.json` and `secrets/token.json` contain Google OAuth secrets — **never commit these to version control**. The `secrets/` directory is in `.gitignore`.
- The `.env` file contains API keys — **never commit it**. Only `.env.example` (with blank values) should be tracked.
- The filesystem tool is sandboxed to `FS_ALLOWED_ROOT` (`./sandbox` by default). Paths that attempt directory traversal outside this root are rejected with `PermissionError` at the tool layer.
- The MCP server binds to `127.0.0.1` by default and is not exposed externally.
- The dashboard binds to `0.0.0.0` (all interfaces) so it is reachable on a local network. Restrict to `127.0.0.1` for production.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## Architecture

For a deep dive into data flow, agent decision trees, and extension points, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## License

MIT © 2026 SOLARIS X Hackathon
