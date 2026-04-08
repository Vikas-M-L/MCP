# Contributing to PersonalOS Agent

Thank you for your interest in contributing! This document covers how to set up a development environment, the coding conventions used throughout the project, and the pull-request process.

---

## Table of Contents

1. [Development Setup](#development-setup)
2. [Project Conventions](#project-conventions)
3. [Adding a New MCP Tool](#adding-a-new-mcp-tool)
4. [Adding a New Agent](#adding-a-new-agent)
5. [Testing](#testing)
6. [Pull Request Checklist](#pull-request-checklist)

---

## Development Setup

### Requirements

- Python 3.11+
- Redis (local or Docker)
- A virtual environment (strongly recommended)

### Install

```bash
git clone https://github.com/your-org/personal-os-agent.git
cd personal-os-agent

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### Environment

```bash
cp .env.example .env
# Fill in at minimum OPENROUTER_API_KEY
```

### Pre-flight

```bash
python check.py   # all checks except MCP should be green
```

### Running without Google OAuth

If you don't have Google credentials set up, use demo mode:

```bash
python main.py --skip-poll     # start without Observer
python tests/seed_events.py   # inject synthetic events
```

---

## Project Conventions

### Code style

- **Formatter**: `black` (line length 100). Run `black .` before committing.
- **Linter**: `ruff` — use the defaults. Run `ruff check .` before committing.
- **Type hints**: all function signatures must have type annotations.
- **Docstrings**: module-level and public functions use plain triple-quoted strings. Avoid obvious comments that restate the code.

### Logging

Use the module-level `structlog` logger obtained via:

```python
import structlog
logger = structlog.get_logger(__name__)
```

Log structured data, not f-strings:

```python
# Good
logger.info("email_processed", message_id=msg_id, subject=subject)

# Bad
logger.info(f"Processed email {msg_id}: {subject}")
```

### Configuration

- All settings live in `config/settings.py` as `Settings` fields.
- Never read `os.environ` directly outside `settings.py`.
- Provide a default value for every new setting so the system runs in demo mode without extra configuration.

### Error handling

- Agent task loops (`start()`) must not swallow `asyncio.CancelledError` — always `raise` after cleanup.
- MCP tool functions should raise `ValueError` for invalid inputs and `RuntimeError` for service-layer failures.
- Prefer returning typed error dicts (`{"success": False, "error": "..."}`) over raising exceptions from MCP tool handlers, since the MCP framework serialises these back to the caller cleanly.

### Async rules

- Never call blocking I/O directly in an `async` function. Wrap synchronous calls (e.g. ChromaDB, Google API) in `asyncio.to_thread(...)`.
- Do not use `asyncio.create_task()` inside MCP tool calls or inside methods that are themselves called from within the `sse_client` anyio task group — use `asyncio.gather(*coroutines)` or sequential `await` calls instead.

---

## Adding a New MCP Tool

1. **Create or extend a tool file** in `mcp_server/`:

```python
# mcp_server/my_tools.py
from mcp_server.server import mcp   # shared FastMCP instance

@mcp.tool()
async def my_tool(param: str) -> dict:
    """
    One-line description of what the tool does.
    Returns {result, ...}.
    """
    # implementation
    return {"result": ...}
```

2. **Register it** in `mcp_server/server.py` inside `build_mcp_app()`:

```python
from mcp_server.my_tools import register_my_tools   # or direct import

# inside build_mcp_app():
register_my_tools(mcp)
```

3. **Expose it to agents** — add the tool name to the Observer's polling loop or the Executor's action dispatcher as appropriate.

4. **Update the MCP Tools Reference table** in `README.md`.

---

## Adding a New Agent

1. Subclass `BaseAgent` in `agents/`:

```python
from agents.base_agent import BaseAgent

class MyAgent(BaseAgent):
    async def run(self) -> None:
        """Main agent loop — called by start() after MCP is connected."""
        while True:
            # your logic here — use self.call_tool("tool_name", {...}) for MCP
            await asyncio.sleep(interval)
```

2. Instantiate and register the task in `main.py`:

```python
from agents.my_agent import MyAgent
my_agent = MyAgent()
tasks.append(asyncio.create_task(my_agent.start(), name="my-agent"))
```

3. Agents must not start background `asyncio.Task` objects that outlive the `start()` coroutine's lifecycle.

---

## Testing

There is currently no automated test suite; contributions that add tests are very welcome.

### Manual integration test

```bash
# Inject demo events and watch the Planner + Executor process them:
python main.py --skip-poll &
python tests/seed_events.py
# Observe the dashboard at http://localhost:8080
```

### Health check

```bash
python check.py
```

All 35 checks (minus the MCP server check, which requires `main.py` to be running) should pass before submitting a PR.

---

## Pull Request Checklist

- [ ] `black .` — no formatting changes remain.
- [ ] `ruff check .` — no linting errors.
- [ ] `python check.py` — all applicable checks pass.
- [ ] New MCP tools are covered in the README tools table.
- [ ] New settings are documented in the README configuration table and added to `.env.example`.
- [ ] `credentials.json`, `token.json`, and `.env` are **not** included in the diff.
- [ ] CHANGELOG.md has an entry under `[Unreleased]`.

---

## Questions?

Open a [GitHub Discussion](https://github.com/your-org/personal-os-agent/discussions) or create an issue with the `question` label.
