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
│                      ┌─────────────────┐
│                      │  Dashboard :8080 │  ← human approval UI
│                      └─────────────────┘
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
  - `>= 90 %` → executed automatically, notification sent via Twilio.
  - `70 – 89 %` → queued for human approval on the dashboard.
  - `< 70 %` → discarded with a log entry.
- **Human-in-the-loop dashboard** — approve or reject pending actions from a web UI in real time.
- **Vector memory** — ChromaDB stores user preference embeddings and historical approval rates, which the Planner uses to continuously improve its confidence calibration.
- **MCP protocol** — all tool calls go through a structured SSE-based JSON-RPC server; agents never call external APIs directly.
- **Pre-flight checker** — `check.py` validates every external dependency before the system starts.
- **Graceful Ctrl+C shutdown** — all MCP SSE connections are cleanly closed before the event loop exits.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | Required for `asyncio` improvements |
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
# Edit .env with your API keys (see Configuration section below)
```

### 3. Set up Google OAuth

Download `credentials.json` from [Google Cloud Console](https://console.cloud.google.com) → **APIs & Services → Credentials**, then run the one-shot OAuth flow:

```bash
python setup_google_auth.py
```

This opens a browser, authorises both Gmail and Calendar scopes, and saves `token.json` locally.

### 4. Start Redis

```bash
# Native
redis-server

# Or Docker
docker run -d -p 6379:6379 redis:alpine
```

### 5. Run the pre-flight check

```bash
python check.py
```

All 35 checks should pass (the MCP server check will show `FAIL` — that is expected, it starts with `main.py`).

### 6. Start the system

```bash
python main.py
```

Open the dashboard at **http://localhost:8080**.

---

## Demo Mode (no Google OAuth required)

```bash
# Terminal 1 — start with polling disabled
python main.py --skip-poll

# Terminal 2 — inject three pre-built demo events
python tests/seed_events.py
```

The Planner and Executor will process the seeded events immediately. Borderline-confidence plans appear on the dashboard for manual approval.

---

## Configuration

All settings are loaded from `.env` via `pydantic-settings`. Every value has a sensible default; only `OPENROUTER_API_KEY` is strictly required.

| Variable | Default | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | *(required)* | OpenRouter API key — get one at [openrouter.ai](https://openrouter.ai) |
| `OPENROUTER_MODEL` | `qwen/qwen3-6b:free` | Any model slug from OpenRouter |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible base URL |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `CHROMA_PERSIST_PATH` | `./chroma_data` | Directory for ChromaDB on-disk persistence |
| `CHROMA_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | SentenceTransformers embedding model |
| `MCP_SERVER_HOST` | `127.0.0.1` | MCP tool server bind address |
| `MCP_SERVER_PORT` | `8000` | MCP tool server port |
| `DASHBOARD_PORT` | `8080` | Human approval dashboard port |
| `GOOGLE_CREDENTIALS_PATH` | `./credentials.json` | Path to Google OAuth client secrets |
| `GOOGLE_TOKEN_PATH` | `./token.json` | Path for storing OAuth access/refresh tokens |
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

# Disable Observer (inject events manually with seed_events.py)
python main.py --skip-poll

# Skip MCP server (for testing agents in isolation, no tool calls)
python main.py --no-mcp
```

---

## Dashboard

The approval dashboard is a real-time web UI served at **http://localhost:8080**.

| Endpoint | Description |
|---|---|
| `GET /` | Approval UI — lists pending plans, approve / reject buttons |
| `GET /api/pending` | JSON list of all plans awaiting human approval |
| `POST /api/approve/{id}` | Approve a plan; routes it to the Executor |
| `POST /api/reject/{id}` | Reject and discard a plan |
| `GET /api/activity` | Recent activity log (last 50 entries) |
| `GET /api/health` | Multi-service health check (Redis, LLM, Google, Twilio, Chroma, MCP) |

---

## Project Structure

```
personal-os-agent/
├── agents/
│   ├── base_agent.py          # Abstract base: MCP lifecycle + tool call retries
│   ├── observer_agent.py      # Polls Gmail / Calendar / FS, pushes to Redis
│   ├── planner_agent.py       # LLM reasoning, produces JSON action plans
│   └── executor_agent.py      # Confidence routing, MCP execution, Chroma recording
├── api/
│   └── dashboard.py           # FastAPI approval dashboard + health API
├── config/
│   └── settings.py            # Pydantic-settings config (loaded once, cached)
├── memory/
│   ├── redis_client.py        # Async Redis singleton: queues, dedup, activity log
│   └── chroma_memory.py       # ChromaDB vector memory: preferences + outcomes
├── mcp_server/
│   ├── server.py              # FastMCP app factory, tool registration guard
│   ├── gmail_tools.py         # read_emails, send_email
│   ├── calendar_tools.py      # read_calendar, create_event
│   ├── filesystem_tools.py    # list_files, move_file (sandboxed)
│   └── google_auth.py         # Shared OAuth2 flow (Gmail + Calendar scopes)
├── utils/
│   ├── logger.py              # Structlog setup (console + file)
│   └── notifier.py            # Twilio outbound calls (or simulation fallback)
├── tests/
│   └── seed_events.py         # Inject demo events into Redis for testing
├── docs/
│   └── ARCHITECTURE.md        # Deep-dive architecture documentation
├── logs/                      # Runtime log output (created at startup)
├── sandbox/                   # Filesystem tool sandbox root (created at startup)
├── main.py                    # Entry point — starts all agents concurrently
├── check.py                   # Pre-flight health checker (run before main.py)
├── setup_google_auth.py       # One-shot Google OAuth setup
├── requirements.txt
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
| Web dashboard | FastAPI + Jinja2 / vanilla JS |
| HTTP client | httpx (async) |
| Configuration | pydantic-settings |
| Logging | structlog + colorama |

---

## Security Notes

- `credentials.json` and `token.json` contain Google OAuth secrets — **never commit these to version control**. Add them to `.gitignore`.
- The `.env` file contains API keys — **never commit it**. Only `.env.example` (with blank values) should be tracked.
- The filesystem tool is sandboxed to `FS_ALLOWED_ROOT` (`./sandbox` by default). Paths outside this root are rejected at the tool layer.
- The MCP server binds to `127.0.0.1` by default and is not exposed externally.

Suggested `.gitignore` additions:

```
.env
credentials.json
token.json
chroma_data/
sandbox/
logs/
__pycache__/
*.pyc
```

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
