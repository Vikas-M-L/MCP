# Architecture — PersonalOS Agent

This document describes the internal design of PersonalOS Agent: data flows, agent decision trees, concurrency model, and extension points.

---

## Table of Contents

1. [High-Level Overview](#high-level-overview)
2. [Concurrency Model](#concurrency-model)
3. [MCP Tool Server](#mcp-tool-server)
4. [Agent Internals](#agent-internals)
   - [BaseAgent](#baseagent)
   - [ObserverAgent](#observeragent)
   - [PlannerAgent](#planneragent)
   - [ExecutorAgent](#executoragent)
5. [Data Flows](#data-flows)
6. [Memory Subsystem](#memory-subsystem)
7. [Human Approval Dashboard](#human-approval-dashboard)
8. [Configuration System](#configuration-system)
9. [Startup Sequence](#startup-sequence)
10. [Shutdown Sequence](#shutdown-sequence)
11. [Extension Points](#extension-points)

---

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  main.py  (asyncio event loop)                                          │
│                                                                         │
│  ┌──────────────────┐                                                   │
│  │  MCP Tool Server │  daemon thread · own event loop · port 8000      │
│  │  ─────────────── │                                                   │
│  │  read_emails     │                                                   │
│  │  send_email      │◄──── JSON-RPC over SSE ──────────────────────┐   │
│  │  read_calendar   │                                               │   │
│  │  create_event    │                                               │   │
│  │  list_files      │                                               │   │
│  │  move_file       │                                               │   │
│  └──────────────────┘                                               │   │
│                                                                     │   │
│  ┌──────────────┐  events:queue  ┌──────────────┐  approvals:     │   │
│  │   Observer   │───────────────▶│   Planner    │  pending        │   │
│  │   (task)     │    (Redis)     │   (task)     │────────────┐    │   │
│  └──────┬───────┘                └──────────────┘            │    │   │
│         │ MCP SSE                                             ▼    │   │
│         └────────────────────────────────────┐   ┌──────────────┐ │   │
│                                              │   │   Executor   │─┘   │
│                                              │   │   (task)     │──────┘
│                                              │   └──────────────┘
│                                              │         │
│                                              └─────────┘ MCP SSE
│
│  ┌──────────────────┐  ┌──────────────────┐
│  │    Dashboard     │  │    Redis         │  event queue · dedup
│  │    FastAPI :8080 │  │    :6379         │  approvals · activity log
│  └──────────────────┘  └──────────────────┘
│
│  ┌──────────────────┐  ┌──────────────────┐
│  │    ChromaDB      │  │    Twilio        │  (optional)
│  │  vector memory   │  │  phone calls     │
│  └──────────────────┘  └──────────────────┘
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Concurrency Model

The system uses **two isolated event loops**:

| Loop | Runs in | Contains |
|------|---------|----------|
| **Main loop** | `asyncio.run()` in the main thread | Observer task, Planner task, Executor task, Dashboard (uvicorn) |
| **MCP loop** | Daemon thread (`mcp-server`) | MCP tool server (uvicorn + FastMCP SSE) |

The MCP server runs in a separate thread with its own event loop so that:
- Its uvicorn server doesn't compete with the agents' event loop.
- On Windows, `asyncio.ProactorEventLoop` (default) cannot nest event loops, but each thread can have its own.
- A daemon thread is automatically killed when the main process exits, avoiding manual teardown.

The main loop agents connect to the MCP server via SSE (`httpx`). Each agent maintains its own persistent SSE connection — one `ClientSession` per agent.

### Agent tasks

All agent tasks are created with `asyncio.create_task()` and gathered by `asyncio.gather()` in `main()`. If any task raises an unhandled exception, `gather()` propagates it. Each agent's `start()` loop catches `Exception` and reconnects; `CancelledError` triggers a clean disconnect and re-raise.

---

## MCP Tool Server

```
fastmcp.FastMCP("PersonalOS")
    ├── register_gmail_tools(mcp)       → read_emails, send_email
    ├── register_calendar_tools(mcp)    → read_calendar, create_event
    └── register_filesystem_tools(mcp) → list_files, move_file
```

`build_mcp_app()` is idempotent — a module-level `_tools_registered` flag prevents double registration if the function is called more than once.

The server exposes:
- `GET /sse` — SSE event stream (agents connect here first; the server replies with an endpoint URL)
- `POST /messages/?session_id=<uuid>` — agents POST JSON-RPC requests here (per-session)

Each agent connection creates a unique session ID on the server, tracked by the SSE transport layer.

### Filesystem sandbox

`list_files` and `move_file` resolve paths relative to `FS_ALLOWED_ROOT` and reject any `..` traversal attempts. Paths are resolved with `Path.resolve()` and checked against the root before any OS call.

---

## Agent Internals

### BaseAgent

`agents/base_agent.py`

```
BaseAgent (ABC)
├── connect_mcp()      — enters sse_client + ClientSession async context managers
├── disconnect_mcp()   — exits both context managers (with try/except)
├── call_tool(name, args) — calls session.call_tool() with 3-attempt retry + back-off
├── start()            — lifecycle loop: connect → run → (on error) disconnect → sleep → repeat
└── run()              — abstract: implemented by each subclass
```

**`call_tool()` retry logic:**

```
attempt 0  →  call
  fail       →  wait 1s, attempt 1
  fail       →  wait 2s, attempt 2
  fail       →  raise RuntimeError("MCP tool '...' failed after 3 attempts")
```

Exceptions with empty `__str__` (e.g. `ClosedResourceError`, `BrokenResourceError`) are logged with their type name so retry logs are actionable.

**`start()` loop:**

```
while True:
    try:
        connect_mcp()
        run()         ← agent-specific loop
    except CancelledError:
        disconnect_mcp()  (shielded)
        raise
    except Exception:
        log "agent_crashed"
        disconnect_mcp()
        sleep 5s
        # loop again → reconnect
```

---

### ObserverAgent

`agents/observer_agent.py`

```
run()
└── loop every OBSERVER_POLL_INTERVAL seconds:
    _poll_all_sources()
    ├── asyncio.gather(
    │     _poll_emails()     → call_tool("read_emails", ...)
    │     _poll_calendar()   → call_tool("read_calendar", ...)
    │     _poll_files()      → call_tool("list_files", ...)
    │   return_exceptions=True)
    ├── normalize each result to {id, source, data, timestamp}
    ├── if ALL three sources failed → raise RuntimeError (triggers reconnect)
    └── for each event:
        if not is_event_seen(event.id):
            mark_event_seen(event.id)
            push_event(event)   → Redis events:queue LPUSH
```

**Deduplication:** each event ID is stored as a Redis string key `seen_event:<id>` with a 24-hour TTL via `SETEX`. This ensures each event is only processed once per day even if the system restarts.

---

### PlannerAgent

`agents/planner_agent.py`

```
run()
└── loop:
    pop_event()   → Redis events:queue BRPOP (blocking, 5s timeout)
    if event:
        _plan_event(event)
        ├── retrieve preference history from ChromaDB
        ├── call OpenRouter LLM with system prompt + event JSON
        │     → structured JSON: {action, tool, args, confidence, urgency, alternatives}
        ├── adjust confidence using ChromaDB approval_rate history
        └── push to approvals:pending  → Redis LPUSH
```

**LLM prompt structure:**

The system prompt instructs the model to return valid JSON with:
- `action` — human-readable description of what to do
- `tool` — the MCP tool name to call
- `args` — tool arguments as a JSON object
- `confidence` — float 0–1 representing certainty
- `urgency` — `"low"` | `"medium"` | `"high"`
- `alternatives` — list of alternative action objects (same schema)

The model is called with `response_format={"type": "json_object"}` to enforce JSON output.

---

### ExecutorAgent

`agents/executor_agent.py`

```
run()
└── loop:
    pop_approval()   → Redis approvals:pending BRPOP (blocking, 5s timeout)
    if plan:
        _route_plan(plan)
        ├── confidence >= 0.90 or plan.override == true:
        │     execute_plan(plan)
        │     ├── call_tool(plan.tool, plan.args)
        │     ├── notifier.call()   → Twilio (or simulation)
        │     └── record_outcome(plan, "approved_auto") → ChromaDB
        ├── 0.70 <= confidence < 0.90:
        │     push_to_dashboard(plan) → Redis dashboard:pending LPUSH
        │     └── log_activity("pending_approval", ...) → Redis activity:log
        └── confidence < 0.70:
              log_activity("discarded_low_confidence", ...)
              record_outcome(plan, "discarded") → ChromaDB
```

When a plan is manually approved via the dashboard, the dashboard API re-queues it to `approvals:pending` with `override=true`, so the Executor auto-routes it.

#### Voice Approval Sub-flow

For medium-confidence plans (70–89%), if `TWILIO_WEBHOOK_BASE_URL` is set, the Executor also places an outbound voice call in parallel with the dashboard push:

```
Email arrives (70-89% confidence)
  ↓
Executor._push_to_dashboard()
  ↓
Twilio call placed → url=/api/twilio/voice/{plan_id}
  ↓
User answers phone:
  "Hey! Requested action: send_email.
   Email: Professor deadline. Say yes, no, or modify..."
  ↓
User says: "Yes, reply that I'll send it by evening"
  ↓
POST /api/twilio/speech/{plan_id}  ← Twilio sends SpeechResult
  ↓
LLM classifies: MODIFY  (with instruction: "reply that I'll send it by evening")
  ↓
plan["action_args"]["body"] += "\n[Voice instruction: reply that I'll send it by evening]"
plan["approved_override"] = True
  ↓
push back to approvals:pending → Executor auto-executes
  ↓
User hears: "Got it! I will send email with your changes. Goodbye!"
  ↓
Dashboard updates live via WebSocket
```

Key implementation details:
- Plan stored in Redis at `voice:plan:{plan_id}` with 5-minute TTL
- LLM intent: `APPROVE` / `REJECT` / `MODIFY` / `UNCLEAR` (keyword fallback if LLM fails)
- `APPROVE` / `MODIFY` → `approved_override=True` → re-queued to `approvals:pending`
- `REJECT` → removed from `dashboard:pending`, marked `rejected_by_voice`
- `UNCLEAR` → re-prompts the caller once before hanging up
- Gracefully skipped when `TWILIO_WEBHOOK_BASE_URL` is not set

---

## Data Flows

### Event: "new unread email arrives"

```
Gmail API
  → ObserverAgent._poll_emails() via MCP read_emails
  → normalize to {id: "gmail:msg_19d6...", source: "gmail", data: {...}}
  → dedup check: seen_event:gmail:msg_19d6... not in Redis
  → mark seen, push to events:queue
  → PlannerAgent pops event
  → OpenRouter LLM produces plan: {action: "Reply to Alice about meeting", tool: "send_email", confidence: 0.85}
  → push to approvals:pending
  → ExecutorAgent routes: 0.85 → push to dashboard:pending
  → User sees "Reply to Alice" in dashboard
  → User clicks Approve
  → Dashboard API pushes to approvals:pending with override=true
  → ExecutorAgent: override=true → execute send_email(to, subject, body)
  → Twilio call: "Action executed: Reply to Alice about meeting"
  → record_outcome("approved_manual") in ChromaDB
```

### Redis key schema

| Key | Type | Purpose |
|---|---|---|
| `events:queue` | List | LPUSH by Observer, BRPOP by Planner |
| `approvals:pending` | List | LPUSH by Planner/Dashboard, BRPOP by Executor |
| `dashboard:pending` | Hash | HSET by Executor (borderline confidence); field=plan_id, value=JSON plan |
| `emails:all` | Hash | HSET by Planner/Executor for every routed plan; field=plan_id, value=JSON |
| `seen_event:<id>` | String | TTL=86400 — deduplication per event ID |
| `activity:log` | List | RPUSH by all agents; last 50 entries served by `/api/feed` |

---

## Memory Subsystem

### Redis (`memory/redis_client.py`)

Async singleton wrapping `redis.asyncio.from_url()`. All agents share the same singleton instance. Key operations:

- `push_event(event)` / `pop_event()` — LPUSH / BRPOP on `events:queue`
- `push_approval(plan)` / `pop_approval()` — LPUSH / BRPOP on `approvals:pending`
- `push_dashboard_item(plan)` — HSET on `dashboard:pending` (field = plan id)
- `get_dashboard_items()` — HGETALL on `dashboard:pending`
- `remove_dashboard_item(id)` — HDEL on `dashboard:pending`
- `push_email_record(plan)` — HSET on `emails:all` (field = plan id)
- `get_email_records()` — HGETALL on `emails:all`, sorted newest-first
- `mark_event_seen(id)` — SETEX `seen_event:<id>` TTL 86400
- `is_event_seen(id)` — EXISTS `seen_event:<id>`
- `clear_event_seen(id)` — DEL `seen_event:<id>` (used by seed scripts)
- `log_activity(entry)` — RPUSH on `activity:log`

### ChromaDB (`memory/chroma_memory.py`)

Two collections:

| Collection | Documents | Use |
|---|---|---|
| `user_preferences` | Free-text preference statements | Semantic search to retrieve relevant preferences at planning time |
| `action_outcomes` | `{action, outcome, timestamp}` | Historical approval/rejection rate used by Planner to calibrate confidence |

All Chroma calls are wrapped in `asyncio.to_thread()` since the Chroma client is synchronous.

**Confidence calibration formula (Planner):**

```python
past = retrieve_similar_outcomes(plan.action, n=5)
approval_rate = sum(1 for o in past if o["outcome"] == "approved") / len(past)
adjusted_confidence = 0.7 * raw_confidence + 0.3 * approval_rate
```

---

## Human Approval Dashboard

`api/app.py` — FastAPI application served on port 8080. Routers live in `api/routers/`; the frontend is served as static files from `api/static/`.

```
GET  /                      → dashboard.html (FileResponse from api/static/)
GET  /api/emails            → all plans sorted newest-first
GET  /api/pending           → plans awaiting human approval
POST /api/approve/{id}      → approve → re-queue to approvals:pending (override=true)
POST /api/reject/{id}       → reject → record_outcome("rejected") in ChromaDB
GET  /api/feed              → last 50 activity log entries (newest first)
GET  /api/health            → {redis, openrouter, google, twilio, chromadb, mcp}
GET  /api/metrics           → confidence histogram, priority/response breakdown, queue depths
GET  /api/preferences       → list ChromaDB user-preference documents
POST /api/preferences       → add a new natural-language preference statement
POST /api/events/inject     → inject a synthetic event into events:queue (demo mode)
POST /api/twilio/test       → trigger a real or simulated Twilio test call
GET  /ws                    → WebSocket — server pushes {"type":"refresh"} on state changes
```

The frontend JavaScript connects to `/ws` on page load. When a plan is approved, rejected, or injected, the server broadcasts `{"type":"refresh"}` to all connected clients, triggering an immediate data reload. A keep-alive ping fires every 20 s. The client falls back to 8 s polling if the WebSocket connection is unavailable.

---

## Configuration System

`config/settings.py` uses `pydantic-settings`:

```python
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()   # reads .env once, cached forever
```

All modules call `get_settings()`. The `@lru_cache` ensures the `.env` file is parsed exactly once per process lifetime, making the settings object effectively a singleton with no global state.

---

## Startup Sequence

```
python main.py  (thin entry point — delegates to core/bootstrap.py)
  1. start_mcp_server_thread()    — daemon thread started, begins uvicorn on port 8000
  2. asyncio.run(run(args))       — core/bootstrap.py::run()
  3. setup_logging()
  4. check_redis()                — ping; fail fast if Redis is down
  5. wait_for_mcp_server()        — HTTP GET probe every 0.5s (max 30s)
  6. create_task(observer.start())
  7. create_task(planner.start())
  8. create_task(executor.start())
  9. create_task(dashboard_server.serve())  — uvicorn serving api/app.py on port 8080
 10. asyncio.gather(*tasks)
```

Each agent's `start()` does:
```
connect_mcp()
  ├── sse_client.__aenter__()     → opens SSE stream, receives endpoint URL
  └── ClientSession.__aenter__()  → JSON-RPC initialize handshake
run()
  └── agent-specific loop
```

---

## Shutdown Sequence

```
Ctrl+C
  → asyncio event loop cancels all tasks
  → each task receives CancelledError at the next await point
  → BaseAgent.start() catches CancelledError
      → asyncio.shield(disconnect_mcp())
          → ClientSession.__aexit__()
          → sse_client.__aexit__()   ← MUST run in the same task that opened it
      → raise CancelledError
  → asyncio.gather() propagates CancelledError
  → asyncio.run() exits cleanly
  → MCP server daemon thread terminates automatically
```

The `asyncio.shield()` call in `BaseAgent.start()` is critical: it prevents a second `CancelledError` from interrupting the disconnect coroutine mid-way. Without it, the `sse_client` async generator's anyio task group is left open and Python's GC tries to close it from the wrong task, producing noisy `RuntimeError: Attempted to exit cancel scope in a different task` tracebacks.

---

## Extension Points

### New data source (e.g. Slack, Linear)

1. Add a new tool file in `mcp_server/` with `@mcp.tool()` functions.
2. Register it in `build_mcp_app()`.
3. Add a `_poll_<source>()` method in `ObserverAgent`.
4. Add the coroutine to the `asyncio.gather()` call in `_poll_all_sources()`.
5. Increment the failure threshold in the "all sources failed" guard.

### New action type (e.g. GitHub PR, Notion page)

1. Add the MCP tool.
2. Update the Planner's system prompt to describe the new tool.
3. Add a dispatch branch in `ExecutorAgent._route_plan()` if the new tool requires different routing logic.

### Different LLM provider

Change `OPENROUTER_BASE_URL` and `OPENROUTER_MODEL` in `.env`. Any OpenAI-compatible endpoint works. The Planner uses `AsyncOpenAI(base_url=..., api_key=...)` with no provider-specific code.

### Persistent event storage

Currently, events processed by the Observer are not stored long-term (only their IDs are stored in Redis for deduplication). To add persistence, add a `log_event(event)` call in `ObserverAgent` that writes to a database or appends to a file before pushing to the queue.
